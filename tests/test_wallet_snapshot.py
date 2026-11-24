"""Tests for Cycle 5 atomic wallet & budget state snapshot/restore (node-snapshot-v1)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from deployweave import (
    SQLiteBudgetAlertStore,
    SQLiteBudgetPolicyEngine,
    SQLiteReservationManager,
    evaluate_budget_forecast,
    export_wallet_snapshot,
    restore_wallet_snapshot,
)
from deployweave.cli import main as cli_main

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _seed_wallet_db(db_path: Path) -> None:
    with (
        SQLiteReservationManager(db_path) as mgr,
        SQLiteBudgetAlertStore(db_path) as alerts,
        SQLiteBudgetPolicyEngine(db_path, reservation_manager=mgr, alert_store=alerts) as policy,
    ):
        mgr.credit_wallet("tenant-alpha", input_tokens=100_000, output_tokens=50_000)
        rid = mgr.reserve_tokens("tenant-alpha", input_tokens=1_000, output_tokens=500)
        mgr.commit_reservation("tenant-alpha", rid, actual_input=800, actual_output=400)
        mgr.reserve_tokens(
            "tenant-alpha",
            input_tokens=500,
            output_tokens=200,
            reservation_id="res-pending-1",
        )
        policy.set_policy("tenant-alpha", max_rpm=30, max_tpm=50_000, daily_token_cap=2_000)
        policy.check_and_record("tenant-alpha", estimated_tokens=1_700, now_ts=1_700_000_000.0)
        evaluate_budget_forecast(
            policy,
            "tenant-alpha",
            now_ts=1_700_000_060.0,
            alert_threshold_pct=0.5,
            alert_store=alerts,
        )


def test_export_and_restore_preserves_reservations_policies_and_alerts(tmp_path: Path) -> None:
    src_db = tmp_path / "source_wallet.db"
    _seed_wallet_db(src_db)

    snap_dir = tmp_path / "snap"
    manifest = export_wallet_snapshot(src_db, snap_dir, node_id="pi-edge-01")

    # Verify node-snapshot-v1 contract fields
    assert manifest["schema"] == "node-snapshot-v1"
    assert manifest["node_id"] == "pi-edge-01"
    assert manifest["snapshot_id"].startswith("wallet-")
    assert manifest["created_at"].endswith("Z")
    assert _SHA256_RE.match(manifest["checksum_sha256"])
    assert len(manifest["components"]) == 1
    comp = manifest["components"][0]
    assert comp["name"] == "deploy_wallet"
    assert comp["kind"] == "sqlite_db"
    assert comp["relative_path"] == "deploy_wallet.db"
    assert _SHA256_RE.match(comp["sha256"])
    assert comp["size_bytes"] > 0
    assert comp["encrypted"] is False

    manifest_path = snap_dir / "snapshot-manifest.json"
    assert manifest_path.is_file()

    # Restore to a brand new target DB path
    target_db = tmp_path / "restored" / "wallet.db"
    summary = restore_wallet_snapshot(manifest_path, target_db)
    assert summary["restored"] is True
    assert summary["node_id"] == "pi-edge-01"
    assert target_db.is_file()

    # Verify reservations, policies, and alerts match source identically
    with (
        SQLiteReservationManager(target_db) as mgr,
        SQLiteBudgetAlertStore(target_db) as alerts,
        SQLiteBudgetPolicyEngine(target_db, reservation_manager=mgr, alert_store=alerts) as policy,
    ):
        bal = mgr.get_balance("tenant-alpha")
        assert bal["input"]["remaining"] == 100_000 - 800 - 500
        assert bal["output"]["remaining"] == 50_000 - 400 - 200
        assert bal["active_reservations"] == 1

        usage = policy.get_usage_summary("tenant-alpha", now_ts=1_700_000_060.0)
        assert usage["max_rpm"] == 30
        assert usage["daily_token_cap"] == 2_000
        assert usage["daily_tokens_used"] == 1_700

        recorded_alerts = alerts.list_alerts(contract_id="tenant-alpha")
        assert len(recorded_alerts) >= 1


def test_restore_rejects_tampered_db_and_preserves_existing_target(tmp_path: Path) -> None:
    src_db = tmp_path / "source.db"
    _seed_wallet_db(src_db)
    snap_dir = tmp_path / "snap"
    export_wallet_snapshot(src_db, snap_dir, node_id="pi-01")

    target_db = tmp_path / "existing_target.db"
    target_db.write_bytes(b"ORIGINAL_TARGET_CONTENTS")

    # Tamper with backed-up SQLite file
    backup_file = snap_dir / "deploy_wallet.db"
    backup_file.write_bytes(backup_file.read_bytes() + b"CORRUPT")

    with pytest.raises(ValueError, match="sha256 mismatch"):
        restore_wallet_snapshot(snap_dir / "snapshot-manifest.json", target_db)

    # Target DB must remain completely untouched
    assert target_db.read_bytes() == b"ORIGINAL_TARGET_CONTENTS"


def test_restore_rejects_tampered_manifest_checksum(tmp_path: Path) -> None:
    src_db = tmp_path / "source.db"
    _seed_wallet_db(src_db)
    snap_dir = tmp_path / "snap"
    export_wallet_snapshot(src_db, snap_dir, node_id="pi-01")

    manifest_path = snap_dir / "snapshot-manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["checksum_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    target_db = tmp_path / "target.db"
    with pytest.raises(ValueError, match="checksum_sha256 mismatch"):
        restore_wallet_snapshot(manifest_path, target_db)
    assert not target_db.exists()


def test_cli_snapshot_and_restore(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src_db = tmp_path / "cli_src.db"
    _seed_wallet_db(src_db)
    snap_dir = tmp_path / "cli_snap"
    target_db = tmp_path / "cli_restored.db"

    rc_snap = cli_main(
        [
            "snapshot",
            "--db",
            str(src_db),
            "--out",
            str(snap_dir),
            "--node-id",
            "pi-cli-01",
            "--json",
        ]
    )
    assert rc_snap == 0
    snap_out = json.loads(capsys.readouterr().out)
    assert snap_out["schema"] == "node-snapshot-v1"
    assert snap_out["node_id"] == "pi-cli-01"

    rc_restore = cli_main(
        [
            "restore",
            "--manifest",
            str(snap_dir / "snapshot-manifest.json"),
            "--target-db",
            str(target_db),
            "--json",
        ]
    )
    assert rc_restore == 0
    restore_out = json.loads(capsys.readouterr().out)
    assert restore_out["restored"] is True
    assert target_db.is_file()
