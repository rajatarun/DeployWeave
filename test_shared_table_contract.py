"""Conformance of DeployWeave's OBSERVATORY_METRICS reader against the shared
table contract vendored at ``contracts/observatory_metrics_item.json``.

DeployWeave only *reads* the shared table (``observatory_metrics.py``,
``fetch_model_spans``). Under contract v1 that read went through the partition
key, so this file asserted that the pk DeployWeave enumerated parsed against
``pk_grammar`` and that the contract listed DeployWeave as a reader of that
namespace. Both were true, and the reader still could not see three of the five
writers: they had chosen other pk prefixes, and a prefix convention that every
writer must independently guess right is not an interface, it is a hope.

Contract v2.0.0 moves reads onto the ``SpanTimelineIndex`` GSI, keyed
``span_date`` + ``timestamp``. What is worth asserting therefore changed shape.
These tests now check that the reader queries *the index the contract names, by
the key attributes the contract names* — read out of the vendored file, never
retyped here, so that editing the contract is what changes what is enforced —
and that the pre-index fallback is narrow enough to be safe.

Namespace reachability (``readers_for``, ``namespace_registry``) is retained
below only as history: it answers a question about rows written before this
migration. Under v2 a namespace with no readers is no longer a defect, because
no reader queries by pk at all.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError

import observatory_metrics as om
from contracts.conformance import check_item, load_contract, readers_for

CONTRACT = load_contract()  # finds contracts/observatory_metrics_item.json beside conformance.py
GSI = CONTRACT["gsi"]

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_fallback_warning():
    """The fallback warns once per process; tests must not inherit that latch."""
    om._index_fallback_warned = False
    yield
    om._index_fallback_warned = False


# ── Doubles ───────────────────────────────────────────────────────────────────

class FakeTable:
    """Records every query. Distinguishes index queries from base-table ones."""

    def __init__(self, index_items=None, legacy_items=None, index_error=None):
        self.index_items = list(index_items or [])
        self.legacy_items = list(legacy_items or [])
        self.index_error = index_error
        self.calls = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        if "IndexName" in kwargs:
            if self.index_error is not None:
                raise self.index_error
            return {"Items": self.index_items}
        return {"Items": self.legacy_items}

    @property
    def index_calls(self):
        return [c for c in self.calls if "IndexName" in c]

    @property
    def base_table_calls(self):
        return [c for c in self.calls if "IndexName" not in c]


def span(pk, sk, span_date="2026-09-13", model_id="m1", operation="invoke_model"):
    """A span as the index (projection ALL) returns it, keyed by any writer's pk."""
    return {
        "pk": pk,
        "sk": sk,
        "span_date": span_date,
        "timestamp": f"{span_date}T10:00:00.000000+00:00",
        "operation": operation,
        "model_id": model_id,
        "cost_usd": Decimal("0.001"),
        "composite_risk_score": Decimal("0.1"),
        "ttl": Decimal(1789000000),
    }


def condition_attributes(condition):
    """Attribute names referenced anywhere in a boto3 Key/Attr condition tree."""
    names = set()
    for value in condition.get_expression()["values"]:
        if hasattr(value, "get_expression"):
            names |= condition_attributes(value)
        elif hasattr(value, "name"):
            names.add(value.name)
    return names


def client_error(code, message="", operation="Query"):
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


# ── The reader reads the index the contract names ─────────────────────────────

def test_index_coordinates_are_taken_from_the_contract_not_retyped():
    """The name and both key attributes must come out of the vendored file.

    If these were literals in the reader, this assertion would be comparing a
    constant against a copy of itself and the next contract bump would pass it
    unchanged.
    """
    assert om.SPAN_INDEX_NAME == GSI["name"]
    assert om.SPAN_INDEX_PARTITION_KEY == GSI["partition_key"]
    assert om.SPAN_INDEX_SORT_KEY == GSI["sort_key"]


def test_reader_queries_the_contracts_index_by_its_contracted_key_attributes():
    table = FakeTable(index_items=[span("OBSERVATORY#invoke_model", "2026-09-13T10:00:00#t1")])

    om.fetch_model_spans(table, limit=25, now=NOW)

    assert table.index_calls, "reader never queried an index"
    for call in table.index_calls:
        assert call["IndexName"] == GSI["name"]
        attrs = condition_attributes(call["KeyConditionExpression"])
        assert attrs == {GSI["partition_key"], GSI["sort_key"]}, (
            f"key condition uses {sorted(attrs)}, but the contract's gsi keys are "
            f"{GSI['partition_key']} + {GSI['sort_key']}"
        )
    assert not table.base_table_calls, (
        "reader still queried the base table; v2 reads go through the index"
    )


def test_the_partition_key_is_no_longer_part_of_the_read():
    """The defect v2 fixes: a read keyed on pk sees only writers who guessed it."""
    table = FakeTable(index_items=[])

    om.fetch_model_spans(table, limit=25, now=NOW)

    for call in table.index_calls:
        attrs = condition_attributes(call["KeyConditionExpression"])
        assert CONTRACT["key_schema"]["partition_key"] not in attrs
        assert om.MODEL_SPAN_PK not in str(call["KeyConditionExpression"].get_expression())


def test_operation_is_applied_as_a_filter_not_as_a_partition():
    table = FakeTable(index_items=[])

    om.fetch_model_spans(table, limit=25, now=NOW)

    for call in table.index_calls:
        expr = call["FilterExpression"]
        assert condition_attributes(expr) == {"operation"}
        assert om.MODEL_SPAN_OPERATION in expr.get_expression()["values"]


def test_span_date_partitions_cover_the_lookback_window_newest_first():
    table = FakeTable(index_items=[])

    om.fetch_model_spans(table, limit=25, lookback_days=3, now=NOW)

    queried = [
        call["KeyConditionExpression"].get_expression()["values"][0].get_expression()["values"][1]
        for call in table.index_calls
    ]
    expected = [(NOW - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(4)]
    assert queried == expected, "every UTC day touched by the window, newest first"
    assert len(set(queried)) == len(queried)
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) for d in queried), "contract I6 format"


def test_sort_key_condition_trims_the_window_to_the_lookback():
    table = FakeTable(index_items=[])

    om.fetch_model_spans(table, limit=25, lookback_days=3, now=NOW)

    cutoffs = {
        call["KeyConditionExpression"].get_expression()["values"][1].get_expression()["values"][1]
        for call in table.index_calls
    }
    assert cutoffs == {"2026-09-10T12:00:00.000000"}, (
        "the oldest day bucket must be trimmed by the sort key, or the window is "
        "'since midnight N days ago' rather than a rolling N days"
    )


def test_spans_from_writers_that_never_used_the_legacy_pk_are_now_read():
    """The payoff, stated as behaviour rather than as query shape.

    These three pks are the shapes the contract's registry records for writers
    other than TeamWeave. Under v1 every one of them was invisible to this
    reader; the only thing that has to be true for them now is that they carry
    span_date, timestamp and operation (contract I6-I8).
    """
    table = FakeTable(index_items=[
        span("SPAN#claude-3-haiku", "2026-09-13T10:00:00#a", model_id="haiku"),
        span("WRAPPER#invoke", "2026-09-13T10:00:01#b", model_id="nova"),
        span("INVOCATION#call", "2026-09-13T10:00:02#c", model_id="gemini"),
    ])

    scored = om.score_models(om.fetch_model_spans(table, limit=50, now=NOW))

    assert set(scored) == {"haiku", "nova", "gemini"}


def test_rows_are_not_double_counted_across_day_partitions():
    """One row must weigh once in an average, however many partitions are asked."""
    table = FakeTable(index_items=[span("SPAN#x", "2026-09-13T10:00:00#a", model_id="m")])

    spans = om.fetch_model_spans(table, limit=50, lookback_days=7, now=NOW)

    assert len(table.index_calls) == 8, "sanity: the window really did span 8 buckets"
    assert len(spans) == 1
    assert om.summarize_spans(spans)["m"]["sample_count"] == 1


# ── The pre-index fallback ────────────────────────────────────────────────────

MISSING_INDEX_MESSAGE = (
    "The table does not have the specified index: SpanTimelineIndex"
)


def test_validation_exception_naming_the_index_falls_back_to_the_legacy_pk_query(caplog):
    legacy = [span("OBSERVATORY#invoke_model", "2026-09-13T10:00:00#a", model_id="m1")]
    table = FakeTable(legacy_items=legacy, index_error=client_error(
        "ValidationException", MISSING_INDEX_MESSAGE))

    with caplog.at_level(logging.WARNING, logger=om.logger.name):
        items = om.fetch_model_spans(table, limit=25, now=NOW)

    assert items == legacy
    assert len(table.base_table_calls) == 1
    legacy_call = table.base_table_calls[0]
    assert om.MODEL_SPAN_PK in str(legacy_call["KeyConditionExpression"].get_expression())
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert om.SPAN_INDEX_NAME in caplog.text


def test_resource_not_found_falls_back_to_the_legacy_pk_query(caplog):
    legacy = [span("OBSERVATORY#invoke_model", "2026-09-13T10:00:00#a", model_id="m1")]
    table = FakeTable(legacy_items=legacy, index_error=client_error(
        "ResourceNotFoundException", "Requested resource not found"))

    with caplog.at_level(logging.WARNING, logger=om.logger.name):
        items = om.fetch_model_spans(table, limit=25, now=NOW)

    assert items == legacy
    assert len(table.base_table_calls) == 1
    assert [r.levelname for r in caplog.records] == ["WARNING"]


def test_the_fallback_warns_once_per_process_not_once_per_call(caplog):
    table = FakeTable(index_error=client_error("ValidationException", MISSING_INDEX_MESSAGE))

    with caplog.at_level(logging.WARNING, logger=om.logger.name):
        om.fetch_model_spans(table, limit=25, now=NOW)
        om.fetch_model_spans(table, limit=25, now=NOW)

    assert len(caplog.records) == 1
    assert len(table.base_table_calls) == 2, "it still falls back every time, it just stops shouting"


@pytest.mark.parametrize("error", [
    client_error("ValidationException",
                 "Query condition missed key schema element: span_date"),
    client_error("ProvisionedThroughputExceededException",
                 "The level of configured provisioned throughput for the table "
                 "index SpanTimelineIndex was exceeded"),
    client_error("AccessDeniedException", "not authorized to perform: dynamodb:Query"),
    client_error("ThrottlingException", "Rate exceeded"),
])
def test_a_genuine_client_error_propagates_instead_of_falling_back(error):
    """A fallback that fires on anything answers with a smaller sample and calls
    it an answer. Note the throughput case names the index in its message: the
    error *code*, not the presence of the string, is what decides."""
    table = FakeTable(legacy_items=[span("OBSERVATORY#invoke_model", "s#a")], index_error=error)

    with pytest.raises(ClientError) as raised:
        om.fetch_model_spans(table, limit=25, now=NOW)

    assert raised.value is error
    assert table.base_table_calls == [], "no silent fallback on a real failure"


@pytest.mark.parametrize("error", [
    RuntimeError(f"{GSI['name']} unreachable"),
    ValueError("boom"),
])
def test_a_non_client_error_propagates_even_when_it_mentions_the_index(error):
    table = FakeTable(legacy_items=[span("OBSERVATORY#invoke_model", "s#a")], index_error=error)

    with pytest.raises(type(error)):
        om.fetch_model_spans(table, limit=25, now=NOW)

    assert table.base_table_calls == []


def test_missing_index_classifier_ignores_exceptions_without_an_error_code():
    assert om._is_missing_index_error(RuntimeError(GSI["name"]), GSI["name"]) is False
    assert om._is_missing_index_error(
        client_error("ValidationException", MISSING_INDEX_MESSAGE), GSI["name"]) is True


# ── The contract itself ───────────────────────────────────────────────────────

def test_vendored_contract_is_the_index_era_contract():
    """A v1 copy sitting next to an index-based reader is the drift this file
    exists to catch: the reader would be querying an index the contract does
    not describe."""
    assert CONTRACT["contract"] == "observatory_metrics_item"
    assert CONTRACT["version"].split(".")[0] == "2", CONTRACT["version"]
    assert set(GSI) >= {"name", "partition_key", "sort_key"}
    for attribute in (GSI["partition_key"], GSI["sort_key"], "operation"):
        assert attribute in CONTRACT["required_attributes"], (
            f"{attribute!r} must be REQUIRED, not recommended: a GSI indexes only items "
            "carrying both of its keys, so an optional key attribute is an optional row"
        )


def test_the_rows_this_reader_depends_on_are_exactly_what_the_contract_requires():
    """check_item() is the writers' test. Running the reader's assumed row shape
    through it is how we know the two halves still describe the same item."""
    conforming = span("SPAN#anything", "2026-09-13T10:00:00.000000#t1")
    assert check_item(conforming, CONTRACT) == []

    without_span_date = dict(conforming)
    del without_span_date[GSI["partition_key"]]
    assert any(p.startswith("I6") for p in check_item(without_span_date, CONTRACT))


def test_legacy_fallback_pk_still_parses_against_the_contracts_pk_grammar():
    """Only the fallback path uses this pk now, but while that path exists the
    string has to be one the contract would recognise."""
    pk_re = re.compile(
        CONTRACT["pk_grammar"]["format"]
        .replace("{namespace}", r"(?P<namespace>[A-Z_]+)")
        .replace("{discriminator}", r"(?P<discriminator>.+)")
    )
    match = pk_re.match(om.MODEL_SPAN_PK)

    assert match is not None
    assert match.group("namespace") in CONTRACT["namespace_registry"]
    assert match.group("discriminator") == om.MODEL_SPAN_OPERATION


def test_namespace_reachability_is_history_under_v2():
    """Formerly this file asserted that DeployWeave was listed as a reader of
    the OBSERVATORY namespace, and that a namespace with no readers was a
    platform gap. v2 retired both questions: reads no longer go through the pk,
    so ``readers_for`` answers only 'which readers would have seen this row
    before the migration'. The registry is kept for reading pre-v2 rows, and
    this test pins that it is marked as such rather than quietly still being
    treated as the reachability model."""
    assert any(inv.startswith("I5:") and "superseded in v2" in inv
               for inv in CONTRACT["invariants"])
    for namespace, entry in CONTRACT["namespace_registry"].items():
        assert entry["status"] == "legacy-informational", namespace

    # Unchanged and still useful for pre-v2 rows only: fails closed on junk.
    assert readers_for("lowercase#invoke_model", CONTRACT) == []
    assert "deployweave:observatory_metrics" in readers_for(om.MODEL_SPAN_PK, CONTRACT)
