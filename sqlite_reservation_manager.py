"""Standalone SQLite WAL token-wallet reservation manager.

Mirrors the atomic contract of ``reservation_manager.py`` (DynamoDB) for local-first
and single-node deployments using SQLite Write-Ahead Logging (WAL) and explicit
``BEGIN IMMEDIATE`` transactions.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "deployweave_wallets.db"
DEFAULT_BUSY_TIMEOUT_MS = 10_000
STALE_THRESHOLD_SECONDS = 300.0

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS wallets (
    contract_id       TEXT PRIMARY KEY,
    status            TEXT    NOT NULL DEFAULT 'active',
    input_allocated   INTEGER NOT NULL DEFAULT 0,
    input_consumed    INTEGER NOT NULL DEFAULT 0,
    input_reserved    INTEGER NOT NULL DEFAULT 0,
    input_remaining   INTEGER NOT NULL DEFAULT 0,
    input_cached      INTEGER NOT NULL DEFAULT 0,
    output_allocated  INTEGER NOT NULL DEFAULT 0,
    output_consumed   INTEGER NOT NULL DEFAULT 0,
    output_reserved   INTEGER NOT NULL DEFAULT 0,
    output_remaining  INTEGER NOT NULL DEFAULT 0,
    created_at        REAL    NOT NULL,
    updated_at        REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    contract_id       TEXT    NOT NULL,
    reservation_id    TEXT    NOT NULL,
    input_reserved    INTEGER NOT NULL,
    output_reserved   INTEGER NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'pending',
    created_at        REAL    NOT NULL,
    PRIMARY KEY (contract_id, reservation_id),
    FOREIGN KEY (contract_id) REFERENCES wallets(contract_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reservations_status_created
    ON reservations (status, created_at);
"""


class WalletError(Exception):
    """Base exception for token-wallet reservation errors."""


class ContractNotFoundError(WalletError, KeyError):
    """Raised when a referenced contract wallet does not exist."""


class ContractInactiveError(WalletError):
    """Raised when attempting to reserve tokens on a non-active contract."""


class InsufficientBalanceError(WalletError, ValueError):
    """Raised when a wallet has insufficient remaining tokens for a reservation."""


class DuplicateReservationError(WalletError, ValueError):
    """Raised when a reservation_id is already pending on the contract."""


class ReservationNotFoundError(WalletError, KeyError):
    """Raised when committing a reservation that does not exist or was already finalized."""


def _validate_non_negative(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")
    return value


class SQLiteReservationManager:
    """Thread-safe, local-first token-wallet reservation manager backed by SQLite WAL.

    Every mutating operation executes inside an explicit ``BEGIN IMMEDIATE``
    transaction so concurrent readers/writers observe atomic wallet invariants:
    ``allocated == consumed + reserved + remaining`` for both input and output tokens.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        raw_path = (
            str(db_path)
            if db_path is not None
            else os.environ.get("DEPLOYWEAVE_SQLITE_PATH", DEFAULT_DB_PATH)
        )
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
            for stmt in _SCHEMA_SQL.strip().split(";"):
                sql = stmt.strip()
                if sql:
                    conn.execute(sql)

    def close(self) -> None:
        """Close all open SQLite connections managed by this instance."""
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

    def __enter__(self) -> SQLiteReservationManager:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @staticmethod
    def _row_to_balance(row: sqlite3.Row, active_reservations: int) -> dict[str, Any]:
        return {
            "contract_id": row["contract_id"],
            "status": row["status"],
            "input": {
                "allocated": row["input_allocated"],
                "consumed": row["input_consumed"],
                "reserved": row["input_reserved"],
                "remaining": row["input_remaining"],
                "cached": row["input_cached"],
            },
            "output": {
                "allocated": row["output_allocated"],
                "consumed": row["output_consumed"],
                "reserved": row["output_reserved"],
                "remaining": row["output_remaining"],
            },
            "active_reservations": active_reservations,
        }

    def _fetch_balance_in_tx(self, conn: sqlite3.Connection, contract_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM wallets WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()
        if row is None:
            raise ContractNotFoundError(f"Contract '{contract_id}' not found")
        count_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM reservations WHERE contract_id = ? AND status = 'pending'",
            (contract_id,),
        ).fetchone()
        active_count = int(count_row["cnt"]) if count_row is not None else 0
        return self._row_to_balance(row, active_count)

    def credit_wallet(
        self,
        contract_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        *,
        status: str = "active",
    ) -> dict[str, Any]:
        """Create a contract wallet or atomically add token credits to an existing one."""
        _validate_non_negative("input_tokens", input_tokens)
        _validate_non_negative("output_tokens", output_tokens)
        now = time.time()

        with self._immediate_tx() as conn:
            existing = conn.execute(
                "SELECT contract_id FROM wallets WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO wallets (
                        contract_id, status,
                        input_allocated, input_consumed, input_reserved, input_remaining, input_cached,
                        output_allocated, output_consumed, output_reserved, output_remaining,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 0, 0, ?, 0, ?, 0, 0, ?, ?, ?)
                    """,
                    (
                        contract_id,
                        status,
                        input_tokens,
                        input_tokens,
                        output_tokens,
                        output_tokens,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE wallets
                    SET input_allocated  = input_allocated + ?,
                        input_remaining  = input_remaining + ?,
                        output_allocated = output_allocated + ?,
                        output_remaining = output_remaining + ?,
                        updated_at       = ?
                    WHERE contract_id = ?
                    """,
                    (input_tokens, input_tokens, output_tokens, output_tokens, now, contract_id),
                )
            return self._fetch_balance_in_tx(conn, contract_id)

    def set_status(self, contract_id: str, status: str) -> dict[str, Any]:
        """Update a contract wallet's status (e.g. 'active', 'suspended', 'closed')."""
        now = time.time()
        with self._immediate_tx() as conn:
            cur = conn.execute(
                "UPDATE wallets SET status = ?, updated_at = ? WHERE contract_id = ?",
                (status, now, contract_id),
            )
            if cur.rowcount == 0:
                raise ContractNotFoundError(f"Contract '{contract_id}' not found")
            return self._fetch_balance_in_tx(conn, contract_id)

    def get_balance(self, contract_id: str) -> dict[str, Any]:
        """Return the current token-wallet balance and active reservation count."""
        conn = self._get_connection()
        if self._is_memory:
            with self._conn_lock:
                return self._fetch_balance_in_tx(conn, contract_id)
        return self._fetch_balance_in_tx(conn, contract_id)

    def reserve_tokens(
        self,
        contract_id: str,
        input_tokens: int,
        output_tokens: int,
        reservation_id: str | None = None,
        *,
        created_at: float | None = None,
    ) -> str:
        """Atomically reserve input and output tokens on an active contract wallet.

        Raises:
            ContractNotFoundError: if ``contract_id`` does not exist.
            ContractInactiveError: if the contract status is not ``'active'``.
            InsufficientBalanceError: if remaining input or output tokens are insufficient.
            DuplicateReservationError: if ``reservation_id`` already exists on the contract.
        """
        _validate_non_negative("input_tokens", input_tokens)
        _validate_non_negative("output_tokens", output_tokens)
        rid = reservation_id or uuid.uuid4().hex
        now = time.time() if created_at is None else float(created_at)

        with self._immediate_tx() as conn:
            row = conn.execute(
                "SELECT status, input_remaining, output_remaining FROM wallets WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
            if row is None:
                raise ContractNotFoundError(f"Contract '{contract_id}' not found")
            if row["status"] != "active":
                raise ContractInactiveError(
                    f"Contract '{contract_id}' is not active (status={row['status']!r})"
                )
            if row["input_remaining"] < input_tokens or row["output_remaining"] < output_tokens:
                raise InsufficientBalanceError(
                    f"Insufficient balance on '{contract_id}': "
                    f"requested=({input_tokens}, {output_tokens}), "
                    f"remaining=({row['input_remaining']}, {row['output_remaining']})"
                )

            try:
                conn.execute(
                    """
                    INSERT INTO reservations (
                        contract_id, reservation_id, input_reserved, output_reserved, status, created_at
                    ) VALUES (?, ?, ?, ?, 'pending', ?)
                    """,
                    (contract_id, rid, input_tokens, output_tokens, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateReservationError(
                    f"Reservation '{rid}' already exists on contract '{contract_id}'"
                ) from exc

            conn.execute(
                """
                UPDATE wallets
                SET input_reserved   = input_reserved + ?,
                    input_remaining  = input_remaining - ?,
                    output_reserved  = output_reserved + ?,
                    output_remaining = output_remaining - ?,
                    updated_at       = ?
                WHERE contract_id = ?
                """,
                (input_tokens, input_tokens, output_tokens, output_tokens, now, contract_id),
            )

        return rid

    def commit_reservation(
        self,
        contract_id: str,
        reservation_id: str,
        actual_input: int,
        actual_output: int,
        cached_input: int = 0,
    ) -> dict[str, Any]:
        """Atomically finalize a reservation and settle actual token usage.

        When ``actual < reserved``, the unused delta is refunded to ``remaining``.
        When ``actual > reserved``, the exact overage ``(actual - reserved)`` is
        debited from ``remaining`` so ``allocated == consumed + reserved + remaining``
        holds unconditionally.
        """
        _validate_non_negative("actual_input", actual_input)
        _validate_non_negative("actual_output", actual_output)
        _validate_non_negative("cached_input", cached_input)
        now = time.time()

        with self._immediate_tx() as conn:
            res = conn.execute(
                """
                SELECT input_reserved, output_reserved
                FROM reservations
                WHERE contract_id = ? AND reservation_id = ? AND status = 'pending'
                """,
                (contract_id, reservation_id),
            ).fetchone()
            if res is None:
                raise ReservationNotFoundError(
                    f"Pending reservation '{reservation_id}' not found on contract '{contract_id}'"
                )

            reserved_input = int(res["input_reserved"])
            reserved_output = int(res["output_reserved"])

            # Signed delta: positive refunds unused reservation; negative debits overage from remaining.
            delta_input = reserved_input - actual_input
            delta_output = reserved_output - actual_output

            conn.execute(
                """
                UPDATE wallets
                SET input_consumed   = input_consumed + ?,
                    input_reserved   = input_reserved - ?,
                    input_remaining  = input_remaining + ?,
                    input_cached     = input_cached + ?,
                    output_consumed  = output_consumed + ?,
                    output_reserved  = output_reserved - ?,
                    output_remaining = output_remaining + ?,
                    updated_at       = ?
                WHERE contract_id = ?
                """,
                (
                    actual_input,
                    reserved_input,
                    delta_input,
                    cached_input,
                    actual_output,
                    reserved_output,
                    delta_output,
                    now,
                    contract_id,
                ),
            )
            conn.execute(
                "DELETE FROM reservations WHERE contract_id = ? AND reservation_id = ?",
                (contract_id, reservation_id),
            )
            balance = self._fetch_balance_in_tx(conn, contract_id)

        balance["reservation_id"] = reservation_id
        balance["overage_input"] = max(0, actual_input - reserved_input)
        balance["overage_output"] = max(0, actual_output - reserved_output)
        balance["refunded_input"] = max(0, delta_input)
        balance["refunded_output"] = max(0, delta_output)
        return balance

    # Alias matching reservation_manager.commit_tokens
    commit_tokens = commit_reservation

    def release_reservation(self, contract_id: str, reservation_id: str) -> bool:
        """Atomically release a pending reservation without deducting consumed tokens.

        Returns ``True`` if a pending reservation was released, or ``False`` if it
        was not found (idempotent).
        """
        now = time.time()
        with self._immediate_tx() as conn:
            res = conn.execute(
                """
                SELECT input_reserved, output_reserved
                FROM reservations
                WHERE contract_id = ? AND reservation_id = ? AND status = 'pending'
                """,
                (contract_id, reservation_id),
            ).fetchone()
            if res is None:
                logger.warning(
                    "Reservation %s not found on %s; skipping release",
                    reservation_id,
                    contract_id,
                )
                return False

            reserved_input = int(res["input_reserved"])
            reserved_output = int(res["output_reserved"])

            conn.execute(
                """
                UPDATE wallets
                SET input_reserved   = input_reserved - ?,
                    input_remaining  = input_remaining + ?,
                    output_reserved  = output_reserved - ?,
                    output_remaining = output_remaining + ?,
                    updated_at       = ?
                WHERE contract_id = ?
                """,
                (
                    reserved_input,
                    reserved_input,
                    reserved_output,
                    reserved_output,
                    now,
                    contract_id,
                ),
            )
            conn.execute(
                "DELETE FROM reservations WHERE contract_id = ? AND reservation_id = ?",
                (contract_id, reservation_id),
            )
        return True

    def expire_stale_reservations(
        self,
        max_age_seconds: float = STALE_THRESHOLD_SECONDS,
        *,
        now: float | None = None,
    ) -> int:
        """Atomically release all pending reservations older than ``max_age_seconds``.

        Refunds reserved input and output tokens back to each contract's remaining
        balance and returns the number of expired reservations.
        """
        if max_age_seconds < 0:
            raise ValueError(f"max_age_seconds must be non-negative, got {max_age_seconds}")

        current_time = time.time() if now is None else float(now)
        cutoff = current_time - float(max_age_seconds)

        with self._immediate_tx() as conn:
            stale_rows = conn.execute(
                """
                SELECT contract_id, reservation_id, input_reserved, output_reserved
                FROM reservations
                WHERE status = 'pending' AND created_at < ?
                """,
                (cutoff,),
            ).fetchall()

            if not stale_rows:
                return 0

            refunds_by_contract: dict[str, list[int]] = {}
            for row in stale_rows:
                cid = row["contract_id"]
                acc = refunds_by_contract.setdefault(cid, [0, 0])
                acc[0] += int(row["input_reserved"])
                acc[1] += int(row["output_reserved"])

            for cid, (in_refund, out_refund) in refunds_by_contract.items():
                conn.execute(
                    """
                    UPDATE wallets
                    SET input_reserved   = input_reserved - ?,
                        input_remaining  = input_remaining + ?,
                        output_reserved  = output_reserved - ?,
                        output_remaining = output_remaining + ?,
                        updated_at       = ?
                    WHERE contract_id = ?
                    """,
                    (in_refund, in_refund, out_refund, out_refund, current_time, cid),
                )

            conn.executemany(
                "DELETE FROM reservations WHERE contract_id = ? AND reservation_id = ?",
                [(row["contract_id"], row["reservation_id"]) for row in stale_rows],
            )
            return len(stale_rows)
