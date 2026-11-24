"""DeployWeave package entry point.

Exposes standalone local-first components without requiring AWS dependencies
at import time, while preserving root-module backward compatibility for AWS
Lambda handlers.
"""
from __future__ import annotations

from deployweave.alerts import SQLiteBudgetAlertStore, evaluate_budget_forecast
from deployweave.policy import PolicyDecision, SQLiteBudgetPolicyEngine
from sqlite_reservation_manager import (
    ContractInactiveError,
    ContractNotFoundError,
    DuplicateReservationError,
    InsufficientBalanceError,
    ReservationNotFoundError,
    SQLiteReservationManager,
    WalletError,
)

__version__ = "0.2.0"

__all__ = [
    "ContractInactiveError",
    "ContractNotFoundError",
    "DuplicateReservationError",
    "InsufficientBalanceError",
    "PolicyDecision",
    "ReservationNotFoundError",
    "SQLiteBudgetAlertStore",
    "SQLiteBudgetPolicyEngine",
    "SQLiteReservationManager",
    "WalletError",
    "__version__",
    "evaluate_budget_forecast",
]
