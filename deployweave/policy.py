"""Local-first SQLite WAL budget policy and rate-limit engine.

Enforces per-contract rolling-window requests-per-minute (RPM), tokens-per-minute
(TPM), and UTC daily token spend caps atomically using explicit ``BEGIN IMMEDIATE``
transactions.
"""
from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deployweave.alerts import SQLiteBudgetAlertStore
    from sqlite_reservation_manager import SQLiteReservationManager

DEFAULT_DB_PATH = "deployweave_wallets.db"
DEFAULT_BUSY_TIMEOUT_MS = 10_000
ROLLING_WINDOW_SECONDS = 60.0
DAILY_WINDOW_SECONDS = 86_400.0

_POLICY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS budget_policies (
    contract_id     TEXT    PRIMARY KEY,
    max_rpm         INTEGER,
    max_tpm         INTEGER,
    daily_token_cap INTEGER,
    updated_at      REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_usage_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id     TEXT    NOT NULL,
    tokens          INTEGER NOT NULL,
    recorded_at     REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_policy_usage_contract_time
    ON policy_usage_events (contract_id, recorded_at);
"""


def _validate_contract_id(contract_id: str) -> str:
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("contract_id must be a non-empty string")
    return contract_id


def _validate_non_negative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")
    return value


def _validate_optional_limit(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    return _validate_non_negative_int(name, value)


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of an atomic policy check-and-record evaluation."""

    allowed: bool
    contract_id: str
    reason: str | None = None
    estimated_tokens: int = 0
    rpm_used: int = 0
    max_rpm: int | None = None
    tpm_used: int = 0
    max_tpm: int | None = None
    daily_tokens_used: int = 0
    daily_token_cap: int | None = None
    retry_after_seconds: float = 0.0
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SQLiteBudgetPolicyEngine:
    """Thread-safe SQLite WAL engine for per-contract RPM, TPM, and daily token caps.

    All policy mutations and check-and-record operations run inside explicit
    ``BEGIN IMMEDIATE`` transactions so concurrent callers cannot exceed limits.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        reservation_manager: SQLiteReservationManager | None = None,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        alert_store: SQLiteBudgetAlertStore | None = None,
    ) -> None:
        if db_path is not None:
            raw_path = str(db_path)
        elif reservation_manager is not None:
            raw_path = reservation_manager.db_path
        else:
            raw_path = os.environ.get("DEPLOYWEAVE_SQLITE_PATH", DEFAULT_DB_PATH)

        self._is_memory = raw_path == ":memory:"
        self.db_path = raw_path
        self.reservation_manager = reservation_manager
        self.alert_store = alert_store
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
            for stmt in _POLICY_SCHEMA_SQL.strip().split(";"):
                sql = stmt.strip()
                if sql:
                    conn.execute(sql)

    def close(self) -> None:
        """Close all open SQLite connections managed by this engine."""
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

    def __enter__(self) -> SQLiteBudgetPolicyEngine:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def set_policy(
        self,
        contract_id: str,
        *,
        max_rpm: int | None = None,
        max_tpm: int | None = None,
        daily_token_cap: int | None = None,
    ) -> dict[str, Any]:
        """Create or update rate-limit and daily token cap policies for a contract."""
        _validate_contract_id(contract_id)
        rpm = _validate_optional_limit("max_rpm", max_rpm)
        tpm = _validate_optional_limit("max_tpm", max_tpm)
        daily_cap = _validate_optional_limit("daily_token_cap", daily_token_cap)
        now = time.time()

        with self._immediate_tx() as conn:
            conn.execute(
                """
                INSERT INTO budget_policies (contract_id, max_rpm, max_tpm, daily_token_cap, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(contract_id) DO UPDATE SET
                    max_rpm         = excluded.max_rpm,
                    max_tpm         = excluded.max_tpm,
                    daily_token_cap = excluded.daily_token_cap,
                    updated_at      = excluded.updated_at
                """,
                (contract_id, rpm, tpm, daily_cap, now),
            )

        return {
            "contract_id": contract_id,
            "max_rpm": rpm,
            "max_tpm": tpm,
            "daily_token_cap": daily_cap,
            "updated_at": now,
        }

    def get_policy(self, contract_id: str) -> dict[str, Any]:
        """Return the configured policy for a contract (None values mean unlimited)."""
        _validate_contract_id(contract_id)
        conn = self._get_connection()
        if self._is_memory:
            with self._conn_lock:
                row = conn.execute(
                    "SELECT * FROM budget_policies WHERE contract_id = ?",
                    (contract_id,),
                ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM budget_policies WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()

        if row is None:
            return {
                "contract_id": contract_id,
                "max_rpm": None,
                "max_tpm": None,
                "daily_token_cap": None,
                "updated_at": None,
            }
        return {
            "contract_id": row["contract_id"],
            "max_rpm": row["max_rpm"],
            "max_tpm": row["max_tpm"],
            "daily_token_cap": row["daily_token_cap"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _compute_window_usage(
        conn: sqlite3.Connection,
        contract_id: str,
        now: float,
    ) -> tuple[int, int, int, float | None, float]:
        """Return (rpm_used, tpm_used, daily_tokens_used, oldest_minute_ts, day_end)."""
        minute_cutoff = now - ROLLING_WINDOW_SECONDS
        day_start = math.floor(now / DAILY_WINDOW_SECONDS) * DAILY_WINDOW_SECONDS
        day_end = day_start + DAILY_WINDOW_SECONDS

        minute_row = conn.execute(
            """
            SELECT COUNT(*) AS req_count,
                   COALESCE(SUM(tokens), 0) AS token_sum,
                   MIN(recorded_at) AS oldest_ts
            FROM policy_usage_events
            WHERE contract_id = ? AND recorded_at > ? AND recorded_at <= ?
            """,
            (contract_id, minute_cutoff, now),
        ).fetchone()

        daily_row = conn.execute(
            """
            SELECT COALESCE(SUM(tokens), 0) AS token_sum
            FROM policy_usage_events
            WHERE contract_id = ? AND recorded_at >= ? AND recorded_at <= ?
            """,
            (contract_id, day_start, now),
        ).fetchone()

        rpm_used = int(minute_row["req_count"]) if minute_row is not None else 0
        tpm_used = int(minute_row["token_sum"]) if minute_row is not None else 0
        oldest_ts = float(minute_row["oldest_ts"]) if (minute_row is not None and minute_row["oldest_ts"] is not None) else None
        daily_tokens_used = int(daily_row["token_sum"]) if daily_row is not None else 0
        return rpm_used, tpm_used, daily_tokens_used, oldest_ts, day_end

    def check_and_record(
        self,
        contract_id: str,
        *,
        estimated_tokens: int,
        now_ts: float | None = None,
    ) -> PolicyDecision:
        """Atomically enforce RPM, TPM, and daily token cap, recording usage if allowed."""
        _validate_contract_id(contract_id)
        tokens = _validate_non_negative_int("estimated_tokens", estimated_tokens)
        now = time.time() if now_ts is None else float(now_ts)

        with self._immediate_tx() as conn:
            policy_row = conn.execute(
                "SELECT max_rpm, max_tpm, daily_token_cap FROM budget_policies WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()

            max_rpm = int(policy_row["max_rpm"]) if (policy_row is not None and policy_row["max_rpm"] is not None) else None
            max_tpm = int(policy_row["max_tpm"]) if (policy_row is not None and policy_row["max_tpm"] is not None) else None
            daily_cap = (
                int(policy_row["daily_token_cap"])
                if (policy_row is not None and policy_row["daily_token_cap"] is not None)
                else None
            )

            rpm_used, tpm_used, daily_used, oldest_minute_ts, day_end = self._compute_window_usage(
                conn, contract_id, now
            )

            if max_rpm is not None and rpm_used + 1 > max_rpm:
                retry_after = (
                    max(0.0, (oldest_minute_ts + ROLLING_WINDOW_SECONDS) - now)
                    if oldest_minute_ts is not None
                    else ROLLING_WINDOW_SECONDS
                )
                return PolicyDecision(
                    allowed=False,
                    contract_id=contract_id,
                    reason="max_rpm_exceeded",
                    estimated_tokens=tokens,
                    rpm_used=rpm_used,
                    max_rpm=max_rpm,
                    tpm_used=tpm_used,
                    max_tpm=max_tpm,
                    daily_tokens_used=daily_used,
                    daily_token_cap=daily_cap,
                    retry_after_seconds=round(retry_after, 6),
                    timestamp=now,
                )

            if max_tpm is not None and tpm_used + tokens > max_tpm:
                retry_after = (
                    max(0.0, (oldest_minute_ts + ROLLING_WINDOW_SECONDS) - now)
                    if oldest_minute_ts is not None
                    else ROLLING_WINDOW_SECONDS
                )
                return PolicyDecision(
                    allowed=False,
                    contract_id=contract_id,
                    reason="max_tpm_exceeded",
                    estimated_tokens=tokens,
                    rpm_used=rpm_used,
                    max_rpm=max_rpm,
                    tpm_used=tpm_used,
                    max_tpm=max_tpm,
                    daily_tokens_used=daily_used,
                    daily_token_cap=daily_cap,
                    retry_after_seconds=round(retry_after, 6),
                    timestamp=now,
                )

            if daily_cap is not None and daily_used + tokens > daily_cap:
                retry_after = max(0.0, day_end - now)
                decision = PolicyDecision(
                    allowed=False,
                    contract_id=contract_id,
                    reason="daily_token_cap_exceeded",
                    estimated_tokens=tokens,
                    rpm_used=rpm_used,
                    max_rpm=max_rpm,
                    tpm_used=tpm_used,
                    max_tpm=max_tpm,
                    daily_tokens_used=daily_used,
                    daily_token_cap=daily_cap,
                    retry_after_seconds=round(retry_after, 6),
                    timestamp=now,
                )
            else:
                conn.execute(
                    """
                    INSERT INTO policy_usage_events (contract_id, tokens, recorded_at)
                    VALUES (?, ?, ?)
                    """,
                    (contract_id, tokens, now),
                )
                decision = PolicyDecision(
                    allowed=True,
                    contract_id=contract_id,
                    reason=None,
                    estimated_tokens=tokens,
                    rpm_used=rpm_used + 1,
                    max_rpm=max_rpm,
                    tpm_used=tpm_used + tokens,
                    max_tpm=max_tpm,
                    daily_tokens_used=daily_used + tokens,
                    daily_token_cap=daily_cap,
                    retry_after_seconds=0.0,
                    timestamp=now,
                )

        if self.alert_store is not None and decision.daily_token_cap is not None:
            cap = decision.daily_token_cap
            used = decision.daily_tokens_used
            threshold = self.alert_store.get_threshold(contract_id)
            if decision.reason == "daily_token_cap_exceeded" or cap == 0 or used >= cap:
                util = 1.0 if cap == 0 else round(used / cap, 6)
                self.alert_store.record_alert(
                    contract_id,
                    alert_level="critical",
                    utilization_pct=util,
                    daily_used=used,
                    daily_cap=cap,
                    threshold_pct=threshold,
                    estimated_hours_remaining=0.0,
                    triggered_at=now,
                )
            elif cap > 0 and (used / cap) >= threshold:
                self.alert_store.record_alert(
                    contract_id,
                    alert_level="warning",
                    utilization_pct=round(used / cap, 6),
                    daily_used=used,
                    daily_cap=cap,
                    threshold_pct=threshold,
                    triggered_at=now,
                )

        return decision

    def get_window_tokens(
        self,
        contract_id: str,
        *,
        window_seconds: int = 3600,
        now_ts: float | None = None,
    ) -> int:
        """Return total tokens recorded for a contract within ``(now - window_seconds, now]``."""
        _validate_contract_id(contract_id)
        if isinstance(window_seconds, bool) or not isinstance(window_seconds, int):
            raise TypeError(f"window_seconds must be an integer, got {type(window_seconds).__name__}")
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be positive, got {window_seconds}")
        now = time.time() if now_ts is None else float(now_ts)
        cutoff = now - float(window_seconds)

        def _query(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(tokens), 0) AS token_sum
                FROM policy_usage_events
                WHERE contract_id = ? AND recorded_at > ? AND recorded_at <= ?
                """,
                (contract_id, cutoff, now),
            ).fetchone()
            return int(row["token_sum"]) if row is not None else 0

        conn = self._get_connection()
        if self._is_memory:
            with self._conn_lock:
                return _query(conn)
        return _query(conn)

    def get_usage_summary(
        self,
        contract_id: str,
        *,
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        """Return rolling-minute, UTC daily, and lifetime usage alongside configured limits."""
        _validate_contract_id(contract_id)
        now = time.time() if now_ts is None else float(now_ts)

        def _query(conn: sqlite3.Connection) -> dict[str, Any]:
            policy_row = conn.execute(
                "SELECT max_rpm, max_tpm, daily_token_cap FROM budget_policies WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
            max_rpm = int(policy_row["max_rpm"]) if (policy_row is not None and policy_row["max_rpm"] is not None) else None
            max_tpm = int(policy_row["max_tpm"]) if (policy_row is not None and policy_row["max_tpm"] is not None) else None
            daily_cap = (
                int(policy_row["daily_token_cap"])
                if (policy_row is not None and policy_row["daily_token_cap"] is not None)
                else None
            )

            rpm_used, tpm_used, daily_used, _, _ = self._compute_window_usage(conn, contract_id, now)

            totals_row = conn.execute(
                """
                SELECT COUNT(*) AS total_requests,
                       COALESCE(SUM(tokens), 0) AS total_tokens
                FROM policy_usage_events
                WHERE contract_id = ?
                """,
                (contract_id,),
            ).fetchone()
            total_requests = int(totals_row["total_requests"]) if totals_row is not None else 0
            total_tokens = int(totals_row["total_tokens"]) if totals_row is not None else 0

            return {
                "contract_id": contract_id,
                "timestamp": now,
                "rpm_used": rpm_used,
                "max_rpm": max_rpm,
                "rpm_remaining": None if max_rpm is None else max(0, max_rpm - rpm_used),
                "tpm_used": tpm_used,
                "max_tpm": max_tpm,
                "tpm_remaining": None if max_tpm is None else max(0, max_tpm - tpm_used),
                "daily_tokens_used": daily_used,
                "daily_token_cap": daily_cap,
                "daily_tokens_remaining": None if daily_cap is None else max(0, daily_cap - daily_used),
                "total_requests": total_requests,
                "total_tokens": total_tokens,
            }

        conn = self._get_connection()
        if self._is_memory:
            with self._conn_lock:
                summary = _query(conn)
        else:
            summary = _query(conn)

        if self.reservation_manager is not None:
            try:
                summary["wallet"] = self.reservation_manager.get_balance(contract_id)
            except KeyError:
                summary["wallet"] = None

        return summary
