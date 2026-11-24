"""Atomic SQLite wallet & budget state snapshot/restore conforming to ``node-snapshot-v1``."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

NODE_SNAPSHOT_SCHEMA = "node-snapshot-v1"
WALLET_COMPONENT_NAME = "deploy_wallet"
WALLET_BACKUP_FILENAME = "deploy_wallet.db"
MANIFEST_FILENAME = "snapshot-manifest.json"
WEAVE_VERSION = "0.2.0"


def _sha256_file(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    total = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
    return hasher.hexdigest(), total


def _components_checksum(components: list[dict[str, Any]]) -> str:
    canonical = json.dumps(components, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def export_wallet_snapshot(
    db_path: str | Path,
    output_dir: str | Path,
    *,
    node_id: str,
) -> dict[str, Any]:
    """Create a WAL-safe SQLite wallet backup and ``node-snapshot-v1`` manifest."""
    if not isinstance(node_id, str) or not node_id.strip():
        raise ValueError("node_id must be a non-empty string")

    src_path = Path(db_path)
    if not src_path.is_file():
        raise FileNotFoundError(f"wallet SQLite database not found: {src_path}")

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    backup_path = out_dir / WALLET_BACKUP_FILENAME

    # Write backup via SQLite online backup API to a temp file, then atomically replace.
    fd, tmp_name = tempfile.mkstemp(prefix=".wallet-snap-", suffix=".db", dir=str(out_dir))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        src_conn = sqlite3.connect(str(src_path))
        dst_conn = sqlite3.connect(str(tmp_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
        os.replace(tmp_path, backup_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    digest, size_bytes = _sha256_file(backup_path)
    components = [
        {
            "name": WALLET_COMPONENT_NAME,
            "kind": "sqlite_db",
            "relative_path": WALLET_BACKUP_FILENAME,
            "sha256": digest,
            "size_bytes": size_bytes,
            "encrypted": False,
        }
    ]
    manifest: dict[str, Any] = {
        "schema": NODE_SNAPSHOT_SCHEMA,
        "snapshot_id": f"wallet-{uuid.uuid4().hex[:12]}",
        "node_id": node_id,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "weave_version": WEAVE_VERSION,
        "components": components,
        "checksum_sha256": _components_checksum(components),
    }

    manifest_path = out_dir / MANIFEST_FILENAME
    mfd, mtmp = tempfile.mkstemp(prefix=".manifest-", suffix=".json", dir=str(out_dir))
    try:
        with os.fdopen(mfd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        os.replace(mtmp, manifest_path)
    except Exception:
        Path(mtmp).unlink(missing_ok=True)
        raise

    return manifest


def restore_wallet_snapshot(
    manifest_path: str | Path,
    target_db_path: str | Path,
) -> dict[str, Any]:
    """Verify ``node-snapshot-v1`` checksums fail-closed and atomically restore the wallet DB."""
    mpath = Path(manifest_path)
    if not mpath.is_file():
        raise FileNotFoundError(f"snapshot manifest not found: {mpath}")

    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid snapshot manifest JSON: {exc}") from exc

    if not isinstance(manifest, dict):
        raise ValueError("snapshot manifest must be a JSON object")
    if manifest.get("schema") != NODE_SNAPSHOT_SCHEMA:
        raise ValueError(
            f"unsupported snapshot schema: expected {NODE_SNAPSHOT_SCHEMA!r}, "
            f"got {manifest.get('schema')!r}"
        )

    components = manifest.get("components")
    if not isinstance(components, list) or not components:
        raise ValueError("snapshot manifest must contain a non-empty components list")

    expected_checksum = manifest.get("checksum_sha256")
    computed_checksum = _components_checksum(components)
    if not isinstance(expected_checksum, str) or not hmac.compare_digest(
        expected_checksum, computed_checksum
    ):
        raise ValueError("snapshot manifest checksum_sha256 mismatch")

    wallet_comp = next(
        (
            c
            for c in components
            if isinstance(c, dict)
            and c.get("kind") == "sqlite_db"
            and c.get("name") == WALLET_COMPONENT_NAME
        ),
        None,
    )
    if wallet_comp is None:
        wallet_comp = next(
            (c for c in components if isinstance(c, dict) and c.get("kind") == "sqlite_db"),
            None,
        )
    if wallet_comp is None:
        raise ValueError("snapshot manifest does not contain a sqlite_db component")

    rel_path = wallet_comp.get("relative_path")
    expected_sha256 = wallet_comp.get("sha256")
    expected_size = wallet_comp.get("size_bytes")
    if not isinstance(rel_path, str) or not rel_path:
        raise ValueError("wallet component missing relative_path")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError("wallet component missing valid sha256")

    base_dir = mpath.parent.resolve()
    comp_path = (base_dir / rel_path).resolve()
    try:
        comp_path.relative_to(base_dir)
    except ValueError as exc:
        raise ValueError(f"unsafe component relative_path escapes snapshot directory: {rel_path}") from exc

    if not comp_path.is_file():
        raise ValueError(f"snapshot component file missing: {comp_path}")

    actual_sha256, actual_size = _sha256_file(comp_path)
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise ValueError("wallet component sha256 mismatch (snapshot tampered or corrupted)")
    if isinstance(expected_size, int) and not isinstance(expected_size, bool) and actual_size != expected_size:
        raise ValueError(
            f"wallet component size_bytes mismatch: expected {expected_size}, got {actual_size}"
        )

    # Verify SQLite file integrity before touching target_db_path
    try:
        check_conn = sqlite3.connect(f"file:{comp_path}?mode=ro", uri=True)
        try:
            row = check_conn.execute("PRAGMA integrity_check").fetchone()
            if row is None or str(row[0]).lower() != "ok":
                raise ValueError(f"SQLite integrity_check failed: {row}")
        finally:
            check_conn.close()
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"snapshot component is not a valid SQLite database: {exc}") from exc

    target_path = Path(target_db_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tfd, tmp_target = tempfile.mkstemp(
        prefix=".wallet-restore-", suffix=".db", dir=str(target_path.parent)
    )
    os.close(tfd)
    tmp_target_path = Path(tmp_target)
    try:
        shutil.copy2(comp_path, tmp_target_path)
        os.replace(tmp_target_path, target_path)
        # Clean up any stale WAL/SHM sidecar files from the previous database instance
        for suffix in ("-wal", "-shm"):
            Path(f"{target_path}{suffix}").unlink(missing_ok=True)
    except Exception:
        tmp_target_path.unlink(missing_ok=True)
        raise

    return {
        "restored": True,
        "snapshot_id": str(manifest.get("snapshot_id", "")),
        "node_id": str(manifest.get("node_id", "")),
        "target_db_path": str(target_path),
        "sha256": actual_sha256,
        "size_bytes": actual_size,
    }


__all__ = [
    "MANIFEST_FILENAME",
    "NODE_SNAPSHOT_SCHEMA",
    "WALLET_BACKUP_FILENAME",
    "WALLET_COMPONENT_NAME",
    "export_wallet_snapshot",
    "restore_wallet_snapshot",
]
