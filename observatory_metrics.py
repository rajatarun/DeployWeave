"""Model scoring from the platform's shared observatory telemetry table.

Five sibling products write mcp-observatory spans to one DynamoDB table
(``OBSERVATORY_METRICS_TABLE``). DeployWeave's own ``deployweave-model-metrics``
table only ever sees what DeployWeave itself invoked; the shared table sees
every model call the platform makes, so it is a much larger sample of the same
question ``model_selector`` is asking.

Span item shape (written by TeamWeave's ``mcp_observatory._push_metric`` and
``_extract_span_fields``):

    pk                    S  "OBSERVATORY#{operation}", e.g. OBSERVATORY#invoke_model
    sk                    S  "{iso_timestamp}#{trace_id}" — sorts chronologically
    model_id              S  present on invoke_model spans (from the `extra` dict)
    timestamp             S  ISO 8601, UTC
    prompt_tokens         N  Decimal
    completion_tokens     N  Decimal
    cost_usd              N  Decimal
    composite_risk_score  N  Decimal in [0,1], LOWER is better
    start_time/end_time   S  ISO 8601 — latency is their difference
    decision              S  policy action ("allow", "block", ...)

Composite score
---------------
Each model's spans are averaged, then combined into one score in [0,1] where
higher is better::

    quality     = 1 - mean(composite_risk_score)            # risk is 0..1, lower better
    latency_fit = clamp(1 - mean_latency_ms / latency_budget_ms, 0, 1)
    cost_fit    = 1 - mean_cost_usd / max(mean_cost_usd over scored models)

    score = 0.5 * quality + 0.3 * latency_fit + 0.2 * cost_fit

Weights say: answer quality matters most, a model that blows the caller's
latency budget is nearly useless however good it is, and cost breaks ties.
``cost_fit`` is relative to the most expensive model in the same comparison —
there is no absolute budget to normalise against, and the question being asked
is only ever "which of these".

Spans with no ``composite_risk_score`` contribute 0.5 (neutral) rather than
being dropped: their latency and cost are still real observations.

Known limitation: observatory spans carry no task_type, so these scores are
per-model across all workloads on the platform, not per-task. DeployWeave's
own metrics table remains the task-specific fallback.
"""

from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Optional

# Operation whose spans carry model_id.
MODEL_SPAN_PK = "OBSERVATORY#invoke_model"

# Scoring weights — see module docstring.
WEIGHT_QUALITY = 0.5
WEIGHT_LATENCY = 0.3
WEIGHT_COST = 0.2

# Risk assumed for a span that reports none.
NEUTRAL_RISK = 0.5

# Default number of most-recent spans examined per query.
DEFAULT_SPAN_LIMIT = 200


def observatory_table_name() -> str:
    """Return OBSERVATORY_METRICS_TABLE, or an empty string when unset."""
    return os.environ.get("OBSERVATORY_METRICS_TABLE", "").strip()


def _to_float(value: Any) -> Optional[float]:
    """Coerce a DynamoDB numeric (Decimal, int, float, numeric string) to float."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def span_latency_ms(item: dict) -> Optional[float]:
    """Latency of one span, from its start_time/end_time pair.

    The observatory schema has no latency field — the wrapper records the two
    timestamps instead. Returns None when either is missing or unparseable,
    or when the pair is inverted (clock skew between products).
    """
    start = _parse_time(item.get("start_time"))
    end = _parse_time(item.get("end_time"))
    if start is None or end is None:
        return None
    if start.tzinfo is None or end.tzinfo is None:
        start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
    delta_ms = (end - start).total_seconds() * 1000.0
    return delta_ms if delta_ms >= 0 else None


def summarize_spans(items: Iterable[dict]) -> dict[str, dict]:
    """Average latency, cost, tokens and risk per ``model_id``.

    Spans without a ``model_id`` are ignored — they cannot be attributed.
    """
    totals: dict[str, dict] = {}

    for item in items:
        model_id = item.get("model_id")
        if not model_id:
            continue
        acc = totals.setdefault(model_id, {
            "sample_count": 0,
            "_risk_sum": 0.0,
            "_cost_sum": 0.0,
            "_token_sum": 0.0,
            "_latency_sum": 0.0,
            "_latency_n": 0,
        })
        acc["sample_count"] += 1
        risk = _to_float(item.get("composite_risk_score"))
        acc["_risk_sum"] += NEUTRAL_RISK if risk is None else risk
        acc["_cost_sum"] += _to_float(item.get("cost_usd")) or 0.0
        acc["_token_sum"] += (
            (_to_float(item.get("prompt_tokens")) or 0.0)
            + (_to_float(item.get("completion_tokens")) or 0.0)
        )
        latency = span_latency_ms(item)
        if latency is not None:
            acc["_latency_sum"] += latency
            acc["_latency_n"] += 1

    summaries: dict[str, dict] = {}
    for model_id, acc in totals.items():
        n = acc["sample_count"]
        latency_n = acc["_latency_n"]
        summaries[model_id] = {
            "sample_count": n,
            "mean_composite_risk": acc["_risk_sum"] / n,
            "mean_cost_usd": acc["_cost_sum"] / n,
            "mean_total_tokens": acc["_token_sum"] / n,
            # None, not 0.0: "no timing data" must not read as "instant".
            "mean_latency_ms": (acc["_latency_sum"] / latency_n) if latency_n else None,
        }
    return summaries


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def score_models(
    items: Iterable[dict],
    latency_budget_ms: int = 1000,
    candidates: Optional[Iterable[str]] = None,
) -> dict[str, dict]:
    """Score each model seen in ``items``. See the module docstring for the formula.

    ``candidates`` restricts the result to the models the caller is choosing
    between, which also keeps the relative cost term honest (a model that is
    not a candidate must not set the cost ceiling).
    """
    summaries = summarize_spans(items)
    if candidates is not None:
        allowed = set(candidates)
        summaries = {k: v for k, v in summaries.items() if k in allowed}
    if not summaries:
        return {}

    max_cost = max(s["mean_cost_usd"] for s in summaries.values())
    budget = float(latency_budget_ms) if latency_budget_ms and latency_budget_ms > 0 else 0.0

    scored: dict[str, dict] = {}
    for model_id, summary in summaries.items():
        quality = _clamp01(1.0 - summary["mean_composite_risk"])

        mean_latency = summary["mean_latency_ms"]
        if mean_latency is None or budget == 0.0:
            # No timing evidence (or no budget to judge against): stay neutral
            # instead of rewarding or punishing on a guess.
            latency_fit = NEUTRAL_RISK
        else:
            latency_fit = _clamp01(1.0 - mean_latency / budget)

        cost_fit = 1.0 if max_cost <= 0 else _clamp01(1.0 - summary["mean_cost_usd"] / max_cost)

        score = (
            WEIGHT_QUALITY * quality
            + WEIGHT_LATENCY * latency_fit
            + WEIGHT_COST * cost_fit
        )
        scored[model_id] = {
            **summary,
            "quality": round(quality, 6),
            "latency_fit": round(latency_fit, 6),
            "cost_fit": round(cost_fit, 6),
            "score": round(score, 6),
        }
    return scored


def fetch_model_spans(table, limit: int = DEFAULT_SPAN_LIMIT) -> list[dict]:
    """Read the most recent ``invoke_model`` spans from the shared table.

    Queries the base table by partition key with ``ScanIndexForward=False``,
    so the sort key (``{iso_timestamp}#{trace_id}``) returns newest first.
    """
    from boto3.dynamodb.conditions import Key  # local import: keeps this module importable without boto3

    resp = table.query(
        KeyConditionExpression=Key("pk").eq(MODEL_SPAN_PK),
        ScanIndexForward=False,
        Limit=limit,
    )
    return resp.get("Items", [])
