# Good first issues (draft for maintainers)

These are scoped tasks derived from the current codebase. Maintainers can copy each block into a GitHub issue.

---

## 1. Tag-aware orphan deletion

**Files:** `orphan_reconciliation.py`, `unit_tests.py`

**Problem:** `should_delete_orphan()` always returns `True`, so reconciliation may delete Bedrock agents not created by DeployWeave in shared AWS accounts.

**Acceptance criteria:**

- Implement tag check via `bedrock-agent.get_agent` for `created_by=deployweave` (or equivalent tags from provisioning).
- Return `False` when tags do not match; log skipped IDs.
- Unit tests cover: tagged deployweave agent (delete), untagged agent (skip), `get_agent` 404 (documented behavior).

---

## 2. Document API Gateway authentication options

**Files:** `deployment.yaml`, `docs/architecture.md`, `README.md`

**Problem:** `POST /invoke` is deployed without a default authorizer.

**Acceptance criteria:**

- Add a short "Hardening" subsection describing IAM authorizer, API keys, or private API patterns (no mandatory template change unless maintainers want a opt-in parameter).
- No secrets or account IDs in docs.

---

## 3. Expand LoRA base model prefixes

**Files:** `lora_validator.py`, `unit_tests.py`

**Problem:** Only `amazon.titan-` and `meta.llama` prefixes are LoRA-compatible per validator.

**Acceptance criteria:**

- When AWS documents new LoRA-capable Bedrock models, add prefixes and tests.
- README limitation paragraph stays accurate.

---

## 4. `adapter_resolver` pagination for large catalogs

**Files:** `deployweave_mcp.py` (`list_adapters` scan path)

**Problem:** `list_adapters` without `task_type` uses `scan(Limit=100)` without pagination.

**Acceptance criteria:**

- Paginate `LastEvaluatedKey` until limit or optional `max_items` param (default 100).
- Tests mock multi-page scan.

---

## 5. Deprecation fix for asyncio in tests

**Files:** `unit_tests.py`

**Problem:** `run()` uses `asyncio.get_event_loop()` (DeprecationWarning on Python 3.11+).

**Acceptance criteria:**

- Use `asyncio.run(coro)` or a session-scoped loop fixture.
- Full pytest suite passes with zero DeprecationWarnings from this helper.

---

## 6. Conformance CLI for observatory items

**Files:** `contracts/conformance.py`, new `examples/` or `scripts/` entry point

**Problem:** Writers in other languages need a quick way to validate sample items.

**Acceptance criteria:**

- `python3 -m contracts.conformance` or small script accepts JSON file path on stdin/file.
- Exit code 0 when valid, non-zero with printed violations.
- Document in `contracts/README.md`.

---

## 7. Split mode respects observatory scores

**Files:** `deployweave_mcp.py` (`model_selector`, `ab_mode="split"`)

**Problem:** Split mode always returns Sonnet/Haiku pair regardless of metrics.

**Acceptance criteria:**

- Optional behavior: primary/secondary from top two scored models when metrics exist; preserve current defaults when no data (documented).
- Tests for winner fallback and split with mocked metrics.

---

## 8. Reservation cleanup metrics

**Files:** `reservation_cleanup.py`, `deployment.yaml` (optional)

**Problem:** Cleanup handler logs counts but does not emit CloudWatch custom metrics.

**Acceptance criteria:**

- Emit `ReservationsReleased` count via `cloudwatch.put_metric_data` (or structured JSON log metric filter).
- Unit test asserts put_metric_data called with expected namespace.

---

## 9. SAM parameter for observatory table import

**Files:** `deployment.yaml`, `deployweave_mcp.py` env in template

**Problem:** `OBSERVATORY_METRICS_TABLE` is only set manually today.

**Acceptance criteria:**

- Optional CloudFormation parameter `ObservatoryMetricsTableName` (empty default).
- When set, inject into MCP Lambda/env for deployed environments.
- Document in README env table.

---

## 10. DLQ message shape documentation

**Files:** `streams_to_sqs.py`, `cleanup_lambda.py`, `docs/architecture.md`

**Problem:** Operators debugging DLQ need an explicit JSON schema for cleanup messages.

**Acceptance criteria:**

- Add `docs/design/cleanup-message-schema.md` with example SQS body fields.
- Cross-link from architecture TTL section.
