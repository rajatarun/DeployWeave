"""Local-first SQLite WAL soft-alert store and daily budget exhaustion forecaster.

Works alongside :class:`deployweave.policy.SQLiteBudgetPolicyEngine` to store
per-contract soft-alert thresholds (default 80% daily cap), log triggered
``warning`` / ``critical`` alerts in SQLite WAL, and forecast daily budget
exhaustion from rolling burn rates.
"""
from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deployweave.policy import SQLiteBudgetPolicyEngine

DEFAULT_DB_PATH = "deployweave_wallets.db"
DEFAULT_BUSY_TIMEOUT_MS = 10_000
DEFAULT_ALERT_THRESHOLD_PCT = 0.8
VALID_ALERT_LEVELS = ("ok", "warning", "critical")

_ALERTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS budget_alert_thresholds (
    contract_id          TEXT PRIMARY KEY,
    alert_threshold_pct  REAL NOT NULL DEFAULT 0.8,
    updated_at           REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_alerts (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id               TEXT    NOT NULL,
    alert_level               TEXT    NOT NULL,
    utilization_pct           REAL    NOT NULL,
    daily_used                INTEGER NOT NULL,
    daily_cap                 INTEGER,
    threshold_pct             REAL    NOT NULL,
    burn_rate_tokens_per_hour REAL,
    estimated_hours_remaining REAL,
    message                   TEXT    NOT NULL,
    triggered_at              REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_budget_alerts_contract_time
    ON budget_alerts (contract_id, triggered_at DESC, id DESC);
"""


def _validate_contract_id(contract_id: str) -> str:
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("contract_id must be a non-empty string")
    return contract_id


def _validate_threshold_pct(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"alert_threshold_pct must be a number, got {type(value).__name__}")
    val = float(value)
    if math.isnan(val) or math.isinf(val) or val <= 0.0 or val > 1.0:
        raise ValueError(f"alert_threshold_pct must be in (0.0, 1.0], got {value}")
    return val


def _validate_positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _validate_alert_level(alert_level: str) -> str:
    if not isinstance(alert_level, str) or alert_level not in VALID_ALERT_LEVELS:
        raise ValueError(f"alert_level must be one of {VALID_ALERT_LEVELS}, got {alert_level!r}")
    return alert_level


class SQLiteBudgetAlertStore:
    """Thread-safe SQLite WAL store for per-contract alert thresholds and alert history."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        if db_path is not None:
            raw_path = str(db_path)
        else:
            raw_path = os.environ.get("DEPLOYWEAVE_SQLITE_PATH", DEFAULT_DB_PATH)

        self._is_memory = raw_path == ":memory:"
        self.db_path = raw_path
        self._busy_timeout_ms = max(100, int(busy_timeout_ms))

        if not self._is_memory:
            parent = Path(self.db_path).expanduser().resolve().parent
            parent.mkdir(parents=True, exist_ok=True)

        self._local = threading.local()
        self._conn_lock = threading.RLock()
        self._open_connections: list[sqlite3.Connection] = []
        self._memory_conn: sqlite3.Connection | None = None

        if self._is_memory:
            self._memory_conn = self._create_connection()
            self._open_connections.append(self._memory_conn)

        self._init_schema()

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=self._busy_timeout_ms / 1000.0,
            isolation_level=None,
            check_same_thread=not self._is_memory,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        if not self._is_memory:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _get_connection(self) -> sqlite3.Connection:
        if self._is_memory:
            assert self._memory_conn is not None
            return self._memory_conn

        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._create_connection()
            self._local.conn = conn
            with self._conn_lock:
                self._open_connections.append(conn)
        return conn

    @contextmanager
    def _immediate_tx(self):
        conn = self._get_connection()
        if self._is_memory:
            with self._conn_lock:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
        else:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def _init_schema(self) -> None:
        with self._immediate_tx() as conn:
            for stmt in _ALERTS_SCHEMA_SQL.strip().split(";"):
                sql = stmt.strip()
                if sql:
                    conn.execute(sql)

    def close(self) -> None:
        """Close all open SQLite connections managed by this store."""
        with self._conn_lock:
            for conn in self._open_connections:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._open_connections.clear()
            self._memory_conn = None
        if hasattr(self._local, "conn"):
            delattr(self._local, "conn")

    def __enter__(self) -> SQLiteBudgetAlertStore:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def set_threshold(
        self,
        contract_id: str,
        alert_threshold_pct: float = DEFAULT_ALERT_THRESHOLD_PCT,
    ) -> dict[str, Any]:
        """Set or update the soft-alert threshold percentage for a contract."""
        _validate_contract_id(contract_id)
        threshold = _validate_threshold_pct(alert_threshold_pct)
        now = time.time()

        with self._immediate_tx() as conn:
            conn.execute(
                """
                INSERT INTO budget_alert_thresholds (contract_id, alert_threshold_pct, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(contract_id) DO UPDATE SET
                    alert_threshold_pct = excluded.alert_threshold_pct,
                    updated_at          = excluded.updated_at
                """,
                (contract_id, threshold, now),
            )

        return {
            "contract_id": contract_id,
            "alert_threshold_pct": threshold,
            "updated_at": now,
        }

    def get_threshold(self, contract_id: str) -> float:
        """Return the configured soft-alert threshold for a contract (default 0.8)."""
        _validate_contract_id(contract_id)
        conn = self._get_connection()
        if self._is_memory:
            with self._conn_lock:
                row = conn.execute(
                    "SELECT alert_threshold_pct FROM budget_alert_thresholds WHERE contract_id = ?",
                    (contract_id,),
                ).fetchone()
        else:
            row = conn.execute(
                "SELECT alert_threshold_pct FROM budget_alert_thresholds WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()

        if row is None:
            return DEFAULT_ALERT_THRESHOLD_PCT
        return float(row["alert_threshold_pct"])

    def record_alert(
        self,
        contract_id: str,
        *,
        alert_level: str,
        utilization_pct: float,
        daily_used: int,
        daily_cap: int | None = None,
        threshold_pct: float = DEFAULT_ALERT_THRESHOLD_PCT,
        burn_rate_tokens_per_hour: float | None = None,
        estimated_hours_remaining: float | None = None,
        message: str | None = None,
        triggered_at: float | None = None,
    ) -> dict[str, Any]:
        """Persist a triggered budget alert in SQLite WAL and return the stored record."""
        _validate_contract_id(contract_id)
        level = _validate_alert_level(alert_level)
        threshold = _validate_threshold_pct(threshold_pct)
        ts = time.time() if triggered_at is None else float(triggered_at)
        util = float(utilization_pct)
        used = int(daily_used)
        cap = None if daily_cap is None else int(daily_cap)
        burn = None if burn_rate_tokens_per_hour is None else float(burn_rate_tokens_per_hour)
        hours_rem = None if estimated_hours_remaining is None else float(estimated_hours_remaining)

        if message is None:
            cap_str = str(cap) if cap is not None else "unlimited"
            msg = (
                f"Budget {level}: contract {contract_id} at {util * 100:.1f}% "
                f"of daily cap ({used}/{cap_str} tokens, threshold {threshold * 100:.1f}%)"
            )
        else:
            msg = str(message)

        with self._immediate_tx() as conn:
            cursor = conn.execute(
                """
                INSERT INTO budget_alerts (
                    contract_id,
                    alert_level,
                    utilization_pct,
                    daily_used,
                    daily_cap,
                    threshold_pct,
                    burn_rate_tokens_per_hour,
                    estimated_hours_remaining,
                    message,
                    triggered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (contract_id, level, util, used, cap, threshold, burn, hours_rem, msg, ts),
            )
            alert_id = int(cursor.lastrowid) if cursor.lastrowid is not None else 0

        return {
            "id": alert_id,
            "contract_id": contract_id,
            "alert_level": level,
            "utilization_pct": util,
            "daily_used": used,
            "daily_cap": cap,
            "threshold_pct": threshold,
            "burn_rate_tokens_per_hour": burn,
            "estimated_hours_remaining": hours_rem,
            "message": msg,
            "triggered_at": ts,
        }

    def list_alerts(
        self,
        contract_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List recorded budget alerts ordered newest-first."""
        lim = _validate_positive_int("limit", limit)
        if contract_id is not None:
            _validate_contract_id(contract_id)

        def _query(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            if contract_id is not None:
                return conn.execute(
                    """
                    SELECT id, contract_id, alert_level, utilization_pct,
                           daily_used, daily_cap, threshold_pct,
                           burn_rate_tokens_per_hour, estimated_hours_remaining,
                           message, triggered_at
                    FROM budget_alerts
                    WHERE contract_id = ?
                    ORDER BY triggered_at DESC, id DESC
                    LIMIT ?
                    """,
                    (contract_id, lim),
                ).fetchall()
            return conn.execute(
                """
                SELECT id, contract_id, alert_level, utilization_pct,
                       daily_used, daily_cap, threshold_pct,
                       burn_rate_tokens_per_hour, estimated_hours_remaining,
                       message, triggered_at
                FROM budget_alerts
                ORDER BY triggered_at DESC, id DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()

        conn = self._get_connection()
        if self._is_memory:
            with self._conn_lock:
                rows = _query(conn)
        else:
            rows = _query(conn)

        return [
            {
                "id": int(row["id"]),
                "contract_id": row["contract_id"],
                "alert_level": row["alert_level"],
                "utilization_pct": float(row["utilization_pct"]),
                "daily_used": int(row["daily_used"]),
                "daily_cap": None if row["daily_cap"] is None else int(row["daily_cap"]),
                "threshold_pct": float(row["threshold_pct"]),
                "burn_rate_tokens_per_hour": (
                    None if row["burn_rate_tokens_per_hour"] is None else float(row["burn_rate_tokens_per_hour"])
                ),
                "estimated_hours_remaining": (
                    None if row["estimated_hours_remaining"] is None else float(row["estimated_hours_remaining"])
                ),
                "message": row["message"],
                "triggered_at": float(row["triggered_at"]),
            }
            for row in rows
        ]


def evaluate_budget_forecast(
    policy_engine: SQLiteBudgetPolicyEngine,
    contract_id: str,
    *,
    window_seconds: int = 3600,
    now_ts: float | None = None,
    alert_threshold_pct: float = DEFAULT_ALERT_THRESHOLD_PCT,
    alert_store: SQLiteBudgetAlertStore | None = None,
    record_alert: bool = True,
) -> dict[str, Any]:
    """Compute daily budget utilization, burn rate, exhaustion forecast, and alert level.

    When ``alert_level`` is ``"warning"`` or ``"critical"`` and ``record_alert`` is
    ``True``, the triggered alert is automatically persisted to ``alert_store``
    (or ``policy_engine.alert_store``, or a ``SQLiteBudgetAlertStore`` backed by
    ``policy_engine.db_path`` for file-backed databases).
    """
    _validate_contract_id(contract_id)
    window_sec = _validate_positive_int("window_seconds", window_seconds)
    now = time.time() if now_ts is None else float(now_ts)

    resolved_store = alert_store if alert_store is not None else getattr(policy_engine, "alert_store", None)
    if alert_threshold_pct is None:
        threshold = (
            resolved_store.get_threshold(contract_id)
            if resolved_store is not None
            else DEFAULT_ALERT_THRESHOLD_PCT
        )
    else:
        threshold = _validate_threshold_pct(alert_threshold_pct)

    summary = policy_engine.get_usage_summary(contract_id, now_ts=now)
    daily_used = int(summary["daily_tokens_used"])
    daily_cap: int | None = summary["daily_token_cap"]

    window_tokens = policy_engine.get_window_tokens(
        contract_id,
        window_seconds=window_sec,
        now_ts=now,
    )
    burn_rate_tokens_per_hour = round(window_tokens * (3600.0 / window_sec), 6)

    if daily_cap is None:
        utilization_pct = 0.0
        estimated_hours_remaining: float | None = None
        alert_level = "ok"
    elif daily_cap == 0:
        utilization_pct = 1.0
        estimated_hours_remaining = 0.0
        alert_level = "critical"
    else:
        raw_utilization = daily_used / daily_cap
        utilization_pct = round(raw_utilization, 6)
        if daily_used >= daily_cap:
            alert_level = "critical"
            estimated_hours_remaining = 0.0
        else:
            alert_level = "warning" if raw_utilization >= threshold else "ok"
            remaining_tokens = daily_cap - daily_used
            if burn_rate_tokens_per_hour > 0.0:
                estimated_hours_remaining = round(remaining_tokens / burn_rate_tokens_per_hour, 6)
            else:
                estimated_hours_remaining = None

    if record_alert and alert_level in ("warning", "critical"):
        if resolved_store is not None:
            resolved_store.record_alert(
                contract_id,
                alert_level=alert_level,
                utilization_pct=utilization_pct,
                daily_used=daily_used,
                daily_cap=daily_cap,
                threshold_pct=threshold,
                burn_rate_tokens_per_hour=burn_rate_tokens_per_hour,
                estimated_hours_remaining=estimated_hours_remaining,
                triggered_at=now,
            )
        elif not getattr(policy_engine, "_is_memory", False):
            with SQLiteBudgetAlertStore(policy_engine.db_path) as store:
                store.record_alert(
                    contract_id,
                    alert_level=alert_level,
                    utilization_pct=utilization_pct,
                    daily_used=daily_used,
                    daily_cap=daily_cap,
                    threshold_pct=threshold,
                    burn_rate_tokens_per_hour=burn_rate_tokens_per_hour,
                    estimated_hours_remaining=estimated_hours_remaining,
                    triggered_at=now,
                )

    return {
        "contract_id": contract_id,
        "timestamp": now,
        "window_seconds": window_sec,
        "window_tokens": window_tokens,
        "daily_used": daily_used,
        "daily_cap": daily_cap,
        "daily_remaining": None if daily_cap is None else max(0, daily_cap - daily_used),
        "utilization_pct": utilization_pct,
        "alert_threshold_pct": threshold,
        "burn_rate_tokens_per_hour": burn_rate_tokens_per_hour,
        "estimated_hours_remaining": estimated_hours_remaining,
        "alert_level": alert_level,
    }
