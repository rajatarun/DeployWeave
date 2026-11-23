"""Standalone CLI (`deployweave-wallet`) for local SQLite wallet & budget policy management."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

from deployweave.policy import DEFAULT_DB_PATH, SQLiteBudgetPolicyEngine
from sqlite_reservation_manager import SQLiteReservationManager, WalletError


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    for key, value in payload.items():
        if isinstance(value, dict):
            nested = ", ".join(f"{k}={v}" for k, v in value.items())
            print(f"{key}: {nested}")
        else:
            print(f"{key}: {value}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deployweave-wallet",
        description="Manage local SQLite DeployWeave token wallets, reservations, and budget policies.",
    )
    parser.add_argument(
        "--db",
        dest="db",
        default=None,
        help=f"Path to SQLite WAL database (default: $DEPLOYWEAVE_SQLITE_PATH or {DEFAULT_DB_PATH}).",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        default=False,
        help="Emit machine-readable JSON output.",
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        dest="db",
        default=argparse.SUPPRESS,
        help=f"Path to SQLite WAL database (default: $DEPLOYWEAVE_SQLITE_PATH or {DEFAULT_DB_PATH}).",
    )
    common.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit machine-readable JSON output.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # credit
    p_credit = subparsers.add_parser("credit", parents=[common], help="Create or top up a contract wallet.")
    p_credit.add_argument("contract_id", help="Contract / tenant identifier.")
    p_credit.add_argument("--input-tokens", "--input", dest="input_tokens", type=int, default=0, help="Input tokens to add.")
    p_credit.add_argument("--output-tokens", "--output", dest="output_tokens", type=int, default=0, help="Output tokens to add.")
    p_credit.add_argument("--status", default="active", help="Initial contract status when creating wallet (default: active).")

    # balance
    p_balance = subparsers.add_parser("balance", parents=[common], help="Show wallet balance for a contract.")
    p_balance.add_argument("contract_id", help="Contract / tenant identifier.")

    # reserve
    p_reserve = subparsers.add_parser("reserve", parents=[common], help="Atomically reserve tokens on an active contract.")
    p_reserve.add_argument("contract_id", help="Contract / tenant identifier.")
    p_reserve.add_argument("--input-tokens", "--input", dest="input_tokens", type=int, default=0, help="Input tokens to reserve.")
    p_reserve.add_argument("--output-tokens", "--output", dest="output_tokens", type=int, default=0, help="Output tokens to reserve.")
    p_reserve.add_argument("--reservation-id", "--id", dest="reservation_id", default=None, help="Optional explicit reservation ID.")
    p_reserve.add_argument(
        "--check-policy",
        action="store_true",
        default=False,
        help="Enforce RPM/TPM/daily policy before reserving tokens.",
    )

    # commit
    p_commit = subparsers.add_parser("commit", parents=[common], help="Commit actual token usage for a pending reservation.")
    p_commit.add_argument("contract_id", help="Contract / tenant identifier.")
    p_commit.add_argument("reservation_id", help="Pending reservation ID.")
    p_commit.add_argument("--actual-input", "--input-tokens", "--input", dest="actual_input", type=int, default=0, help="Actual input tokens consumed.")
    p_commit.add_argument("--actual-output", "--output-tokens", "--output", dest="actual_output", type=int, default=0, help="Actual output tokens consumed.")
    p_commit.add_argument("--cached-input", "--cached", dest="cached_input", type=int, default=0, help="Cached input tokens.")

    # release
    p_release = subparsers.add_parser("release", parents=[common], help="Release a pending reservation without consuming tokens.")
    p_release.add_argument("contract_id", help="Contract / tenant identifier.")
    p_release.add_argument("reservation_id", help="Pending reservation ID.")

    # set-policy
    p_policy = subparsers.add_parser("set-policy", parents=[common], help="Set RPM, TPM, and daily token cap for a contract.")
    p_policy.add_argument("contract_id", help="Contract / tenant identifier.")
    p_policy.add_argument("--max-rpm", type=int, default=None, help="Maximum requests per rolling 60s window.")
    p_policy.add_argument("--max-tpm", type=int, default=None, help="Maximum tokens per rolling 60s window.")
    p_policy.add_argument("--daily-token-cap", "--daily-cap", dest="daily_token_cap", type=int, default=None, help="Hard UTC daily token cap.")

    # usage
    p_usage = subparsers.add_parser("usage", parents=[common], help="Show rolling-window and daily policy usage for a contract.")
    p_usage.add_argument("contract_id", help="Contract / tenant identifier.")
    p_usage.add_argument("--now-ts", type=float, default=None, help="Optional POSIX timestamp override for deterministic inspection.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    db_path = args.db if args.db is not None else os.environ.get("DEPLOYWEAVE_SQLITE_PATH", DEFAULT_DB_PATH)
    as_json: bool = bool(args.as_json)

    try:
        with (
            SQLiteReservationManager(db_path) as mgr,
            SQLiteBudgetPolicyEngine(db_path, reservation_manager=mgr) as policy_engine,
        ):
            cmd = args.command

            if cmd == "credit":
                result = mgr.credit_wallet(
                    args.contract_id,
                    input_tokens=args.input_tokens,
                    output_tokens=args.output_tokens,
                    status=args.status,
                )
                _emit(result, as_json=as_json)
                return 0

            if cmd == "balance":
                result = mgr.get_balance(args.contract_id)
                _emit(result, as_json=as_json)
                return 0

            if cmd == "reserve":
                policy_decision = None
                if args.check_policy:
                    decision = policy_engine.check_and_record(
                        args.contract_id,
                        estimated_tokens=args.input_tokens + args.output_tokens,
                    )
                    policy_decision = decision.to_dict()
                    if not decision.allowed:
                        _emit(policy_decision, as_json=as_json)
                        return 2

                rid = mgr.reserve_tokens(
                    args.contract_id,
                    input_tokens=args.input_tokens,
                    output_tokens=args.output_tokens,
                    reservation_id=args.reservation_id,
                )
                result: dict[str, Any] = {
                    "contract_id": args.contract_id,
                    "reservation_id": rid,
                    "input_reserved": args.input_tokens,
                    "output_reserved": args.output_tokens,
                    "balance": mgr.get_balance(args.contract_id),
                }
                if policy_decision is not None:
                    result["policy_decision"] = policy_decision
                _emit(result, as_json=as_json)
                return 0

            if cmd == "commit":
                result = mgr.commit_reservation(
                    args.contract_id,
                    args.reservation_id,
                    actual_input=args.actual_input,
                    actual_output=args.actual_output,
                    cached_input=args.cached_input,
                )
                _emit(result, as_json=as_json)
                return 0

            if cmd == "release":
                released = mgr.release_reservation(args.contract_id, args.reservation_id)
                result = {
                    "contract_id": args.contract_id,
                    "reservation_id": args.reservation_id,
                    "released": released,
                }
                _emit(result, as_json=as_json)
                return 0

            if cmd == "set-policy":
                result = policy_engine.set_policy(
                    args.contract_id,
                    max_rpm=args.max_rpm,
                    max_tpm=args.max_tpm,
                    daily_token_cap=args.daily_token_cap,
                )
                _emit(result, as_json=as_json)
                return 0

            if cmd == "usage":
                result = policy_engine.get_usage_summary(
                    args.contract_id,
                    now_ts=args.now_ts,
                )
                _emit(result, as_json=as_json)
                return 0

            parser.error(f"Unknown command: {cmd}")
            return 2

    except (WalletError, ValueError, TypeError) as exc:
        if as_json:
            print(
                json.dumps({"error": str(exc), "error_type": type(exc).__name__}, indent=2, sort_keys=True),
                file=sys.stderr,
            )
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
