"""Unit and integration tests for SQLiteBudgetPolicyEngine and deployweave-wallet CLI."""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

import deployweave
from deployweave.cli import main as cli_main
from deployweave.policy import PolicyDecision, SQLiteBudgetPolicyEngine
from sqlite_reservation_manager import SQLiteReservationManager


class TestSQLiteBudgetPolicyEngine:
    def test_package_exports(self):
        assert deployweave.SQLiteBudgetPolicyEngine is SQLiteBudgetPolicyEngine
        assert deployweave.PolicyDecision is PolicyDecision

    def test_file_backed_uses_wal_mode(self, tmp_path):
        db_file = tmp_path / "policy.db"
        with SQLiteBudgetPolicyEngine(db_file) as engine:
            engine.set_policy("c-wal", max_rpm=10)
            conn = sqlite3.connect(db_file)
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                conn.close()
        assert mode.lower() == "wal"

    def test_set_and_get_policy_validation(self, tmp_path):
        with SQLiteBudgetPolicyEngine(tmp_path / "policy.db") as engine:
            default_policy = engine.get_policy("unconfigured")
            assert default_policy["max_rpm"] is None
            assert default_policy["max_tpm"] is None
            assert default_policy["daily_token_cap"] is None

            saved = engine.set_policy("c-1", max_rpm=5, max_tpm=1000, daily_token_cap=5000)
            assert saved["max_rpm"] == 5
            assert saved["max_tpm"] == 1000
            assert saved["daily_token_cap"] == 5000

            fetched = engine.get_policy("c-1")
            assert fetched["max_rpm"] == 5
            assert fetched["max_tpm"] == 1000
            assert fetched["daily_token_cap"] == 5000

            with pytest.raises(ValueError):
                engine.set_policy("", max_rpm=5)
            with pytest.raises(ValueError):
                engine.set_policy("c-1", max_rpm=-1)
            with pytest.raises(TypeError):
                engine.set_policy("c-1", max_tpm=True)  # type: ignore[arg-type]
            with pytest.raises(ValueError):
                engine.check_and_record("c-1", estimated_tokens=-10)

    def test_rpm_limit_enforcement_and_window_expiry(self, tmp_path):
        base_ts = 1_700_000_000.0
        with SQLiteBudgetPolicyEngine(tmp_path / "policy.db") as engine:
            engine.set_policy("c-rpm", max_rpm=2)

            d1 = engine.check_and_record("c-rpm", estimated_tokens=100, now_ts=base_ts)
            d2 = engine.check_and_record("c-rpm", estimated_tokens=100, now_ts=base_ts + 10.0)
            d3 = engine.check_and_record("c-rpm", estimated_tokens=100, now_ts=base_ts + 20.0)

            assert d1.allowed is True
            assert d1.rpm_used == 1
            assert d2.allowed is True
            assert d2.rpm_used == 2
            assert d3.allowed is False
            assert d3.reason == "max_rpm_exceeded"
            assert d3.rpm_used == 2
            assert d3.retry_after_seconds == pytest.approx(40.0)

            # Denied request must not have been recorded
            summary = engine.get_usage_summary("c-rpm", now_ts=base_ts + 20.0)
            assert summary["rpm_used"] == 2
            assert summary["rpm_remaining"] == 0
            assert summary["total_requests"] == 2

            # Once base_ts expires out of the 60s rolling window, one slot frees up
            d4 = engine.check_and_record("c-rpm", estimated_tokens=100, now_ts=base_ts + 60.0)
            assert d4.allowed is True
            assert d4.rpm_used == 2

            # Still at max_rpm=2 until base_ts + 10.0 expires
            d5 = engine.check_and_record("c-rpm", estimated_tokens=100, now_ts=base_ts + 65.0)
            assert d5.allowed is False
            assert d5.reason == "max_rpm_exceeded"
            assert d5.retry_after_seconds == pytest.approx(5.0)

    def test_tpm_limit_enforcement_and_window_expiry(self, tmp_path):
        base_ts = 1_700_000_000.0
        with SQLiteBudgetPolicyEngine(tmp_path / "policy.db") as engine:
            engine.set_policy("c-tpm", max_tpm=500)

            d1 = engine.check_and_record("c-tpm", estimated_tokens=300, now_ts=base_ts)
            assert d1.allowed is True
            assert d1.tpm_used == 300

            d2 = engine.check_and_record("c-tpm", estimated_tokens=250, now_ts=base_ts + 5.0)
            assert d2.allowed is False
            assert d2.reason == "max_tpm_exceeded"
            assert d2.tpm_used == 300
            assert d2.retry_after_seconds == pytest.approx(55.0)

            # Smaller request fitting within remaining 200 TPM is still allowed
            d3 = engine.check_and_record("c-tpm", estimated_tokens=200, now_ts=base_ts + 10.0)
            assert d3.allowed is True
            assert d3.tpm_used == 500

            # After first event expires, 300 tokens are available again
            d4 = engine.check_and_record("c-tpm", estimated_tokens=300, now_ts=base_ts + 60.0)
            assert d4.allowed is True
            assert d4.tpm_used == 500

    def test_hard_daily_token_cap_and_utc_day_reset(self, tmp_path):
        # Exact UTC midnight: 1_700_006_400 == 2023-11-15T00:00:00Z
        day_start = 1_700_006_400.0
        with SQLiteBudgetPolicyEngine(tmp_path / "policy.db") as engine:
            engine.set_policy("c-day", max_rpm=100, max_tpm=1000, daily_token_cap=1000)

            d1 = engine.check_and_record("c-day", estimated_tokens=600, now_ts=day_start + 10.0)
            assert d1.allowed is True
            assert d1.daily_tokens_used == 600

            # Advance 2 hours so RPM/TPM windows are completely empty, but still same UTC day
            d2 = engine.check_and_record("c-day", estimated_tokens=400, now_ts=day_start + 7200.0)
            assert d2.allowed is True
            assert d2.daily_tokens_used == 1000

            # Even 1 more token on the same UTC day must be rejected
            d3 = engine.check_and_record("c-day", estimated_tokens=1, now_ts=day_start + 7300.0)
            assert d3.allowed is False
            assert d3.reason == "daily_token_cap_exceeded"
            assert d3.daily_tokens_used == 1000
            assert d3.retry_after_seconds == pytest.approx(86_400.0 - 7300.0)

            # Next UTC day resets the daily token cap
            d4 = engine.check_and_record("c-day", estimated_tokens=500, now_ts=day_start + 86_400.0)
            assert d4.allowed is True
            assert d4.daily_tokens_used == 500

    def test_persistence_across_reopen(self, tmp_path):
        db_file = tmp_path / "persist.db"
        ts = 1_700_006_400.0 + 30.0

        with SQLiteBudgetPolicyEngine(db_file) as engine1:
            engine1.set_policy("c-persist", max_rpm=3, max_tpm=1000, daily_token_cap=2000)
            d1 = engine1.check_and_record("c-persist", estimated_tokens=400, now_ts=ts)
            d2 = engine1.check_and_record("c-persist", estimated_tokens=300, now_ts=ts + 1.0)
            assert d1.allowed is True
            assert d2.allowed is True

        # Reopen a brand new engine instance against the same SQLite DB
        with SQLiteBudgetPolicyEngine(db_file) as engine2:
            summary = engine2.get_usage_summary("c-persist", now_ts=ts + 2.0)
            assert summary["rpm_used"] == 2
            assert summary["rpm_remaining"] == 1
            assert summary["tpm_used"] == 700
            assert summary["tpm_remaining"] == 300
            assert summary["daily_tokens_used"] == 700
            assert summary["daily_tokens_remaining"] == 1300

            # Exceeding remaining 300 TPM is rejected after reopen
            denied = engine2.check_and_record("c-persist", estimated_tokens=301, now_ts=ts + 3.0)
            assert denied.allowed is False
            assert denied.reason == "max_tpm_exceeded"

            # Fitting within remaining 300 TPM and 1 RPM succeeds
            allowed = engine2.check_and_record("c-persist", estimated_tokens=300, now_ts=ts + 4.0)
            assert allowed.allowed is True
            assert allowed.rpm_used == 3
            assert allowed.tpm_used == 1000

    def test_concurrent_check_and_record_atomicity(self, tmp_path):
        db_file = tmp_path / "concurrent.db"
        ts = 1_700_006_400.0 + 100.0
        with SQLiteBudgetPolicyEngine(db_file) as engine:
            engine.set_policy("c-conc", max_rpm=4, max_tpm=1000, daily_token_cap=1000)

            def _worker(_idx: int) -> bool:
                return engine.check_and_record("c-conc", estimated_tokens=100, now_ts=ts).allowed

            with ThreadPoolExecutor(max_workers=8) as pool:
                outcomes = list(pool.map(_worker, range(12)))

            assert outcomes.count(True) == 4
            assert outcomes.count(False) == 8
            summary = engine.get_usage_summary("c-conc", now_ts=ts)
            assert summary["rpm_used"] == 4
            assert summary["tpm_used"] == 400
            assert summary["total_requests"] == 4

    def test_usage_summary_includes_wallet_when_reservation_manager_provided(self, tmp_path):
        db_file = tmp_path / "shared.db"
        with (
            SQLiteReservationManager(db_file) as mgr,
            SQLiteBudgetPolicyEngine(db_file, reservation_manager=mgr) as engine,
        ):
            mgr.credit_wallet("c-shared", input_tokens=500, output_tokens=250)
            engine.set_policy("c-shared", max_rpm=10)
            engine.check_and_record("c-shared", estimated_tokens=120, now_ts=1_700_006_400.0)

            summary = engine.get_usage_summary("c-shared", now_ts=1_700_006_405.0)
            assert summary["rpm_used"] == 1
            assert summary["wallet"] is not None
            assert summary["wallet"]["input"]["remaining"] == 500
            assert summary["wallet"]["output"]["remaining"] == 250


class TestDeployWeaveWalletCLI:
    def test_cli_credit_reserve_commit_balance_round_trip(self, tmp_path, capsys):
        db_file = str(tmp_path / "cli_wallet.db")

        # 1. credit (--db and --json before subcommand)
        rc = cli_main(
            ["--db", db_file, "--json", "credit", "tenant-1", "--input-tokens", "1000", "--output-tokens", "500"]
        )
        assert rc == 0
        credit_out = json.loads(capsys.readouterr().out)
        assert credit_out["contract_id"] == "tenant-1"
        assert credit_out["input"]["remaining"] == 1000
        assert credit_out["output"]["remaining"] == 500

        # 2. reserve (--db and --json after subcommand)
        rc = cli_main(
            [
                "reserve",
                "tenant-1",
                "--input-tokens",
                "300",
                "--output-tokens",
                "150",
                "--reservation-id",
                "res-1",
                "--db",
                db_file,
                "--json",
            ]
        )
        assert rc == 0
        res_out = json.loads(capsys.readouterr().out)
        assert res_out["reservation_id"] == "res-1"
        assert res_out["balance"]["input"]["reserved"] == 300
        assert res_out["balance"]["input"]["remaining"] == 700
        assert res_out["balance"]["active_reservations"] == 1

        # 3. commit with under-usage refund
        rc = cli_main(
            [
                "commit",
                "tenant-1",
                "res-1",
                "--actual-input",
                "200",
                "--actual-output",
                "100",
                "--cached-input",
                "50",
                "--db",
                db_file,
                "--json",
            ]
        )
        assert rc == 0
        commit_out = json.loads(capsys.readouterr().out)
        assert commit_out["input"]["consumed"] == 200
        assert commit_out["input"]["remaining"] == 800
        assert commit_out["input"]["cached"] == 50
        assert commit_out["refunded_input"] == 100
        assert commit_out["refunded_output"] == 50

        # 4. balance
        rc = cli_main(["balance", "tenant-1", "--db", db_file, "--json"])
        assert rc == 0
        bal_out = json.loads(capsys.readouterr().out)
        assert bal_out["input"]["allocated"] == 1000
        assert bal_out["input"]["consumed"] == 200
        assert bal_out["input"]["reserved"] == 0
        assert bal_out["input"]["remaining"] == 800
        assert bal_out["output"]["consumed"] == 100
        assert bal_out["output"]["remaining"] == 400
        assert bal_out["active_reservations"] == 0

    def test_cli_release_set_policy_usage_and_policy_gate(self, tmp_path, capsys):
        db_file = str(tmp_path / "cli_policy.db")

        assert cli_main(["credit", "tenant-2", "--input", "1000", "--output", "1000", "--db", db_file, "--json"]) == 0
        capsys.readouterr()

        # Reserve and release
        assert (
            cli_main(
                ["reserve", "tenant-2", "--input", "400", "--output", "200", "--id", "r-rel", "--db", db_file, "--json"]
            )
            == 0
        )
        capsys.readouterr()

        assert cli_main(["release", "tenant-2", "r-rel", "--db", db_file, "--json"]) == 0
        rel_out = json.loads(capsys.readouterr().out)
        assert rel_out["released"] is True

        # Idempotent second release returns released=False
        assert cli_main(["release", "tenant-2", "r-rel", "--db", db_file, "--json"]) == 0
        rel_out_2 = json.loads(capsys.readouterr().out)
        assert rel_out_2["released"] is False

        # Set policy
        assert (
            cli_main(
                [
                    "set-policy",
                    "tenant-2",
                    "--max-rpm",
                    "1",
                    "--max-tpm",
                    "500",
                    "--daily-token-cap",
                    "1000",
                    "--db",
                    db_file,
                    "--json",
                ]
            )
            == 0
        )
        pol_out = json.loads(capsys.readouterr().out)
        assert pol_out["max_rpm"] == 1
        assert pol_out["max_tpm"] == 500
        assert pol_out["daily_token_cap"] == 1000

        # First reserve with --check-policy passes
        rc = cli_main(
            [
                "reserve",
                "tenant-2",
                "--input",
                "100",
                "--output",
                "50",
                "--id",
                "r-pol-1",
                "--check-policy",
                "--db",
                db_file,
                "--json",
            ]
        )
        assert rc == 0
        r1_out = json.loads(capsys.readouterr().out)
        assert r1_out["policy_decision"]["allowed"] is True

        # Second reserve immediately hits max_rpm=1 and exits with code 2
        rc = cli_main(
            [
                "reserve",
                "tenant-2",
                "--input",
                "100",
                "--output",
                "50",
                "--id",
                "r-pol-2",
                "--check-policy",
                "--db",
                db_file,
                "--json",
            ]
        )
        assert rc == 2
        denied_out = json.loads(capsys.readouterr().out)
        assert denied_out["allowed"] is False
        assert denied_out["reason"] == "max_rpm_exceeded"

        # Usage reflects 1 recorded request / 150 tokens
        assert cli_main(["usage", "tenant-2", "--db", db_file, "--json"]) == 0
        usage_out = json.loads(capsys.readouterr().out)
        assert usage_out["rpm_used"] == 1
        assert usage_out["rpm_remaining"] == 0
        assert usage_out["tpm_used"] == 150
        assert usage_out["daily_tokens_used"] == 150
        assert usage_out["wallet"]["active_reservations"] == 1

    def test_cli_error_and_human_output(self, tmp_path, capsys):
        db_file = str(tmp_path / "cli_err.db")

        # Missing contract returns exit code 1 with structured JSON error on stderr
        rc = cli_main(["balance", "does-not-exist", "--db", db_file, "--json"])
        assert rc == 1
        captured = capsys.readouterr()
        err_payload = json.loads(captured.err)
        assert err_payload["error_type"] == "ContractNotFoundError"

        # Human-readable output (without --json)
        rc = cli_main(["credit", "human-1", "--input", "42", "--output", "10", "--db", db_file])
        assert rc == 0
        human_out = capsys.readouterr().out
        assert "contract_id: human-1" in human_out
        assert "allocated=42" in human_out
