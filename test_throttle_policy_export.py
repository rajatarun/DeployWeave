"""Unit and CLI tests for DeployWeave auto-throttle policy exporter (agent-policy-v1)."""
from __future__ import annotations

import json
import math
from datetime import datetime

import pytest

import deployweave
from deployweave.alerts import SQLiteBudgetAlertStore
from deployweave.cli import main as cli_main
from deployweave.policy import SQLiteBudgetPolicyEngine
from deployweave.throttle import (
    AGENT_POLICY_SCHEMA,
    PREFER_LOCAL_MAX_COST_USD_PER_CALL,
    compute_throttle_recommendation,
)

# Fixed UTC midday timestamp so all events in tests fall within the same UTC day.
BASE_TS = 1_700_050_000.0  # 2023-11-15T12:06:40Z


def _record_tokens(
    engine: SQLiteBudgetPolicyEngine,
    contract_id: str,
    tokens: int,
    *,
    now_ts: float = BASE_TS,
) -> None:
    if tokens <= 0:
        return
    decision = engine.check_and_record(contract_id, estimated_tokens=tokens, now_ts=now_ts)
    assert decision.allowed is True


class TestComputeThrottleRecommendation:
    def test_package_export(self):
        assert deployweave.compute_throttle_recommendation is compute_throttle_recommendation

    def test_unlimited_daily_cap_returns_normal(self, tmp_path):
        db_file = tmp_path / "unlimited.db"
        with SQLiteBudgetPolicyEngine(db_file) as engine:
            engine.set_policy("c-unlimited", daily_token_cap=None)
            _record_tokens(engine, "c-unlimited", 5000, now_ts=BASE_TS)

            rec = compute_throttle_recommendation(engine, "c-unlimited", now_ts=BASE_TS)

        assert rec["routing_mode"] == "normal"
        assert rec["max_cost_usd_per_call"] is None
        assert rec["utilization_pct"] == pytest.approx(0.0)
        assert rec["agent_policy"]["schema"] == AGENT_POLICY_SCHEMA
        assert rec["agent_policy"]["routing_mode"] == "normal"
        assert rec["agent_policy"]["max_cost_usd_per_call"] is None

    def test_all_four_routing_mode_transitions(self, tmp_path):
        db_file = tmp_path / "transitions.db"
        with SQLiteBudgetPolicyEngine(db_file) as engine:
            engine.set_policy("c-trans", daily_token_cap=1000)

            # 1) <75% -> normal (500 / 1000 = 50%)
            _record_tokens(engine, "c-trans", 500, now_ts=BASE_TS - 300)
            rec_normal = compute_throttle_recommendation(engine, "c-trans", now_ts=BASE_TS)
            assert rec_normal["routing_mode"] == "normal"
            assert rec_normal["max_cost_usd_per_call"] is None
            assert rec_normal["utilization_pct"] == pytest.approx(0.50)

            # 2) 75% to <90% -> prefer_local (750 / 1000 = 75% exact boundary)
            _record_tokens(engine, "c-trans", 250, now_ts=BASE_TS - 200)
            rec_prefer = compute_throttle_recommendation(engine, "c-trans", now_ts=BASE_TS)
            assert rec_prefer["routing_mode"] == "prefer_local"
            assert rec_prefer["max_cost_usd_per_call"] == pytest.approx(PREFER_LOCAL_MAX_COST_USD_PER_CALL)
            assert rec_prefer["utilization_pct"] == pytest.approx(0.75)

            # 3) 90% to <100% -> local_only (900 / 1000 = 90% exact boundary)
            _record_tokens(engine, "c-trans", 150, now_ts=BASE_TS - 100)
            rec_local = compute_throttle_recommendation(engine, "c-trans", now_ts=BASE_TS)
            assert rec_local["routing_mode"] == "local_only"
            assert rec_local["max_cost_usd_per_call"] == pytest.approx(0.0)
            assert rec_local["utilization_pct"] == pytest.approx(0.90)

            # 4) >=100% -> halt (1000 / 1000 = 100% exact boundary)
            _record_tokens(engine, "c-trans", 100, now_ts=BASE_TS)
            rec_halt = compute_throttle_recommendation(engine, "c-trans", now_ts=BASE_TS)
            assert rec_halt["routing_mode"] == "halt"
            assert rec_halt["max_cost_usd_per_call"] == pytest.approx(0.0)
            assert rec_halt["utilization_pct"] == pytest.approx(1.0)

    def test_zero_daily_cap_returns_halt(self, tmp_path):
        db_file = tmp_path / "zero_cap.db"
        with SQLiteBudgetPolicyEngine(db_file) as engine:
            engine.set_policy("c-zero", daily_token_cap=0)
            rec = compute_throttle_recommendation(engine, "c-zero", now_ts=BASE_TS)

        assert rec["routing_mode"] == "halt"
        assert rec["max_cost_usd_per_call"] == pytest.approx(0.0)
        assert rec["agent_policy"]["routing_mode"] == "halt"

    def test_agent_policy_v1_payload_structure(self, tmp_path):
        db_file = tmp_path / "schema_check.db"
        with SQLiteBudgetPolicyEngine(db_file) as engine:
            engine.set_policy("tenant-alpha", daily_token_cap=1000)
            _record_tokens(engine, "tenant-alpha", 800, now_ts=BASE_TS - 60)

            rec = compute_throttle_recommendation(
                engine,
                "tenant-alpha",
                window_seconds=3600,
                now_ts=BASE_TS,
            )

        policy = rec["agent_policy"]
        assert policy["schema"] == "agent-policy-v1"
        assert isinstance(policy["policy_id"], str) and policy["policy_id"]
        assert policy["subject_id"] == "tenant-alpha"
        assert policy["enforcement_mode"] in ("enforce", "audit", "disabled")
        assert policy["routing_mode"] == "prefer_local"
        assert policy["max_cost_usd_per_call"] == pytest.approx(0.01)

        # generated_at must be a valid ISO-8601 UTC timestamp
        assert policy["generated_at"].endswith("Z")
        parsed_ts = datetime.fromisoformat(policy["generated_at"].replace("Z", "+00:00"))
        assert parsed_ts.timestamp() == pytest.approx(BASE_TS)

        # forecast sub-dict must mirror evaluate_budget_forecast output
        forecast = policy["forecast"]
        assert forecast["contract_id"] == "tenant-alpha"
        assert forecast["daily_used"] == 800
        assert forecast["daily_cap"] == 1000
        assert forecast["utilization_pct"] == pytest.approx(0.8)
        assert forecast["burn_rate_tokens_per_hour"] == pytest.approx(800.0)

    def test_custom_thresholds_and_read_only_alert_store(self, tmp_path):
        db_file = tmp_path / "custom.db"
        with (
            SQLiteBudgetAlertStore(db_file) as alert_store,
            SQLiteBudgetPolicyEngine(db_file, alert_store=alert_store) as engine,
        ):
            engine.set_policy("c-custom", daily_token_cap=1000)
            _record_tokens(engine, "c-custom", 600, now_ts=BASE_TS)

            # With warn=0.50, local_only=0.70, 60% -> prefer_local
            rec = compute_throttle_recommendation(
                engine,
                "c-custom",
                now_ts=BASE_TS,
                warn_threshold_pct=0.50,
                local_only_threshold_pct=0.70,
            )
            assert rec["routing_mode"] == "prefer_local"
            assert rec["max_cost_usd_per_call"] == pytest.approx(0.01)

            # Read-only policy export should not mutate the alert history table
            assert alert_store.list_alerts(contract_id="c-custom") == []

    def test_validation_errors(self, tmp_path):
        db_file = tmp_path / "validation.db"
        with SQLiteBudgetPolicyEngine(db_file) as engine:
            with pytest.raises(ValueError):
                compute_throttle_recommendation(engine, "", now_ts=BASE_TS)
            with pytest.raises(ValueError):
                compute_throttle_recommendation(
                    engine, "c-1", warn_threshold_pct=0.90, local_only_threshold_pct=0.90
                )
            with pytest.raises(ValueError):
                compute_throttle_recommendation(
                    engine, "c-1", warn_threshold_pct=0.95, local_only_threshold_pct=0.80
                )
            with pytest.raises(ValueError):
                compute_throttle_recommendation(engine, "c-1", warn_threshold_pct=0.0)
            with pytest.raises(ValueError):
                compute_throttle_recommendation(engine, "c-1", warn_threshold_pct=1.0)
            with pytest.raises(ValueError):
                compute_throttle_recommendation(engine, "c-1", local_only_threshold_pct=1.1)
            with pytest.raises(ValueError):
                compute_throttle_recommendation(engine, "c-1", warn_threshold_pct=math.nan)
            with pytest.raises(TypeError):
                compute_throttle_recommendation(engine, "c-1", warn_threshold_pct=True)  # type: ignore[arg-type]


class TestPolicyExportCLI:
    def test_cli_policy_export_json_and_file_output(self, tmp_path, capsys):
        db_file = tmp_path / "cli_export.db"
        out_file = tmp_path / "nested" / "throttle_policy.json"

        with SQLiteBudgetPolicyEngine(db_file) as engine:
            engine.set_policy("cli-contract", daily_token_cap=1000)
            _record_tokens(engine, "cli-contract", 920, now_ts=BASE_TS - 120)

        rc = cli_main(
            [
                "policy-export",
                "--db",
                str(db_file),
                "--contract-id",
                "cli-contract",
                "--now-ts",
                str(BASE_TS),
                "--output",
                str(out_file),
                "--json",
            ]
        )
        assert rc == 0

        stdout = capsys.readouterr().out
        payload = json.loads(stdout)
        assert payload["contract_id"] == "cli-contract"
        assert payload["routing_mode"] == "local_only"
        assert payload["max_cost_usd_per_call"] == pytest.approx(0.0)
        assert payload["output_path"] == str(out_file)
        assert payload["agent_policy"]["schema"] == "agent-policy-v1"

        assert out_file.exists()
        written = json.loads(out_file.read_text(encoding="utf-8"))
        assert written == payload["agent_policy"]
        assert written["schema"] == "agent-policy-v1"
        assert written["subject_id"] == "cli-contract"
        assert written["routing_mode"] == "local_only"
        assert written["max_cost_usd_per_call"] == pytest.approx(0.0)

    def test_cli_policy_export_human_readable_output(self, tmp_path, capsys):
        db_file = tmp_path / "cli_human.db"
        with SQLiteBudgetPolicyEngine(db_file) as engine:
            engine.set_policy("human-contract", daily_token_cap=1000)
            _record_tokens(engine, "human-contract", 200, now_ts=BASE_TS)

        rc = cli_main(
            [
                "policy-export",
                "--db",
                str(db_file),
                "--contract-id",
                "human-contract",
                "--now-ts",
                str(BASE_TS),
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "routing_mode: normal" in out
        assert "contract_id: human-contract" in out

    def test_cli_policy_export_invalid_thresholds_returns_error(self, tmp_path, capsys):
        db_file = tmp_path / "cli_err.db"
        rc = cli_main(
            [
                "policy-export",
                "--db",
                str(db_file),
                "--contract-id",
                "c-err",
                "--warn-threshold",
                "0.95",
                "--local-only-threshold",
                "0.80",
                "--json",
            ]
        )
        assert rc == 1
        err = json.loads(capsys.readouterr().err)
        assert err["error_type"] == "ValueError"
