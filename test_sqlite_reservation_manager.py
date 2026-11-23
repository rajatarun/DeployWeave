"""Unit and concurrency tests for SQLiteReservationManager."""
from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import deployweave
from sqlite_reservation_manager import (
    ContractInactiveError,
    ContractNotFoundError,
    DuplicateReservationError,
    InsufficientBalanceError,
    ReservationNotFoundError,
    SQLiteReservationManager,
)


def _assert_wallet_invariant(balance: dict) -> None:
    """Verify allocated == consumed + reserved + remaining for input and output."""
    inp = balance["input"]
    out = balance["output"]
    assert inp["allocated"] == inp["consumed"] + inp["reserved"] + inp["remaining"]
    assert out["allocated"] == out["consumed"] + out["reserved"] + out["remaining"]
    assert inp["reserved"] >= 0
    assert out["reserved"] >= 0


class TestSQLiteReservationManagerBasic:
    def test_package_exports_manager(self):
        assert deployweave.SQLiteReservationManager is SQLiteReservationManager

    def test_file_backed_uses_wal_mode(self, tmp_path):
        db_file = tmp_path / "wallets.db"
        with SQLiteReservationManager(db_file) as mgr:
            mgr.credit_wallet("c-wal", input_tokens=100, output_tokens=50)
            conn = sqlite3.connect(db_file)
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                conn.close()
        assert mode.lower() == "wal"

    def test_credit_wallet_create_and_top_up(self, tmp_path):
        with SQLiteReservationManager(tmp_path / "w.db") as mgr:
            b1 = mgr.credit_wallet("c-1", input_tokens=1000, output_tokens=500)
            assert b1["status"] == "active"
            assert b1["input"]["allocated"] == 1000
            assert b1["input"]["remaining"] == 1000
            assert b1["output"]["allocated"] == 500
            assert b1["output"]["remaining"] == 500
            assert b1["active_reservations"] == 0
            _assert_wallet_invariant(b1)

            b2 = mgr.credit_wallet("c-1", input_tokens=250, output_tokens=100)
            assert b2["input"]["allocated"] == 1250
            assert b2["input"]["remaining"] == 1250
            assert b2["output"]["allocated"] == 600
            assert b2["output"]["remaining"] == 600
            _assert_wallet_invariant(b2)

    def test_credit_wallet_rejects_negative_and_bool(self, tmp_path):
        with SQLiteReservationManager(tmp_path / "w.db") as mgr:
            with pytest.raises(ValueError):
                mgr.credit_wallet("c-1", input_tokens=-1, output_tokens=0)
            with pytest.raises(ValueError):
                mgr.credit_wallet("c-1", input_tokens=0, output_tokens=-5)
            with pytest.raises(TypeError):
                mgr.credit_wallet("c-1", input_tokens=True, output_tokens=0)  # type: ignore[arg-type]

    def test_get_balance_missing_contract_raises(self, tmp_path):
        with SQLiteReservationManager(tmp_path / "w.db") as mgr:
            with pytest.raises(ContractNotFoundError):
                mgr.get_balance("nonexistent")

    def test_atomic_reserve_and_commit_under_usage_refund(self, tmp_path):
        with SQLiteReservationManager(tmp_path / "w.db") as mgr:
            mgr.credit_wallet("c-1", input_tokens=1000, output_tokens=500)

            rid = mgr.reserve_tokens("c-1", input_tokens=300, output_tokens=150, reservation_id="r-1")
            assert rid == "r-1"
            mid = mgr.get_balance("c-1")
            assert mid["input"]["reserved"] == 300
            assert mid["input"]["remaining"] == 700
            assert mid["output"]["reserved"] == 150
            assert mid["output"]["remaining"] == 350
            assert mid["active_reservations"] == 1
            _assert_wallet_invariant(mid)

            receipt = mgr.commit_reservation(
                "c-1",
                "r-1",
                actual_input=180,
                actual_output=90,
                cached_input=40,
            )
            assert receipt["input"]["consumed"] == 180
            assert receipt["input"]["reserved"] == 0
            assert receipt["input"]["remaining"] == 820
            assert receipt["input"]["cached"] == 40
            assert receipt["output"]["consumed"] == 90
            assert receipt["output"]["reserved"] == 0
            assert receipt["output"]["remaining"] == 410
            assert receipt["active_reservations"] == 0
            assert receipt["refunded_input"] == 120
            assert receipt["refunded_output"] == 60
            assert receipt["overage_input"] == 0
            assert receipt["overage_output"] == 0
            _assert_wallet_invariant(receipt)

    def test_commit_overage_debit_when_actual_exceeds_reserved(self, tmp_path):
        with SQLiteReservationManager(tmp_path / "w.db") as mgr:
            mgr.credit_wallet("c-over", input_tokens=1000, output_tokens=500)
            mgr.reserve_tokens("c-over", input_tokens=200, output_tokens=100, reservation_id="r-over")

            receipt = mgr.commit_reservation(
                "c-over",
                "r-over",
                actual_input=350,
                actual_output=175,
            )
            assert receipt["input"]["consumed"] == 350
            assert receipt["input"]["reserved"] == 0
            assert receipt["input"]["remaining"] == 650
            assert receipt["output"]["consumed"] == 175
            assert receipt["output"]["reserved"] == 0
            assert receipt["output"]["remaining"] == 325
            assert receipt["overage_input"] == 150
            assert receipt["overage_output"] == 75
            assert receipt["refunded_input"] == 0
            assert receipt["refunded_output"] == 0
            _assert_wallet_invariant(receipt)

    def test_commit_overage_can_drive_remaining_negative_and_block_next_reserve(self, tmp_path):
        with SQLiteReservationManager(tmp_path / "w.db") as mgr:
            mgr.credit_wallet("c-neg", input_tokens=100, output_tokens=100)
            mgr.reserve_tokens("c-neg", input_tokens=80, output_tokens=80, reservation_id="r-1")

            receipt = mgr.commit_tokens("c-neg", "r-1", actual_input=130, actual_output=90)
            assert receipt["input"]["consumed"] == 130
            assert receipt["input"]["remaining"] == -30
            assert receipt["output"]["consumed"] == 90
            assert receipt["output"]["remaining"] == 10
            assert receipt["overage_input"] == 50
            _assert_wallet_invariant(receipt)

            with pytest.raises(InsufficientBalanceError):
                mgr.reserve_tokens("c-neg", input_tokens=1, output_tokens=1)

            # Re-crediting restores positive remaining and unblocks reservations
            topped = mgr.credit_wallet("c-neg", input_tokens=50, output_tokens=0)
            assert topped["input"]["remaining"] == 20
            _assert_wallet_invariant(topped)
            rid2 = mgr.reserve_tokens("c-neg", input_tokens=20, output_tokens=10)
            assert isinstance(rid2, str) and len(rid2) > 0

    def test_insufficient_balance_and_invalid_states_rejected(self, tmp_path):
        with SQLiteReservationManager(tmp_path / "w.db") as mgr:
            mgr.credit_wallet("c-low", input_tokens=100, output_tokens=50)

            with pytest.raises(InsufficientBalanceError):
                mgr.reserve_tokens("c-low", input_tokens=101, output_tokens=10)

            with pytest.raises(InsufficientBalanceError):
                mgr.reserve_tokens("c-low", input_tokens=10, output_tokens=51)

            with pytest.raises(ContractNotFoundError):
                mgr.reserve_tokens("c-missing", input_tokens=1, output_tokens=1)

            mgr.reserve_tokens("c-low", input_tokens=20, output_tokens=10, reservation_id="dup")
            with pytest.raises(DuplicateReservationError):
                mgr.reserve_tokens("c-low", input_tokens=10, output_tokens=5, reservation_id="dup")

            mgr.set_status("c-low", "suspended")
            with pytest.raises(ContractInactiveError):
                mgr.reserve_tokens("c-low", input_tokens=1, output_tokens=1)

            # Balance unchanged by rejected operations
            bal = mgr.get_balance("c-low")
            assert bal["input"]["reserved"] == 20
            assert bal["input"]["remaining"] == 80
            assert bal["output"]["reserved"] == 10
            assert bal["output"]["remaining"] == 40
            _assert_wallet_invariant(bal)

    def test_commit_missing_or_double_commit_raises(self, tmp_path):
        with SQLiteReservationManager(tmp_path / "w.db") as mgr:
            mgr.credit_wallet("c-1", input_tokens=500, output_tokens=500)
            mgr.reserve_tokens("c-1", input_tokens=100, output_tokens=50, reservation_id="r-once")
            mgr.commit_reservation("c-1", "r-once", actual_input=100, actual_output=50)

            with pytest.raises(ReservationNotFoundError):
                mgr.commit_reservation("c-1", "r-once", actual_input=10, actual_output=10)

            with pytest.raises(ReservationNotFoundError):
                mgr.commit_reservation("c-1", "r-never", actual_input=10, actual_output=10)

    def test_release_reservation_refunds_and_is_idempotent(self, tmp_path):
        with SQLiteReservationManager(tmp_path / "w.db") as mgr:
            mgr.credit_wallet("c-rel", input_tokens=400, output_tokens=200)
            mgr.reserve_tokens("c-rel", input_tokens=150, output_tokens=75, reservation_id="r-rel")

            assert mgr.release_reservation("c-rel", "r-rel") is True
            assert mgr.release_reservation("c-rel", "r-rel") is False
            assert mgr.release_reservation("c-rel", "r-unknown") is False

            bal = mgr.get_balance("c-rel")
            assert bal["input"]["consumed"] == 0
            assert bal["input"]["reserved"] == 0
            assert bal["input"]["remaining"] == 400
            assert bal["output"]["consumed"] == 0
            assert bal["output"]["reserved"] == 0
            assert bal["output"]["remaining"] == 200
            assert bal["active_reservations"] == 0
            _assert_wallet_invariant(bal)

    def test_expire_stale_reservations_refunds_only_expired(self, tmp_path):
        with SQLiteReservationManager(tmp_path / "w.db") as mgr:
            mgr.credit_wallet("c-a", input_tokens=1000, output_tokens=1000)
            mgr.credit_wallet("c-b", input_tokens=500, output_tokens=500)

            base_now = 1_700_000_000.0
            # Stale (> 300s old)
            mgr.reserve_tokens("c-a", 100, 50, reservation_id="stale-1", created_at=base_now - 600)
            mgr.reserve_tokens("c-a", 200, 100, reservation_id="stale-2", created_at=base_now - 301)
            mgr.reserve_tokens("c-b", 120, 60, reservation_id="stale-3", created_at=base_now - 400)
            # Fresh (<= 300s old)
            mgr.reserve_tokens("c-a", 50, 25, reservation_id="fresh-1", created_at=base_now - 100)
            mgr.reserve_tokens("c-b", 30, 15, reservation_id="fresh-2", created_at=base_now - 300)

            expired = mgr.expire_stale_reservations(max_age_seconds=300.0, now=base_now)
            assert expired == 3

            bal_a = mgr.get_balance("c-a")
            assert bal_a["input"]["reserved"] == 50
            assert bal_a["input"]["remaining"] == 950
            assert bal_a["output"]["reserved"] == 25
            assert bal_a["output"]["remaining"] == 975
            assert bal_a["active_reservations"] == 1
            _assert_wallet_invariant(bal_a)

            bal_b = mgr.get_balance("c-b")
            assert bal_b["input"]["reserved"] == 30
            assert bal_b["input"]["remaining"] == 470
            assert bal_b["output"]["reserved"] == 15
            assert bal_b["output"]["remaining"] == 485
            assert bal_b["active_reservations"] == 1
            _assert_wallet_invariant(bal_b)

            # Second run at same timestamp expires nothing
            assert mgr.expire_stale_reservations(max_age_seconds=300.0, now=base_now) == 0

    def test_in_memory_mode(self):
        with SQLiteReservationManager(":memory:") as mgr:
            mgr.credit_wallet("mem-1", input_tokens=100, output_tokens=50)
            rid = mgr.reserve_tokens("mem-1", 40, 20)
            res = mgr.commit_reservation("mem-1", rid, actual_input=35, actual_output=25)
            assert res["input"]["remaining"] == 65
            assert res["output"]["remaining"] == 25
            _assert_wallet_invariant(res)


class TestSQLiteReservationManagerConcurrency:
    def test_concurrent_reservations_never_over_allocate(self, tmp_path):
        db_file = tmp_path / "concurrent_wal.db"
        with SQLiteReservationManager(db_file) as mgr:
            # Exactly 25 reservations of (10, 5) can fit into (250, 125)
            mgr.credit_wallet("c-race", input_tokens=250, output_tokens=125)

            n_threads = 60
            barrier = threading.Barrier(n_threads)
            succeeded: list[str] = []
            rejected = 0
            lock = threading.Lock()

            def worker(idx: int) -> None:
                nonlocal rejected
                barrier.wait()
                try:
                    rid = mgr.reserve_tokens(
                        "c-race",
                        input_tokens=10,
                        output_tokens=5,
                        reservation_id=f"r-{idx}",
                    )
                    with lock:
                        succeeded.append(rid)
                except InsufficientBalanceError:
                    with lock:
                        rejected += 1

            with ThreadPoolExecutor(max_workers=n_threads) as pool:
                futures = [pool.submit(worker, i) for i in range(n_threads)]
                for f in futures:
                    f.result()

            assert len(succeeded) == 25
            assert rejected == 35

            bal = mgr.get_balance("c-race")
            assert bal["input"]["reserved"] == 250
            assert bal["input"]["remaining"] == 0
            assert bal["output"]["reserved"] == 125
            assert bal["output"]["remaining"] == 0
            assert bal["active_reservations"] == 25
            _assert_wallet_invariant(bal)

    def test_concurrent_reserve_commit_release_across_two_instances(self, tmp_path):
        db_file = tmp_path / "multi_instance_wal.db"
        mgr1 = SQLiteReservationManager(db_file)
        mgr2 = SQLiteReservationManager(db_file)
        try:
            mgr1.credit_wallet("c-multi", input_tokens=100_000, output_tokens=50_000)

            n_workers = 40
            barrier = threading.Barrier(n_workers)

            def worker(idx: int) -> tuple[int, int]:
                mgr = mgr1 if idx % 2 == 0 else mgr2
                barrier.wait()
                rid = mgr.reserve_tokens("c-multi", input_tokens=100, output_tokens=50)
                if idx % 4 == 0:
                    # Release (0 consumed)
                    assert mgr.release_reservation("c-multi", rid) is True
                    return (0, 0)
                if idx % 4 == 1:
                    # Overage (130 in, 65 out)
                    mgr.commit_reservation("c-multi", rid, actual_input=130, actual_output=65)
                    return (130, 65)
                # Under-usage (70 in, 35 out)
                mgr.commit_reservation("c-multi", rid, actual_input=70, actual_output=35)
                return (70, 35)

            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                results = list(pool.map(worker, range(n_workers)))

            expected_in = sum(r[0] for r in results)
            expected_out = sum(r[1] for r in results)

            bal = mgr1.get_balance("c-multi")
            assert bal["active_reservations"] == 0
            assert bal["input"]["reserved"] == 0
            assert bal["output"]["reserved"] == 0
            assert bal["input"]["consumed"] == expected_in
            assert bal["output"]["consumed"] == expected_out
            assert bal["input"]["remaining"] == 100_000 - expected_in
            assert bal["output"]["remaining"] == 50_000 - expected_out
            _assert_wallet_invariant(bal)
        finally:
            mgr1.close()
            mgr2.close()

    def test_expire_stale_uses_real_clock_when_now_omitted(self, tmp_path):
        with SQLiteReservationManager(tmp_path / "clock.db") as mgr:
            mgr.credit_wallet("c-clk", input_tokens=100, output_tokens=100)
            mgr.reserve_tokens("c-clk", 40, 20, reservation_id="old", created_at=time.time() - 600)
            mgr.reserve_tokens("c-clk", 10, 5, reservation_id="new")
            assert mgr.expire_stale_reservations(max_age_seconds=300) == 1
            bal = mgr.get_balance("c-clk")
            assert bal["input"]["reserved"] == 10
            assert bal["input"]["remaining"] == 90
            _assert_wallet_invariant(bal)
