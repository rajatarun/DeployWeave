"""Tests for scoring models from shared observatory spans.

Stub items mirror what TeamWeave's ``mcp_observatory._push_metric`` writes:
Decimal numerics, ISO-8601 ``start_time``/``end_time``, ``composite_risk_score``
in [0,1] where lower is better.
"""

from decimal import Decimal

import pytest

import observatory_metrics as om


def span(
    model_id="anthropic.claude-3-haiku-20240307-v1:0",
    risk="0.10",
    cost="0.0004",
    prompt_tokens=400,
    completion_tokens=120,
    start="2026-09-13T10:00:00.000000+00:00",
    end="2026-09-13T10:00:00.250000+00:00",
    **extra,
):
    """One observatory span item as the DynamoDB resource API returns it."""
    item = {
        "pk": om.MODEL_SPAN_PK,
        "sk": f"2026-09-13T10:00:00.000000#trace-{model_id}",
        "trace_id": f"trace-{model_id}",
        "operation": "invoke_model",
        "timestamp": "2026-09-13T10:00:00.000000",
        "model_id": model_id,
        "prompt_tokens": Decimal(prompt_tokens),
        "completion_tokens": Decimal(completion_tokens),
        "cost_usd": Decimal(cost),
        "composite_risk_score": Decimal(risk),
        "start_time": start,
        "end_time": end,
        "decision": "allow",
        "decision_reason": "none",
        "ttl": Decimal(1789000000),
    }
    item.update(extra)
    return item


# ── Latency derivation ────────────────────────────────────────────────────────

def test_latency_comes_from_the_start_end_pair():
    assert om.span_latency_ms(span()) == 250.0


def test_latency_is_none_without_timestamps():
    assert om.span_latency_ms(span(start=None, end=None)) is None
    assert om.span_latency_ms({}) is None


def test_latency_is_none_when_the_pair_is_inverted():
    inverted = span(
        start="2026-09-13T10:00:01.000000+00:00",
        end="2026-09-13T10:00:00.000000+00:00",
    )
    assert om.span_latency_ms(inverted) is None


def test_latency_tolerates_naive_and_z_suffixed_timestamps():
    assert om.span_latency_ms(
        span(start="2026-09-13T10:00:00Z", end="2026-09-13T10:00:00.500000Z")
    ) == 500.0
    assert om.span_latency_ms(
        span(start="2026-09-13T10:00:00", end="2026-09-13T10:00:01")
    ) == 1000.0


# ── Summarisation ─────────────────────────────────────────────────────────────

def test_summaries_average_per_model():
    items = [
        span(model_id="m1", risk="0.20", cost="0.001"),
        span(model_id="m1", risk="0.40", cost="0.003"),
        span(model_id="m2", risk="0.10", cost="0.002"),
    ]
    s = om.summarize_spans(items)
    assert s["m1"]["sample_count"] == 2
    assert s["m1"]["mean_composite_risk"] == pytest.approx(0.30)
    assert s["m1"]["mean_cost_usd"] == pytest.approx(0.002)
    assert s["m2"]["sample_count"] == 1


def test_tokens_are_summed_across_prompt_and_completion():
    s = om.summarize_spans([span(model_id="m1", prompt_tokens=400, completion_tokens=100)])
    assert s["m1"]["mean_total_tokens"] == 500.0


def test_spans_without_model_id_are_not_attributed():
    anonymous = span()
    del anonymous["model_id"]
    assert om.summarize_spans([anonymous]) == {}


def test_missing_risk_counts_as_neutral_not_as_zero():
    no_risk = span(model_id="m1")
    del no_risk["composite_risk_score"]
    s = om.summarize_spans([no_risk])
    assert s["m1"]["mean_composite_risk"] == om.NEUTRAL_RISK


def test_missing_timings_leave_latency_unknown_rather_than_zero():
    s = om.summarize_spans([span(model_id="m1", start=None, end=None)])
    assert s["m1"]["mean_latency_ms"] is None


# ── Scoring ───────────────────────────────────────────────────────────────────

def test_score_follows_the_documented_formula():
    # risk 0.20 → quality 0.80; 250ms of a 1000ms budget → latency_fit 0.75;
    # sole model sets the cost ceiling → cost_fit 0.0.
    scored = om.score_models([span(model_id="m1", risk="0.20")], latency_budget_ms=1000)
    expected = 0.5 * 0.80 + 0.3 * 0.75 + 0.2 * 0.0
    assert scored["m1"]["score"] == round(expected, 6)
    assert scored["m1"]["quality"] == 0.8
    assert scored["m1"]["latency_fit"] == 0.75
    assert scored["m1"]["cost_fit"] == 0.0


def test_lower_risk_wins_all_else_equal():
    scored = om.score_models(
        [span(model_id="safe", risk="0.05"), span(model_id="risky", risk="0.80")],
        latency_budget_ms=1000,
    )
    assert scored["safe"]["score"] > scored["risky"]["score"]


def test_model_over_the_latency_budget_is_penalised_to_zero_not_negative():
    scored = om.score_models(
        [span(model_id="slow", start="2026-09-13T10:00:00Z", end="2026-09-13T10:00:05Z")],
        latency_budget_ms=1000,
    )
    assert scored["slow"]["latency_fit"] == 0.0
    assert scored["slow"]["score"] >= 0.0


def test_cost_is_relative_to_the_most_expensive_candidate():
    scored = om.score_models(
        [span(model_id="cheap", cost="0.001"), span(model_id="pricey", cost="0.010")],
        latency_budget_ms=1000,
    )
    assert scored["pricey"]["cost_fit"] == 0.0
    assert scored["cheap"]["cost_fit"] == round(1 - 0.001 / 0.010, 6)


def test_zero_cost_spans_do_not_divide_by_zero():
    scored = om.score_models([span(model_id="free", cost="0")], latency_budget_ms=1000)
    assert scored["free"]["cost_fit"] == 1.0


def test_candidates_filter_restricts_scoring_and_the_cost_ceiling():
    items = [
        span(model_id="candidate", cost="0.001"),
        span(model_id="other-product-model", cost="0.500"),
    ]
    scored = om.score_models(items, latency_budget_ms=1000, candidates=["candidate"])
    assert set(scored) == {"candidate"}
    # The non-candidate must not set the ceiling that makes 'candidate' look cheap.
    assert scored["candidate"]["cost_fit"] == 0.0


def test_unknown_latency_scores_neutral_rather_than_perfect():
    scored = om.score_models(
        [span(model_id="m1", risk="0.0", cost="0", start=None, end=None)],
        latency_budget_ms=1000,
    )
    assert scored["m1"]["latency_fit"] == om.NEUTRAL_RISK


def test_no_spans_scores_nothing_so_the_caller_can_fall_back():
    assert om.score_models([], latency_budget_ms=1000) == {}
    assert om.score_models([span(model_id="m1")], candidates=["other"]) == {}


def test_scores_stay_inside_the_unit_interval():
    items = [span(model_id="m1", risk="0.0", cost="0"), span(model_id="m2", risk="1.0", cost="9")]
    for entry in om.score_models(items, latency_budget_ms=1000).values():
        assert 0.0 <= entry["score"] <= 1.0


def test_weights_sum_to_one():
    assert om.WEIGHT_QUALITY + om.WEIGHT_LATENCY + om.WEIGHT_COST == 1.0


# ── Table access ──────────────────────────────────────────────────────────────

class FakeTable:
    def __init__(self, items):
        self.items = items
        self.kwargs = None

    def query(self, **kwargs):
        self.kwargs = kwargs
        return {"Items": self.items}


def test_fetch_queries_the_invoke_model_partition_newest_first():
    table = FakeTable([span()])
    items = om.fetch_model_spans(table, limit=25)
    assert len(items) == 1
    assert table.kwargs["ScanIndexForward"] is False
    assert table.kwargs["Limit"] == 25


def test_table_name_helper_reads_the_shared_env_var(monkeypatch):
    monkeypatch.delenv("OBSERVATORY_METRICS_TABLE", raising=False)
    assert om.observatory_table_name() == ""
    monkeypatch.setenv("OBSERVATORY_METRICS_TABLE", " shared-observatory ")
    assert om.observatory_table_name() == "shared-observatory"
