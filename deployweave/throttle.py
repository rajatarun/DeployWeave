"""Auto-throttle policy exporter producing ``agent-policy-v1`` routing hints.

Translates daily token-budget utilization from :func:`deployweave.alerts.evaluate_budget_forecast`
into deterministic cost-adaptive routing recommendations (``normal`` → ``prefer_local`` →
``local_only`` → ``halt``) that downstream routers such as ``ModelWeave`` and ``WeaveOS``
can enforce without hardcoding cloud spend caps.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from deployweave.alerts import evaluate_budget_forecast

if TYPE_CHECKING:
    from deployweave.policy import SQLiteBudgetPolicyEngine

AGENT_POLICY_SCHEMA = "agent-policy-v1"
DEFAULT_WARN_THRESHOLD_PCT = 0.75
DEFAULT_LOCAL_ONLY_THRESHOLD_PCT = 0.90
PREFER_LOCAL_MAX_COST_USD_PER_CALL = 0.01


def _validate_ratio(name: str, value: float, *, allow_one: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number, got {type(value).__name__}")
    val = float(value)
    upper_ok = val <= 1.0 if allow_one else val < 1.0
    if math.isnan(val) or math.isinf(val) or val <= 0.0 or not upper_ok:
        bound = "(0.0, 1.0]" if allow_one else "(0.0, 1.0)"
        raise ValueError(f"{name} must be in {bound}, got {value}")
    return val


def compute_throttle_recommendation(
    policy_engine: SQLiteBudgetPolicyEngine,
    contract_id: str,
    *,
    window_seconds: int = 3600,
    now_ts: float | None = None,
    warn_threshold_pct: float = DEFAULT_WARN_THRESHOLD_PCT,
    local_only_threshold_pct: float = DEFAULT_LOCAL_ONLY_THRESHOLD_PCT,
) -> dict[str, Any]:
    """Compute an auto-throttle routing recommendation and ``agent-policy-v1`` payload.

    Utilization thresholds:
    - ``< warn_threshold_pct`` (default 75%): ``routing_mode="normal"``, ``max_cost_usd_per_call=None``
    - ``warn_threshold_pct <= u < local_only_threshold_pct`` (75%-<90%):
      ``routing_mode="prefer_local"``, ``max_cost_usd_per_call=0.01``
    - ``local_only_threshold_pct <= u < 100%`` (90%-<100%):
      ``routing_mode="local_only"``, ``max_cost_usd_per_call=0.0``
    - ``>= 100%`` (or ``daily_cap == 0``): ``routing_mode="halt"``, ``max_cost_usd_per_call=0.0``
    """
    warn_pct = _validate_ratio("warn_threshold_pct", warn_threshold_pct, allow_one=False)
    local_only_pct = _validate_ratio("local_only_threshold_pct", local_only_threshold_pct, allow_one=True)
    if warn_pct >= local_only_pct:
        raise ValueError(
            f"warn_threshold_pct ({warn_pct}) must be strictly less than "
            f"local_only_threshold_pct ({local_only_pct})"
        )

    forecast = evaluate_budget_forecast(
        policy_engine,
        contract_id,
        window_seconds=window_seconds,
        now_ts=now_ts,
        alert_threshold_pct=warn_pct,
        record_alert=False,
    )

    daily_cap: int | None = forecast["daily_cap"]
    daily_used: int = int(forecast["daily_used"])
    utilization_pct: float = float(forecast["utilization_pct"])

    if daily_cap is not None and (daily_cap == 0 or daily_used >= daily_cap or utilization_pct >= 1.0):
        routing_mode = "halt"
        max_cost_usd_per_call: float | None = 0.0
        enforcement_mode = "enforce"
        reason = (
            f"Daily token cap reached or exceeded ({daily_used}/{daily_cap} tokens, "
            f"{utilization_pct * 100:.1f}% utilization); halting spend."
        )
    elif daily_cap is not None and utilization_pct >= local_only_pct:
        routing_mode = "local_only"
        max_cost_usd_per_call = 0.0
        enforcement_mode = "enforce"
        reason = (
            f"Daily utilization ({utilization_pct * 100:.1f}%) reached local-only threshold "
            f"({local_only_pct * 100:.1f}%); restricting to zero-cost local models."
        )
    elif daily_cap is not None and utilization_pct >= warn_pct:
        routing_mode = "prefer_local"
        max_cost_usd_per_call = PREFER_LOCAL_MAX_COST_USD_PER_CALL
        enforcement_mode = "enforce"
        reason = (
            f"Daily utilization ({utilization_pct * 100:.1f}%) reached warn threshold "
            f"({warn_pct * 100:.1f}%); preferring local models with $0.01/call cap."
        )
    else:
        routing_mode = "normal"
        max_cost_usd_per_call = None
        enforcement_mode = "enforce"
        if daily_cap is None:
            reason = "No daily token cap configured; normal routing."
        else:
            reason = (
                f"Daily utilization ({utilization_pct * 100:.1f}%) below warn threshold "
                f"({warn_pct * 100:.1f}%); normal routing."
            )

    generated_at = datetime.fromtimestamp(float(forecast["timestamp"]), tz=UTC).isoformat().replace("+00:00", "Z")
    policy_id = f"deployweave-throttle-{contract_id}"

    agent_policy: dict[str, Any] = {
        "schema": AGENT_POLICY_SCHEMA,
        "policy_id": policy_id,
        "subject_id": contract_id,
        "enforcement_mode": enforcement_mode,
        "routing_mode": routing_mode,
        "max_cost_usd_per_call": max_cost_usd_per_call,
        "generated_at": generated_at,
        "forecast": forecast,
    }

    return {
        "contract_id": contract_id,
        "routing_mode": routing_mode,
        "max_cost_usd_per_call": max_cost_usd_per_call,
        "utilization_pct": utilization_pct,
        "warn_threshold_pct": warn_pct,
        "local_only_threshold_pct": local_only_pct,
        "reason": reason,
        "generated_at": generated_at,
        "forecast": forecast,
        "agent_policy": agent_policy,
    }
