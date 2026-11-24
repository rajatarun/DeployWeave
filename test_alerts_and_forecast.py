"""Unit and integration tests for SQLiteBudgetAlertStore, budget forecasting, and CLI."""
from __future__ import annotations

import json
import math
import sqlite3

import pytest

import deployweave
from deployweave.alerts import (
    DEFAULT_ALERT_THRESHOLD_PCT,
    SQLiteBudgetAlertStore,
    evaluate_budget_forecast,
)
from deployweave.cli import main as cli_main
from deployweave.policy import SQLiteBudgetPolicyEngine


class TestSQLiteBudgetAlertStore:
    def test_package_exports(self):
        assert deployweave.SQLiteBudgetAlertStore is SQLiteBudgetAlertStore
        assert deployweave.evaluate_budget_forecast is evaluate_budget_forecast

    def test_file_backed_uses_wal_mode(self, tmp_path):
        db_file = tmp_path / "alerts.db"
        with SQLiteBudgetAlertStore(db_file) as store:
            store.set_threshold("c-wal", 0.75)
            conn = sqlite3.connect(db_file)
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                conn.close()
        assert mode.lower() == "wal"

    def test_threshold_defaults_persistence_and_validation(self, tmp_path):
        db_file = tmp_path / "thresholds.db"
        with SQLiteBudgetAlertStore(db_file) as store:
            assert store.get_threshold("unconfigured") == pytest.approx(DEFAULT_ALERT_THRESHOLD_PCT)
            saved = store.set_threshold("c-1", 0.65)
            assert saved["contract_id"] == "c-1"
            assert saved["alert_threshold_pct"] == pytest.approx(0.65)
            assert store.get_threshold("c-1") == pytest.approx(0.65)

            # Update existing threshold
            store.set_threshold("c-1", 0.9)
            assert store.get_threshold("c-1") == pytest.approx(0.9)

            # Validation errors
            with pytest.raises(ValueError):
                store.set_threshold("", 0.8)
            with pytest.raises(ValueError):
                store.set_threshold("c-1", 0.0)
            with pytest.raises(ValueError):
                store.set_threshold("c-1", -0.2)
            with pytest.raises(ValueError):
                store.set_threshold("c-1", 1.05)
            with pytest.raises(ValueError):
                store.set_threshold("c-1", math.nan)
            with pytest.raises(TypeError):
                store.set_threshold("c-1", True)  # type: ignore[arg-type]

        # Reopen and verify persistence
        with SQLiteBudgetAlertStore(db_file) as store:
            assert store.get_threshold("c-1") == pytest.approx(0.9)

    def test_record_and_list_alerts_filtering_and_limit(self, tmp_path):
        db_file = tmp_path / "alerts_list.db"
        with SQLiteBudgetAlertStore(db_file) as store:
            a1 = store.record_alert(
                "c-a",
                alert_level="warning",
                utilization_pct=0.82,
                daily_used=820,
                daily_cap=1000,
                threshold_pct=0.8,
                burn_rate_tokens_per_hour=400.0,
                estimated_hours_remaining=0.45,
                triggered_at=1_700_000_100.0,
            )
            a2 = store.record_alert(
                "c-b",
                alert_level="critical",
                utilization_pct=1.0,
                daily_used=500,
                daily_cap=500,
                threshold_pct=0.8,
                burn_rate_tokens_per_hour=500.0,
                estimated_hours_remaining=0.0,
                triggered_at=1_700_000_200.0,
            )
            a3 = store.record_alert(
                "c-a",
                alert_level="critical",
                utilization_pct=1.0,
                daily_used=1000,
                daily_cap=1000,
                threshold_pct=0.8,
                burn_rate_tokens_per_hour=600.0,
                estimated_hours_remaining=0.0,
                triggered_at=1_700_000_300.0,
            )

            assert a1["id"] < a2["id"] < a3["id"]

            all_alerts = store.list_alerts()
            assert [a["id"] for a in all_alerts] == [a3["id"], a2["id"], a1["id"]]

            ca_alerts = store.list_alerts(contract_id="c-a")
            assert [a["id"] for a in ca_alerts] == [a3["id"], a1["id"]]

            limited = store.list_alerts(limit=2)
            assert [a["id"] for a in limited] == [a3["id"], a2["id"]]

            with pytest.raises(ValueError):
                store.list_alerts(limit=0)
            with pytest.raises(ValueError):
                store.record_alert(
                    "c-a",
                    alert_level="invalid",
                    utilization_pct=0.5,
                    daily_used=50,
                    daily_cap=100,
                )

    def test_in_memory_store(self):
        with SQLiteBudgetAlertStore(":memory:") as store:
            store.set_threshold("mem-1", 0.7)
            assert store.get_threshold("mem-1") == pytest.approx(0.7)
            rec = store.record_alert(
                "mem-1",
                alert_level="warning",
                utilization_pct=0.75,
                daily_used=750,
                daily_cap=1000,
                threshold_pct=0.7,
            )
            listed = store.list_alerts("mem-1")
            assert len(listed) == 1
            assert listed[0]["id"] == rec["id"]


class TestEvaluateBudgetForecast:
    def test_ok_warning_and_critical_levels_and_alert_logging(self, tmp_path):
        db_file = tmp_path / "forecast.db"
        # Exact UTC midnight: 1_700_006_400 == 2023-11-15T00:00:00Z
        day_start = 1_700_006_400.0

        with (
            SQLiteBudgetPolicyEngine(db_file) as engine,
            SQLiteBudgetAlertStore(db_file) as alert_store,
        ):
            engine.set_policy("c-1", daily_token_cap=10_000)

            # 50% utilization -> "ok", no alert logged
            engine.check_and_record("c-1", estimated_tokens=5_000, now_ts=day_start + 1_800.0)
            fc_ok = evaluate_budget_forecast(
                engine,
                "c-1",
                window_seconds=3600,
                now_ts=day_start + 3_600.0,
                alert_threshold_pct=0.8,
            )
            assert fc_ok["daily_used"] == 5_000
            assert fc_ok["daily_cap"] == 10_000
            assert fc_ok["utilization_pct"] == pytest.approx(0.5)
            assert fc_ok["burn_rate_tokens_per_hour"] == pytest.approx(5_000.0)
            assert fc_ok["estimated_hours_remaining"] == pytest.approx(1.0)
            assert fc_ok["alert_level"] == "ok"
            assert alert_store.list_alerts("c-1") == []

            # Add 3,000 tokens -> 80% utilization -> "warning", logs alert
            engine.check_and_record("c-1", estimated_tokens=3_000, now_ts=day_start + 3_700.0)
            fc_warn = evaluate_budget_forecast(
                engine,
                "c-1",
                window_seconds=3600,
                now_ts=day_start + 3_800.0,
                alert_threshold_pct=0.8,
            )
            assert fc_warn["daily_used"] == 8_000
            assert fc_warn["daily_cap"] == 10_000
            assert fc_warn["utilization_pct"] == pytest.approx(0.8)
            assert fc_warn["burn_rate_tokens_per_hour"] == pytest.approx(8_000.0)
            assert fc_warn["estimated_hours_remaining"] == pytest.approx(0.25)
            assert fc_warn["alert_level"] == "warning"

            alerts_after_warn = alert_store.list_alerts("c-1")
            assert len(alerts_after_warn) == 1
            assert alerts_after_warn[0]["alert_level"] == "warning"
            assert alerts_after_warn[0]["utilization_pct"] == pytest.approx(0.8)
            assert alerts_after_warn[0]["daily_used"] == 8_000

            # Add 2,000 tokens -> 100% utilization -> "critical", logs alert
            engine.check_and_record("c-1", estimated_tokens=2_000, now_ts=day_start + 3_900.0)
            fc_crit = evaluate_budget_forecast(
                engine,
                "c-1",
                window_seconds=3600,
                now_ts=day_start + 4_000.0,
                alert_threshold_pct=0.8,
            )
            assert fc_crit["daily_used"] == 10_000
            assert fc_crit["daily_cap"] == 10_000
            assert fc_crit["utilization_pct"] == pytest.approx(1.0)
            assert fc_crit["estimated_hours_remaining"] == pytest.approx(0.0)
            assert fc_crit["alert_level"] == "critical"

            alerts_after_crit = alert_store.list_alerts("c-1")
            assert len(alerts_after_crit) == 2
            assert alerts_after_crit[0]["alert_level"] == "critical"

    def test_burn_rate_math_with_custom_window_and_older_daily_usage(self, tmp_path):
        db_file = tmp_path / "burn_math.db"
        day_start = 1_700_006_400.0

        with SQLiteBudgetPolicyEngine(db_file) as engine:
            engine.set_policy("c-burn", daily_token_cap=20_000)

            # Morning burst: 4,000 tokens at +01:00 (outside later 30-min window, inside UTC day)
            engine.check_and_record("c-burn", estimated_tokens=4_000, now_ts=day_start + 3_600.0)
            # Recent 30-minute activity: 1,000 + 2,000 = 3,000 tokens in (day_start+9000, day_start+10800]
            engine.check_and_record("c-burn", estimated_tokens=1_000, now_ts=day_start + 9_600.0)
            engine.check_and_record("c-burn", estimated_tokens=2_000, now_ts=day_start + 10_500.0)

            # Evaluate at day_start + 10,800 (+03:00) over a 1800s (30-min) window:
            # window_tokens = 3,000 -> burn_rate = 3,000 * (3600 / 1800) = 6,000 tokens/hr
            # daily_used = 7,000, remaining = 13,000 -> hours_remaining = 13,000 / 6,000 = 2.166667
            fc = evaluate_budget_forecast(
                engine,
                "c-burn",
                window_seconds=1800,
                now_ts=day_start + 10_800.0,
            )
            assert fc["daily_used"] == 7_000
            assert fc["window_tokens"] == 3_000
            assert fc["burn_rate_tokens_per_hour"] == pytest.approx(6_000.0)
            assert fc["estimated_hours_remaining"] == pytest.approx(13_000.0 / 6_000.0, rel=1e-5)
            assert fc["utilization_pct"] == pytest.approx(0.35)
            assert fc["alert_level"] == "ok"

            # Later in the day with zero recent burn in the last hour -> estimated_hours_remaining is None
            fc_idle = evaluate_budget_forecast(
                engine,
                "c-burn",
                window_seconds=3600,
                now_ts=day_start + 20_000.0,
            )
            assert fc_idle["daily_used"] == 7_000
            assert fc_idle["burn_rate_tokens_per_hour"] == pytest.approx(0.0)
            assert fc_idle["estimated_hours_remaining"] is None
            assert fc_idle["alert_level"] == "ok"

    def test_unconfigured_daily_cap_and_policy_engine_alert_integration(self, tmp_path):
        db_file = tmp_path / "integrated.db"
        day_start = 1_700_006_400.0

        with (
            SQLiteBudgetAlertStore(db_file) as alert_store,
            SQLiteBudgetPolicyEngine(db_file, alert_store=alert_store) as engine,
        ):
            # Unconfigured daily cap -> ok, None hours remaining
            fc_uncapped = evaluate_budget_forecast(engine, "uncapped", now_ts=day_start + 60.0)
            assert fc_uncapped["daily_cap"] is None
            assert fc_uncapped["utilization_pct"] == pytest.approx(0.0)
            assert fc_uncapped["estimated_hours_remaining"] is None
            assert fc_uncapped["alert_level"] == "ok"

            # Policy engine integrated with alert_store auto-records soft & critical alerts
            engine.set_policy("c-auto", daily_token_cap=1_000)
            alert_store.set_threshold("c-auto", 0.75)

            d1 = engine.check_and_record("c-auto", estimated_tokens=500, now_ts=day_start + 10.0)
            assert d1.allowed is True
            assert alert_store.list_alerts("c-auto") == []

            d2 = engine.check_and_record("c-auto", estimated_tokens=300, now_ts=day_start + 20.0)
            assert d2.allowed is True
            alerts = alert_store.list_alerts("c-auto")
            assert len(alerts) == 1
            assert alerts[0]["alert_level"] == "warning"

            d3 = engine.check_and_record("c-auto", estimated_tokens=300, now_ts=day_start + 30.0)
            assert d3.allowed is False
            assert d3.reason == "daily_token_cap_exceeded"
            alerts = alert_store.list_alerts("c-auto")
            assert len(alerts) == 2
            assert alerts[0]["alert_level"] == "critical"


class TestForecastAndAlertsCLI:
    def test_cli_forecast_and_alerts_end_to_end(self, tmp_path, capsys):
        db_file = str(tmp_path / "cli_forecast.db")
        day_start = 1_700_006_400.0

        # Seed deterministic usage via policy engine on the same SQLite DB
        with SQLiteBudgetPolicyEngine(db_file) as engine:
            engine.set_policy("tenant-fc", daily_token_cap=10_000)
            engine.check_and_record("tenant-fc", estimated_tokens=4_000, now_ts=day_start + 1_000.0)
            engine.check_and_record("tenant-fc", estimated_tokens=4_500, now_ts=day_start + 2_800.0)

        # Run forecast CLI with --json -> 85% utilization => "warning" and alert recorded
        rc = cli_main(
            [
                "forecast",
                "--db",
                db_file,
                "--contract-id",
                "tenant-fc",
                "--window-seconds",
                "3600",
                "--threshold",
                "0.8",
                "--now-ts",
                str(day_start + 3_600.0),
                "--json",
            ]
        )
        assert rc == 0
        fc_out = json.loads(capsys.readouterr().out)
        assert fc_out["contract_id"] == "tenant-fc"
        assert fc_out["daily_used"] == 8_500
        assert fc_out["daily_cap"] == 10_000
        assert fc_out["utilization_pct"] == pytest.approx(0.85)
        assert fc_out["burn_rate_tokens_per_hour"] == pytest.approx(8_500.0)
        assert fc_out["estimated_hours_remaining"] == pytest.approx(1_500.0 / 8_500.0, rel=1e-5)
        assert fc_out["alert_level"] == "warning"

        # Query alerts CLI filtered by --contract-id with --json
        rc = cli_main(
            [
                "alerts",
                "--db",
                db_file,
                "--contract-id",
                "tenant-fc",
                "--json",
            ]
        )
        assert rc == 0
        alerts_out = json.loads(capsys.readouterr().out)
        assert alerts_out["contract_id"] == "tenant-fc"
        assert alerts_out["count"] == 1
        assert alerts_out["alerts"][0]["alert_level"] == "warning"
        assert alerts_out["alerts"][0]["daily_used"] == 8_500

        # Query all alerts without --contract-id and human-readable output
        rc = cli_main(["alerts", "--db", db_file])
        assert rc == 0
        human_out = capsys.readouterr().out
        assert "count: 1" in human_out
        assert "alert_level=warning" in human_out

        # Invalid threshold returns exit code 1 with structured JSON error
        rc = cli_main(
            [
                "forecast",
                "--db",
                db_file,
                "--contract-id",
                "tenant-fc",
                "--threshold",
                "1.5",
                "--json",
            ]
        )
        assert rc == 1
        err_out = json.loads(capsys.readouterr().err)
        assert err_out["error_type"] == "ValueError"
