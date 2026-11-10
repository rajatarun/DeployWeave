# Extending DeployWeave

DeployWeave is intentionally modular: MCP tools, Lambda workers, and shared DynamoDB tables. Use these extension points instead of forking core wallet or provisioning logic.

## MCP tools (`deployweave_mcp.py`)

New tools follow the FastMCP pattern:

```python
@mcp.tool()
async def my_tool(param: str) -> dict:
    """One-line description shown to MCP clients."""
    ...
```

Conventions:

- Read table names from environment variables (`AGENT_TABLE`, etc.).
- Use `get_dynamodb()` / `get_bedrock_agents()` rather than creating clients per call.
- Return JSON-serializable dicts; raise `ValueError` for caller errors.

After adding a tool, add tests in `unit_tests.py` with `@patch.object(m, "get_dynamodb")` mirroring existing suites.

## Lambda functions

1. Add handler module at repo root (or refactor shared code into importable modules).
2. Wire function in `deployment.yaml` with `Events`, timeout, and `DeployWeaveLambdaRole`.
3. Add tests with mocked boto3 clients (see `unit_tests.py` sections for gateway, cleanup, etc.).

Environment variables are injected by SAM; document new vars in README and `docs/architecture.md`.

## Orphan reconciliation hook

```python
def should_delete_orphan(bedrock_agent_id: str) -> bool:
```

**File:** `orphan_reconciliation.py`

Default returns `True` (delete all Bedrock-only orphans). Production multi-tenant accounts should filter on Bedrock agent tags (`created_by == "deployweave"`) via `get_agent()` before returning `True`.

## LoRA compatibility

**File:** `lora_validator.py`

Extend `LORA_COMPATIBLE_MODELS` prefixes when Bedrock adds LoRA-capable foundation models. `team_provisioner` calls `validate_lora_compatibility` before attaching adapters.

## Observatory telemetry writers

**Files:** `contracts/observatory_metrics_item.json`, `contracts/conformance.py`

External services should emit DynamoDB items that pass `check_item()`. Partition/sort keys and GSI fields must match contract v2.0.0 so `observatory_metrics.fetch_model_spans` can query `SpanTimelineIndex`.

## SAM outputs for downstream stacks

Exported outputs follow `deployweave-<resource>-<env>`. New shared resources should add matching `Outputs` entries for `Fn::ImportValue` consumers.

## Plugin format (informal)

There is no dynamic plugin loader. Extensions are **pull requests** that:

1. Add code + tests
2. Extend IAM in `deployment.yaml` if new AWS APIs are called
3. Update architecture docs and INTEGRATIONS.md status

Planned: formal adapter packages (e.g. OTEL sidecar) may live in separate repos and subscribe to SNS or write observatory spans only.
