"""Model scoring from the platform's shared observatory telemetry table.

Five sibling products write mcp-observatory spans to one DynamoDB table
(``OBSERVATORY_METRICS_TABLE``). DeployWeave's own ``deployweave-model-metrics``
table only ever sees what DeployWeave itself invoked; the shared table sees
every model call the platform makes, so it is a much larger sample of the same
question ``model_selector`` is asking.

Reads go through the ``SpanTimelineIndex`` GSI (shared contract v2.0.0, vendored
in ``contracts/``), not through the partition key. Under v1 this module queried
``OBSERVATORY#invoke_model`` by exact pk, so it saw a writer's spans only if
that writer had guessed the same prefix grammar — three of the five had not, and
their telemetry was durable, billable and invisible to the model selector. The
index is keyed on a date bucket plus a timestamp, which no writer has to agree
with anyone about, and ``operation`` is applied as a filter instead.

Span item shape (written by TeamWeave's ``mcp_observatory._push_metric`` and
``_extract_span_fields``):

    pk                    S  "{namespace}#{discriminator}" — the writer's own business now
    sk                    S  "{iso_timestamp}#{trace_id}" — sorts chronologically
    span_date             S  "YYYY-MM-DD" UTC — SpanTimelineIndex partition key
    model_id              S  present on invoke_model spans (from the `extra` dict)
    operation             S  "invoke_model" here — filtered on, no longer partitioned on
    timestamp             S  ISO 8601, UTC — SpanTimelineIndex sort key
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

import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

from contracts.conformance import load_contract

logger = logging.getLogger(__name__)

# The SpanTimelineIndex coordinates come from the vendored contract, not from
# literals retyped here: the index name and its two key attributes are a
# cross-repository interface, and a copy of them in this file is a second place
# to get them wrong. contracts/README.md forbids hand-editing the vendored copy,
# so reading it is also what makes the version bump visible here.
_CONTRACT = load_contract()
_GSI = _CONTRACT["gsi"]
SPAN_INDEX_NAME = _GSI["name"]                      # "SpanTimelineIndex"
SPAN_INDEX_PARTITION_KEY = _GSI["partition_key"]    # "span_date", YYYY-MM-DD UTC
SPAN_INDEX_SORT_KEY = _GSI["sort_key"]              # "timestamp", ISO 8601 UTC

# Operation whose spans carry model_id. Under v2 this is a FilterExpression on
# the index, not a partition key, so spans from writers that chose any other pk
# prefix are now read too.
MODEL_SPAN_OPERATION = "invoke_model"

# Legacy v1 partition key, kept only for the pre-index fallback below.
MODEL_SPAN_PK = f"OBSERVATORY#{MODEL_SPAN_OPERATION}"

# Scoring weights — see module docstring.
WEIGHT_QUALITY = 0.5
WEIGHT_LATENCY = 0.3
WEIGHT_COST = 0.2

# Risk assumed for a span that reports none.
NEUTRAL_RISK = 0.5

# Default number of most-recent spans examined per query.
DEFAULT_SPAN_LIMIT = 200

# How far back the index read reaches. A date-partitioned index has to be told
# which days to ask for, which DEFAULT_SPAN_LIMIT (a count, not a duration)
# cannot answer; the two compose -- at most DEFAULT_SPAN_LIMIT spans, drawn from
# the last DEFAULT_LOOKBACK_DAYS days. Seven days is a week of platform traffic,
# long enough that a quiet weekend does not empty the sample and short enough
# that a model's scores reflect its current behaviour.
DEFAULT_LOOKBACK_DAYS = 7

# Set once when the index turns out not to exist yet, so the warning below is
# logged a single time per process rather than on every model-selection call.
_index_fallback_warned = False


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


def span_dates(lookback_days: int = DEFAULT_LOOKBACK_DAYS, now: Optional[datetime] = None) -> list[str]:
    """The ``span_date`` buckets covering the last ``lookback_days`` days, newest first.

    Days are UTC calendar days because that is what the index is keyed on
    (contract I6: ``span_date`` is ``YYYY-MM-DD`` in UTC, normally
    ``timestamp[:10]``). The boundary day is included whole and then trimmed by
    the sort-key condition, so the window is a rolling ``lookback_days``
    interval rather than a ragged "since midnight N days ago".
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, int(lookback_days)))
    days: list[str] = []
    day = now.date()
    while day >= cutoff.date():
        days.append(day.isoformat())
        day -= timedelta(days=1)
    return days


def _cutoff_timestamp(lookback_days: int, now: Optional[datetime] = None) -> str:
    """Lower bound for the index sort key, as a string that compares correctly.

    Written without a timezone suffix on purpose. Writers disagree about how
    they spell UTC -- the contract accepts bare, ``Z`` and ``+00:00`` forms --
    and DynamoDB compares sort keys as bytes, so a cutoff that stops at the
    microseconds is a prefix all three spellings sort after.
    """
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=max(1, int(lookback_days)))).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _is_missing_index_error(exc: Exception, index_name: str) -> bool:
    """True only when ``exc`` says the index itself is not there (yet).

    The GSI is created by a separate CloudFormation change and has to finish
    backfilling before readers switch, so between deploys this reader must be
    able to keep working off the old partition key. That tolerance has to be
    narrow: every other failure -- throttling, credentials, a malformed key
    condition, a missing table -- must still reach the caller, because a
    fallback that swallows those returns a smaller sample and calls it an
    answer, which is worse than an error.

    Two shapes count, and nothing else:

    * ``ResourceNotFoundException`` -- DynamoDB's answer when the named index
      is not on the table. It is also the answer when the *table* is gone, but
      that case does not get silently absorbed: the legacy query runs against
      the same table and raises it again.
    * ``ValidationException`` whose message names this index -- the wording
      botocore returns for a query against an index the table does not have.
      A ``ValidationException`` about anything else (a bad comparison operator,
      a key attribute that is not part of the schema) does not match.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error") or {}
    code = error.get("Code")
    if code == "ResourceNotFoundException":
        return True
    if code != "ValidationException":
        return False
    return index_name.lower() in str(error.get("Message", "")).lower()


def _fetch_model_spans_by_pk(table, limit: int) -> list[dict]:
    """The v1 read: one partition, by exact pk. Only reachable via the fallback.

    Sees only spans whose writer happened to use ``OBSERVATORY#invoke_model``,
    which is the defect v2 exists to fix -- so this runs only while the index
    is still being created, and is not a steady state.
    """
    from boto3.dynamodb.conditions import Key  # local import: keeps this module importable without boto3

    resp = table.query(
        KeyConditionExpression=Key("pk").eq(MODEL_SPAN_PK),
        ScanIndexForward=False,
        Limit=limit,
    )
    return resp.get("Items", [])


def fetch_model_spans(
    table,
    limit: int = DEFAULT_SPAN_LIMIT,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Read the most recent ``invoke_model`` spans from the shared table.

    Queries ``SpanTimelineIndex`` (contract v2) one ``span_date`` partition per
    day, newest day first, with ``ScanIndexForward=False`` so each day's rows
    come back newest first too; the result is therefore in overall
    newest-first order, as it was when this read went through the base table.

    ``operation == "invoke_model"`` is a ``FilterExpression`` rather than the
    partition key. That is the point of the migration: under v1 a span was
    visible only if its writer had guessed the ``OBSERVATORY#invoke_model``
    prefix, and three of the five writers had not. Their spans are indexed by
    date like everyone else's, so this read picks them up whatever pk they
    chose.

    Two caveats worth knowing:

    * DynamoDB applies ``Limit`` to items *read*, before the filter, so a day
      whose traffic is mostly other operations can return fewer than ``limit``
      model spans. This reader is scoring an average, not paginating a UI, so a
      short sample is acceptable where a wrong one would not be.
    * The index is not retroactive. Rows written before v2 carry no
      ``span_date`` and are absent from it; they remain reachable only by their
      original pk.
    """
    from boto3.dynamodb.conditions import Attr, Key  # local import: keeps this module importable without boto3
    from botocore.exceptions import ClientError

    cutoff = _cutoff_timestamp(lookback_days, now)

    collected: list[dict] = []
    seen: set = set()
    try:
        for day in span_dates(lookback_days, now):
            resp = table.query(
                IndexName=SPAN_INDEX_NAME,
                KeyConditionExpression=(
                    Key(SPAN_INDEX_PARTITION_KEY).eq(day)
                    & Key(SPAN_INDEX_SORT_KEY).gte(cutoff)
                ),
                FilterExpression=Attr("operation").eq(MODEL_SPAN_OPERATION),
                ScanIndexForward=False,
                # Per day, not a running remainder: the cap is on how much this
                # reader is willing to read, and the merged result is truncated
                # below.
                Limit=limit,
            )
            for item in resp.get("Items", []):
                # (pk, sk) is the base-table identity, projected into the index
                # by ALL. Averages are computed over these rows, so a row
                # counted twice quietly reweights a model's score.
                identity = (item.get("pk"), item.get("sk"))
                if all(identity):
                    if identity in seen:
                        continue
                    seen.add(identity)
                collected.append(item)
            if len(collected) >= limit:
                break
    except ClientError as exc:
        if not _is_missing_index_error(exc, SPAN_INDEX_NAME):
            raise
        _warn_index_missing_once()
        return _fetch_model_spans_by_pk(table, limit)

    return collected[:limit]


def _warn_index_missing_once() -> None:
    """Log the fallback once per process, at warning level."""
    global _index_fallback_warned
    if _index_fallback_warned:
        return
    _index_fallback_warned = True
    logger.warning(
        "%s is not on the shared observatory table yet; falling back to the legacy "
        "%s partition query, which sees only spans written with that pk prefix. "
        "This resolves itself once the shared stack's GSI is created and backfilled.",
        SPAN_INDEX_NAME,
        MODEL_SPAN_PK,
    )
