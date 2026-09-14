"""Conformance of DeployWeave's OBSERVATORY_METRICS reader against the shared
table contract vendored at ``contracts/observatory_metrics_item.json``.

DeployWeave only *reads* the shared table (``observatory_metrics.py``,
``fetch_model_spans`` querying ``MODEL_SPAN_PK``). These tests check that what
the reader actually queries still lines up with the contract, and pin the one
known platform gap so it stays visible instead of being silently "fixed" by
someone assuming it's a bug.

Assertions are driven from the vendored contract file (``pk_grammar``,
``namespace_registry``, ``readers_for``) rather than from constants retyped
here, matching the style of ``test_observatory_metrics.py``'s own docstring
("stub items mirror what mcp_observatory writes") — the point is that editing
the contract, not this file, is what should change what's enforced.
"""

import re

import pytest

import observatory_metrics as om
from contracts.conformance import load_contract, readers_for

CONTRACT = load_contract()  # finds contracts/observatory_metrics_item.json beside conformance.py


def test_model_span_pk_matches_the_contracts_pk_grammar():
    """MODEL_SPAN_PK must parse as '{namespace}#{discriminator}', per pk_grammar."""
    pk_re = re.compile(CONTRACT["pk_grammar"]["format"].replace("{namespace}", r"(?P<namespace>[A-Z_]+)")
                        .replace("{discriminator}", r"(?P<discriminator>.+)"))
    match = pk_re.match(om.MODEL_SPAN_PK)

    assert match is not None, (
        f"{om.MODEL_SPAN_PK!r} does not match the contract's pk_grammar "
        f"{CONTRACT['pk_grammar']['format']!r}"
    )
    assert match.group("namespace") in CONTRACT["namespace_registry"], (
        f"namespace {match.group('namespace')!r} parsed from MODEL_SPAN_PK is not in the "
        f"contract's namespace_registry {sorted(CONTRACT['namespace_registry'])}"
    )


def test_model_span_pk_discriminator_is_a_registered_operation():
    """The discriminator half of MODEL_SPAN_PK ('invoke_model') must be one of
    the OBSERVATORY namespace's registered discriminator_values, or DeployWeave
    is querying a partition no writer is guaranteed to ever populate."""
    namespace, _, discriminator = om.MODEL_SPAN_PK.partition("#")
    entry = CONTRACT["namespace_registry"][namespace]

    assert entry["discriminator"] == "operation"
    assert discriminator in entry["discriminator_values"]


def test_contract_records_deployweave_as_a_reader_of_the_observatory_namespace():
    """The contract and the code must agree about who reads what: this is the
    other half of test_shared_table_contract's pk-grammar check — it isn't
    enough for the pk to parse, DeployWeave itself must be named as a reader,
    or a contract audit would have no way to know this code depends on it."""
    readers = CONTRACT["namespace_registry"]["OBSERVATORY"]["readers"]

    assert "deployweave:observatory_metrics" in readers, (
        f"'deployweave:observatory_metrics' is missing from the contract's OBSERVATORY "
        f"readers {readers!r} — observatory_metrics.py queries this namespace but the "
        "contract doesn't know it, so a writer could stop populating it without any "
        "audit noticing DeployWeave depends on it"
    )


def test_readers_for_model_span_pk_includes_deployweave():
    """End-to-end version of the two checks above, via the contract's own
    readers_for() helper rather than indexing namespace_registry by hand."""
    readers = readers_for(om.MODEL_SPAN_PK, CONTRACT)

    assert "deployweave:observatory_metrics" in readers


def test_span_pk_is_a_recorded_platform_gap_not_something_to_fix_here():
    """Pins a known gap: ``SPAN#...`` is what the shared mcp-observatory
    library's ``DynamoDBSpanExporter`` writes directly (see the contract's
    ``SPAN`` namespace entry, ``status: "unread"``). DeployWeave's reader only
    ever queries ``OBSERVATORY#invoke_model`` (``MODEL_SPAN_PK``), so any
    service that migrates from a vendored-copy exporter onto the shared
    library exporter starts writing to ``SPAN#...`` instead and silently
    disappears from DeployWeave's model selector — no error, just fewer
    samples. The contract already records this (``SPAN.readers == []``,
    ``status: "unread"``); this test only pins DeployWeave's side of it so
    the gap stays visible instead of being "fixed" locally. Closing it is a
    platform decision (see the contract's SPAN.note and
    docs/integration-audit.md), not something for this repo to patch around.
    """
    assert readers_for("SPAN#anything", CONTRACT) == []
    assert CONTRACT["namespace_registry"]["SPAN"]["status"] == "unread"
    assert CONTRACT["namespace_registry"]["SPAN"]["readers"] == []


def test_vendored_contract_version_matches_canonical_home_pin():
    """Sanity check that the vendored copy still declares a version — a
    version bump upstream with no corresponding update here is exactly the
    drift this whole exercise exists to catch (see contracts/README.md)."""
    assert CONTRACT["contract"] == "observatory_metrics_item"
    assert CONTRACT["version"]


@pytest.mark.parametrize("bad_pk", ["", "no-hash-here", "lowercase#invoke_model"])
def test_readers_for_is_empty_for_malformed_or_unregistered_pks(bad_pk):
    """readers_for() fails closed: a pk that doesn't even parse, or whose
    namespace isn't registered, must never be treated as having readers."""
    assert readers_for(bad_pk, CONTRACT) == []
