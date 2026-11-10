# Integration opportunities

DeployWeave is an **AWS-native** deployment layer: a FastMCP server plus Lambda-backed token enforcement for Amazon Bedrock Agents. Integrations below are ranked by how naturally they map onto that surface area. **Implemented today** means shipping code or tests exist in this repository; otherwise the row describes a plausible adapter, not a commitment.

| Integration | Fit | Status | Effort (rough) | What an adapter would do |
|-------------|-----|--------|----------------|---------------------------|
| **MCP clients** (Claude Desktop, Claude Code, other MCP hosts) | **High** | **Implemented** | — | Run `deployweave_mcp.py`; four tools: `model_selector`, `team_provisioner`, `adapter_resolver`, `agent_lifecycle`. See [README](../README.md#quickstart-5-minutes). |
| **TeamWeave** (orchestration sibling) | **High** | Partial (documented flywheel) | Medium | TeamWeave calls MCP tools for provisioning; DPO training data flows through TrainWeave into `adapter_resolver`. Manual steps remain for alias publication and `team.json` edits (see README). |
| **Shared observatory telemetry** (cross-product DynamoDB spans) | **High** | **Implemented** | Low to extend | Set `OBSERVATORY_METRICS_TABLE`; `model_selector` scores via `observatory_metrics.py` and `contracts/conformance.py`. Adapter work: emit conformant span items from your writer. |
| **Amazon Bedrock / AgentCore** | **High** | **Implemented** | Medium for AgentCore-specific features | Provisioning uses `bedrock-agent`; invocations use `bedrock-agent-runtime` through `invocation_gateway.py`. AgentCore-specific runtimes would wrap the same tables and wallet semantics. |
| **AWS SAM / CloudFormation** | **High** | **Implemented** | — | `deployment.yaml` defines tables, Lambdas, API Gateway, EventBridge schedules. Export outputs for downstream stacks via `Fn::ImportValue`. |
| **OpenTelemetry** | **Medium** | Not implemented | Medium | Export reservation/commit spans and model_selector decisions as OTEL traces/metrics; correlate with existing SNS wallet alerts. No OTEL code today. |
| **LangChain / LangGraph** | **Medium** | Not implemented | Medium–High | LangGraph nodes could call MCP tools or HTTP `POST /invoke` for token-gated runs; state machine maps to agent lifecycle operations. |
| **OpenAI / Anthropic SDKs (direct)** | **Low** | Not implemented | High | DeployWeave targets Bedrock Agents, not direct vendor APIs. A thin translator could map SDK chat calls to Bedrock, but that is outside current scope. |
| **PagerDuty / Slack via SNS** | **Medium** | Partial | Low | Subscribe SNS topic `deployweave-alerts-<env>` to Lambda, Slack webhook, or PagerDuty Events API. Alert payload shape is documented in `docs/architecture.md`. |
| **TrainWeave** (LoRA training) | **Medium** | Partial (catalog only) | Medium | `register_adapter` + S3 paths; `lora_validator.py` gates Titan/Llama base models. End-to-end attach-after-train is manual today. |

## Highest-value next adapters (not built here)

1. **OTEL exporter on `invocation_gateway`** — surfaces wallet reservation latency, Bedrock errors, and threshold crossings in standard observability stacks (**fit: medium–high**, effort: ~2–3 days).
2. **Observatory span writer template** — minimal Python/Node sample that `put_item`s a conformant row per `contracts/observatory_metrics_item.json` (**fit: high** for multi-repo platforms, effort: ~1 day).
3. **LangGraph toolkit package** — optional PyPI module wrapping the four MCP tools as `@tool` callables with mock mode for local dev (**fit: medium**, effort: ~1 week).

## Extension points in this repo

- **Orphan deletion policy:** `should_delete_orphan()` in `orphan_reconciliation.py` (tag-aware filtering).
- **LoRA compatibility:** `LORA_COMPATIBLE_MODELS` in `lora_validator.py`.
- **Model scoring:** `observatory_metrics.score_models()` and fallback metrics in `deployweave_mcp.py`.
- **Contract conformance:** `contracts/conformance.py` for shared telemetry shape.

See [EXTENDING.md](EXTENDING.md) for conventions when adding tools or Lambdas.
