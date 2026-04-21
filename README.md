# DeployWeave

Modular MCP tool suite for dynamic model selection, Bedrock agent provisioning, LoRA adapter management, and real-time token enforcement on AWS Bedrock. DeployWeave handles **deployment concerns only** — orchestration and execution logic lives in a separate layer (TeamWeave).

## Overview

```
MCP Client (Claude / TeamWeave)
        │
        ▼
deployweave_mcp.py  (FastMCP server — 4 tools)
        ├── model_selector        ──► DynamoDB: model-metrics
        ├── team_provisioner      ──► Bedrock Agents + DynamoDB: agent-registry
        ├── adapter_resolver      ──► DynamoDB: adapter-catalog
        └── agent_lifecycle       ──► Bedrock Agents + DynamoDB: agent-registry

REST Callers (HTTP)
        │
        ▼  POST /invoke
invocation_gateway.py  (Lambda + API Gateway)
        ├── Pre-check  ──► DynamoDB: contracts (wallet remaining)
        ├── Reserve    ──► Conditional update (reservation pattern)
        ├── Invoke     ──► Bedrock Agent Runtime
        ├── Commit     ──► Deduct actual tokens, release reservation
        └── Alert      ──► threshold_alerter → SNS topic

Background Schedules
        ├── reservation_cleanup.py   (every 5 min)  ──► release stuck reservations
        └── orphan_reconciliation.py (every 1 hour) ──► sync Bedrock ↔ DynamoDB

TTL Cleanup Pipeline
        DynamoDB TTL expiry
            ▼
        DynamoDB Streams (OLD_IMAGE)
            ▼
        streams_to_sqs.py  (Lambda bridge)
            ▼
        SQS: deployweave-cleanup-queue
            ▼
        cleanup_lambda.py
            ├── bedrock_agents.delete_agent()
            └── dynamodb.delete_item()
                    ▼ (on 3 failures)
        SQS: deployweave-cleanup-dlq
```

See [docs/architecture.md](docs/architecture.md) for the full architecture deep-dive.

---

## MCP Tools

### `model_selector`

Selects the optimal Bedrock model based on latency budget and A/B testing mode.

| Parameter | Type | Description |
|---|---|---|
| `task_type` | string | `"classification"`, `"content_generation"`, `"reasoning"`, `"planning"` |
| `latency_budget_ms` | int | Hard latency cap; ≤500 forces Haiku |
| `token_budget` | int | Used for cost estimation |
| `ab_mode` | string | `"winner"` \| `"split"` \| `"metric"` |
| `ab_split_ratio` | float | Primary traffic fraction for split mode (0–1) |

```json
{
  "selected_model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
  "reasoning": "Highest composite score (0.871) across recent reasoning runs",
  "ab_mode": "winner",
  "estimated_cost_usd": 0.0000123
}
```

### `team_provisioner`

Provisions up to 5 Bedrock agents in a single transactional call. If any agent fails, all previously created agents in the batch are rolled back from Bedrock.

| Parameter | Type | Description |
|---|---|---|
| `agent_specs` | list | `[{name, system_prompt, model, capabilities}]` |
| `ttl_hours` | int | Agent TTL in hours (default 72) |
| `use_lora` | bool | Look up and apply a matching LoRA adapter |
| `contract_id` | string | Optional contract to link agents to (stored as tag + DDB field) |

LoRA adapters are only applied when the model is LoRA-compatible (Titan and Llama families). Claude/Nova models raise a `ValueError` at provisioning time.

```json
{
  "provisioned_agents": [
    {
      "agent_id": "agent-3f2a...",
      "bedrock_agent_id": "ABCDE12345",
      "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
      "lora_applied": false,
      "expires_at": "2026-04-24T14:30:00+00:00"
    }
  ],
  "provision_status": "success",
  "total_agents_provisioned": 1
}
```

### `adapter_resolver`

Manages the LoRA adapter catalog in DynamoDB.

| `operation` | Required params | Description |
|---|---|---|
| `list_adapters` | — | List all adapters; filter by `task_type` or `base_model` |
| `get_adapter` | `adapter_id` | Fetch a single adapter |
| `register_adapter` | `adapter_metadata` | Add adapter with `base_model`, `s3_path`, `tags` |
| `search_by_tags` | `tags` | Tag-based search (primary tag uses GSI; extras filtered client-side) |

### `agent_lifecycle`

Manages an individual Bedrock agent's full lifecycle.

| `operation` | Required params | Description |
|---|---|---|
| `provision` | `agent_name`, `system_prompt` | Create single agent + store with TTL |
| `list_agents` | — | List all agents with active/expired classification |
| `get_agent` | `agent_id` | Fetch agent details |
| `delete_agent` | `agent_id` | Synchronous delete from Bedrock + DynamoDB |
| `extend_ttl` | `agent_id`, `ttl_hours` | Push expiry forward |

---

## Token Enforcement (POST /invoke)

All Bedrock invocations for contract-linked agents must go through the `invocation_gateway` Lambda. This prevents overspend by validating the token wallet before invoking Bedrock and deducting actual usage atomically after.

### Request

```
POST https://<api-id>.execute-api.<region>.amazonaws.com/Prod/invoke
Content-Type: application/json

{
  "agent_id":                   "agent-intake-uuid",
  "input_text":                 "User query here",
  "session_id":                 "session-uuid",
  "max_expected_output_tokens": 500
}
```

### Response (200)

```json
{
  "statusCode": 200,
  "body": "<agent completion text>",
  "usage": {
    "input_tokens": 42,
    "output_tokens": 138,
    "cached_input_tokens": 0,
    "cost_usd": 0.00000231
  },
  "wallet_remaining": {
    "input_remaining": 952228,
    "output_remaining": 486412
  }
}
```

### Error codes

| Code | Meaning |
|---|---|
| `400` | Missing required fields or agent has no contract |
| `402` | Insufficient tokens, suspended contract, or concurrent reservation conflict |
| `404` | Agent or contract not found |
| `500` | Bedrock invocation error (reservation is released automatically) |

### Reservation pattern

Concurrent invocations on the same contract are safe:

1. **Reserve** — atomic conditional update; fails if `remaining < required`
2. **Invoke** — Bedrock call happens after reservation is held
3. **Commit** — deducts actual usage; refunds over-reservation back to `remaining`
4. **Release** — called instead of commit on any Bedrock error

Reservations older than 5 minutes are swept by `reservation_cleanup.py`.

---

## Threshold Alerts

The threshold alerter fires once per threshold level per contract, publishing to the SNS topic `deployweave-alerts-<env>`. Subscribe any endpoint (email, Lambda, SQS, PagerDuty) to the SNS topic from the AWS console.

| Threshold | Default | Behaviour |
|---|---|---|
| 70% | Configurable per contract | SNS alert published once |
| 90% | Configurable per contract | SNS alert published once |
| 100% (depletion) | — | Contract auto-suspended; all subsequent invocations return 402 |

---

## Prerequisites

- Python 3.11+
- AWS CLI configured with Bedrock access
- AWS SAM CLI (`pip install aws-sam-cli`)
- Bedrock model access enabled for Claude Haiku, Sonnet, and Opus

## Installation

```bash
pip install -r requirements.txt
```

## Deployment

### CI/CD (recommended)

Push to `main` to deploy to `prod` automatically via GitHub Actions. For other environments, trigger `deploy.yml` manually via `workflow_dispatch` and select the target environment (`sandbox`, `dev`, `staging`, `prod`).

The workflow authenticates to AWS via OIDC (no long-lived credentials) and uses SAM to build and deploy.

### Manual deploy

```bash
sam build --use-container --template deployment.yaml

sam deploy \
  --resolve-s3 \
  --stack-name deployweave-dev \
  --parameter-overrides Environment=dev \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --region us-east-1
```

`--resolve-s3` creates a SAM-managed S3 bucket automatically for Lambda artifact uploads. Replace `dev` with `sandbox`, `staging`, or `prod` as needed.

## Running the MCP Server

```bash
python deployweave_mcp.py
```

Or add to Claude Code MCP settings:

```json
{
  "mcpServers": {
    "deployweave": {
      "command": "python",
      "args": ["/path/to/DeployWeave/deployweave_mcp.py"]
    }
  }
}
```

## Running Tests

```bash
python -m pytest unit_tests.py -v
```

All 85 tests mock AWS calls — no live credentials required.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `AGENT_TABLE` | `deployweave-agent-registry` | DynamoDB agent registry |
| `ADAPTER_TABLE` | `deployweave-adapter-catalog` | DynamoDB adapter catalog |
| `METRICS_TABLE` | `deployweave-model-metrics` | DynamoDB A/B metrics |
| `CLEANUP_QUEUE_URL` | — | SQS cleanup queue URL (injected by SAM) |
| `CONTRACT_TABLE` | `deployweave-contracts` | DynamoDB contract + wallet table |
| `ALERTS_TOPIC_ARN` | — | SNS topic ARN for threshold alerts (injected by SAM) |

`AWS_REGION` is not listed here — Lambda injects it automatically and it cannot be set manually.

## DynamoDB Tables

| Table | Billing | TTL | Streams | Purpose |
|---|---|---|---|---|
| `deployweave-agent-registry` | PAY_PER_REQUEST | `ttl` | OLD_IMAGE | Provisioned agents |
| `deployweave-adapter-catalog` | PAY_PER_REQUEST | — | — | LoRA adapter catalog |
| `deployweave-model-metrics` | PAY_PER_REQUEST | — | — | A/B testing metrics |
| `deployweave-contracts` | PAY_PER_REQUEST | — | — | Token wallets + reservations |

## Lambda Functions

| Function | Trigger | Purpose |
|---|---|---|
| `deployweave-cleanup` | SQS | Delete expired Bedrock agents |
| `deployweave-streams-bridge` | DynamoDB Streams | Bridge TTL events → SQS |
| `deployweave-invocation-gateway` | API Gateway POST /invoke | Token enforcement + Bedrock invoke |
| `deployweave-reservation-cleanup` | EventBridge (5 min) | Release stuck reservations |
| `deployweave-orphan-reconciliation` | EventBridge (1 hour) | Reconcile Bedrock ↔ DynamoDB |

## Cost Estimation

| Service | Estimate | Notes |
|---|---|---|
| DynamoDB | ~$2–5/month | 4 tables, pay-per-request |
| Lambda (all functions) | ~$0.05–0.50/month | Scales with invocation volume |
| API Gateway | ~$3.50/million requests | First 1M/month free on free tier |
| SQS | Free tier | First 1M requests/month free |
| SNS | ~$0.50/million publishes | First 1M/month free |
| Bedrock | Variable | Billed per actual token usage |
| **Fixed overhead** | **~$5–10/month** | Excluding Bedrock usage |

## Security

- IAM role uses least-privilege: Bedrock actions scoped to specific operations, DynamoDB scoped to DeployWeave tables, SNS scoped to the alerts topic
- No credentials in code; all config via environment variables
- DynamoDB conditional updates prevent race conditions on token wallet modifications
- Reservation pattern ensures atomic pre-allocation before any Bedrock call is made
- Alert deduplication prevents SNS spam under concurrent invocations
- GitHub Actions uses OIDC for AWS authentication — no long-lived secrets stored in GitHub
