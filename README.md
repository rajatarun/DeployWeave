# DeployWeave

**Provision Amazon Bedrock Agents, manage LoRA adapters, and enforce token wallets — exposed as MCP tools plus a token-gated HTTP invoke path.**

DeployWeave is for **platform and ML engineers on AWS** who already use (or plan to use) **Bedrock Agents** and want MCP-driven provisioning with TTL cleanup and spend limits. It is not a general-purpose agent framework; orchestration lives in separate systems (for example TeamWeave).

> **Demo GIF:** Placeholder — add `docs/assets/deployweave-demo.gif` (MCP tool call or `pytest` run) before a public launch. See [docs/LAUNCH.md](docs/LAUNCH.md).

| Doc | Purpose |
|-----|---------|
| [Architecture](docs/architecture.md) | Full system design, schemas, IAM, CI |
| [Integrations](docs/INTEGRATIONS.md) | MCP, Bedrock, observatory, LangChain, OTEL |
| [Flywheel](docs/design/flywheel.md) | TeamWeave → TrainWeave → DeployWeave loop |
| [Security](SECURITY.md) | Reporting vulnerabilities |
| [Contributing](CONTRIBUTING.md) | Dev setup and PR process |

## What you get

- **MCP server** (`deployweave_mcp.py`) — `model_selector`, `team_provisioner`, `adapter_resolver`, `agent_lifecycle`
- **Token gateway** — `POST /invoke` with reserve → Bedrock → commit wallet pattern
- **SAM stack** (`deployment.yaml`) — DynamoDB, Lambdas, SQS TTL cleanup, SNS alerts

```
MCP Client ──► deployweave_mcp.py ──► Bedrock Agents + DynamoDB
HTTP Client  ──► API Gateway /invoke ──► invocation_gateway (wallet enforcement)
```

**Requires an AWS account** with Bedrock model access for deployment and live MCP calls. **Unit tests and several examples run fully offline** (mocked AWS).

---

## Quickstart (5 minutes)

From a clone of this repository:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt pytest
python3 -m pytest -q               # 132 tests, mocked AWS — expect all passed
python3 examples/model_selector_latency_gate.py
```

Optional scoring harness (synthetic data only):

```bash
python3 benchmarks/observatory_scoring_harness.py
```

### Run the MCP server (local)

```bash
pip install -r requirements.txt
python3 deployweave_mcp.py
```

Add to an MCP host (paths adjusted to your machine):

```json
{
  "mcpServers": {
    "deployweave": {
      "command": "python3",
      "args": ["/path/to/DeployWeave/deployweave_mcp.py"]
    }
  }
}
```

Live tools call AWS APIs when invoked — configure credentials and table names first (see [Environment variables](#environment-variables)).

---

## Core workflow examples

### 1. Pick a model under a latency budget (no AWS for ≤500 ms gate)

MCP tool `model_selector` or the [example script](examples/model_selector_latency_gate.py):

```python
# Equivalent MCP arguments:
# task_type="reasoning", latency_budget_ms=400, ab_mode="winner"
# → selects Claude Haiku when budget ≤ 500 ms
```

### 2. Provision a team of agents (needs AWS)

```json
{
  "tool": "team_provisioner",
  "arguments": {
    "agent_specs": [
      {
        "name": "intake",
        "system_prompt": "You classify support tickets.",
        "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "capabilities": ["classification"]
      }
    ],
    "ttl_hours": 72,
    "use_lora": false,
    "contract_id": "your-contract-uuid"
  }
}
```

### 3. Token-gated invocation (deployed stack)

```bash
curl -sS -X POST "${INVOKE_URL}/invoke" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent-uuid",
    "input_text": "Summarize this ticket.",
    "session_id": "session-1",
    "max_expected_output_tokens": 500
  }'
```

Returns usage and `wallet_remaining` on success; `402` when the contract wallet cannot satisfy the reservation.

---

## Deploy to AWS

**Prerequisites:** Python 3.11+, AWS CLI, SAM CLI, Bedrock model access (Claude Haiku/Sonnet/Opus).

```bash
pip install -r requirements.txt
sam build --use-container --template deployment.yaml
sam deploy \
  --resolve-s3 \
  --stack-name deployweave-dev \
  --parameter-overrides Environment=dev \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --region us-east-1
```

CI on `main` runs tests then deploys to `prod` via GitHub Actions OIDC (see `.github/workflows/deploy.yml`). Use `workflow_dispatch` for `sandbox`, `dev`, or `staging`.

---

## MCP tools (summary)

| Tool | Role |
|------|------|
| `model_selector` | Latency gate + A/B modes (`winner`, `split`, `metric`); optional observatory scoring |
| `team_provisioner` | Batch create up to 5 Bedrock agents with rollback |
| `adapter_resolver` | LoRA catalog list/get/register/search |
| `agent_lifecycle` | Single-agent provision, list, get, delete, extend TTL |

Detailed parameters and response shapes: [docs/architecture.md#mcp-tool-layer](docs/architecture.md#mcp-tool-layer).

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `AGENT_TABLE` | `deployweave-agent-registry` | Agent registry |
| `ADAPTER_TABLE` | `deployweave-adapter-catalog` | LoRA catalog |
| `METRICS_TABLE` | `deployweave-model-metrics` | Task-level A/B metrics |
| `OBSERVATORY_METRICS_TABLE` | — | Optional shared telemetry table for `model_selector` |
| `CONTRACT_TABLE` | `deployweave-contracts` | Token wallets |
| `CLEANUP_QUEUE_URL` | — | SQS cleanup (SAM-injected) |
| `ALERTS_TOPIC_ARN` | — | SNS alerts (SAM-injected) |

Region follows the standard AWS SDK chain (`AWS_REGION` / profile). Lambdas receive region from the runtime.

---

## Tests

```bash
python3 -m pytest -q
python3 -m pytest unit_tests.py -v
```

Tests mock boto3 — no live credentials required.

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).
