# DeployWeave

Modular MCP tool suite for dynamic model selection, agent provisioning, LoRA adapter management, and lifecycle management on AWS Bedrock. DeployWeave handles **deployment concerns only** — orchestration and execution logic lives in a separate layer (TeamWeave).

## Architecture

```
MCP Client
    │
    ▼
deployweave_mcp.py  (FastMCP server — 4 tools)
    ├── model_selector      ──► DynamoDB: deployweave-model-metrics
    ├── team_provisioner    ──► Bedrock Agents (create_agent)
    │                           DynamoDB: deployweave-agent-registry (TTL)
    ├── adapter_resolver    ──► DynamoDB: deployweave-adapter-catalog
    └── agent_lifecycle     ──► Bedrock Agents + DynamoDB agent-registry
                                        │
                               TTL expiry / explicit delete
                                        ▼
                           DynamoDB Streams (OLD_IMAGE)
                                        ▼
                           streams_to_sqs.py  (Lambda bridge)
                                        ▼
                           SQS: deployweave-cleanup-queue
                                        ▼
                           cleanup_lambda.py  (pay-per-request)
                             ├── bedrock_agents.delete_agent()
                             └── dynamodb.delete_item()
                                        ▼ (on 3 failures)
                           SQS: deployweave-cleanup-dlq
```

## Tools

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
  "primary_model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
  "secondary_model": null,
  "reasoning": "Highest composite score (0.871) across recent reasoning runs",
  "ab_mode": "winner",
  "estimated_cost_usd": 0.0000123
}
```

### `team_provisioner`

Provisions multiple Bedrock agents from caller-provided specs in a single call (max 5).

| Parameter | Type | Description |
|---|---|---|
| `agent_specs` | list | Array of `{name, system_prompt, model, capabilities}` |
| `ttl_hours` | int | Agent TTL in hours (default 72) |
| `use_lora` | bool | Search adapter catalog and apply matching LoRA adapter |

Automatically rolls back all created agents if any provisioning call fails.

```json
{
  "provisioned_agents": [
    {
      "agent_id": "agent-3f2a...",
      "name": "intake_agent",
      "bedrock_agent_id": "ABCDE12345",
      "bedrock_agent_arn": "arn:aws:bedrock:us-east-1:123456789:agent/ABCDE12345",
      "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
      "lora_applied": false,
      "created_at": "2026-04-17T14:30:00+00:00",
      "expires_at": "2026-04-20T14:30:00+00:00"
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
| `list_adapters` | — | List all adapters; optionally filter by `task_type` or `base_model` |
| `get_adapter` | `adapter_id` | Fetch a single adapter by ID |
| `register_adapter` | `adapter_metadata` | Add adapter with `base_model`, `s3_path`, `tags` |
| `search_by_tags` | `tags` | Search by tag list (primary tag uses GSI; extras filtered client-side) |

### `agent_lifecycle`

Manages an individual Bedrock agent's full lifecycle.

| `operation` | Required params | Description |
|---|---|---|
| `provision` | `agent_name`, `system_prompt` | Create single agent + store with TTL |
| `list_agents` | — | List all agents with active/expired classification |
| `get_agent` | `agent_id` | Fetch agent details; checks expiry client-side |
| `delete_agent` | `agent_id` | Delete from Bedrock + DynamoDB (triggers async Streams cleanup) |
| `extend_ttl` | `agent_id`, `ttl_hours` | Push expiry forward by `ttl_hours` |

## Prerequisites

- Python 3.9+
- AWS CLI configured with Bedrock access in `us-east-1`
- AWS SAM CLI (`pip install aws-sam-cli`)
- Bedrock model access enabled for Claude Haiku, Sonnet, and Opus

## Installation

```bash
pip install -r requirements.txt
```

## Deployment

```bash
sam build
sam deploy --guided
```

Follow the prompts. Key parameters:
- **Stack Name**: `deployweave-dev`
- **AWS Region**: `us-east-1`
- **Environment**: `dev` / `staging` / `prod`

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

All tests mock AWS calls — no live credentials required.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `AWS_REGION` | `us-east-1` | AWS region for all services |
| `AGENT_TABLE` | `deployweave-agent-registry` | DynamoDB table for provisioned agents |
| `ADAPTER_TABLE` | `deployweave-adapter-catalog` | DynamoDB table for LoRA adapters |
| `METRICS_TABLE` | `deployweave-model-metrics` | DynamoDB table for A/B metrics |
| `CLEANUP_QUEUE_URL` | — | SQS URL for cleanup queue (set by SAM) |

## DynamoDB Tables

| Table | Billing | TTL | Streams |
|---|---|---|---|
| `deployweave-agent-registry` | PAY_PER_REQUEST | `ttl` attribute | OLD_IMAGE |
| `deployweave-adapter-catalog` | PAY_PER_REQUEST | — | — |
| `deployweave-model-metrics` | PAY_PER_REQUEST | — | — |

## Cleanup Pipeline

Agents are automatically cleaned up when their TTL expires:

1. DynamoDB TTL removes the item → fires a `REMOVE` stream event
2. `streams_to_sqs.py` reads the `OldImage` and enqueues `{agent_id, bedrock_agent_id}` to SQS
3. `cleanup_lambda.py` deletes the Bedrock agent then the DynamoDB record
4. On 3 consecutive failures: message routes to the DLQ for manual review

Explicit deletion via `agent_lifecycle(operation="delete_agent")` deletes synchronously; the stream event becomes a no-op.

## Cost Estimation (Light Usage)

| Service | Estimate | Notes |
|---|---|---|
| DynamoDB | ~$1–3/month | ~10k reads + 1k writes/day |
| Lambda (cleanup + bridge) | ~$0.02/month | Invoked only on TTL expiry |
| SQS | Free tier | First 1M requests/month free |
| Bedrock (Sonnet) | ~$0.003/1k input tokens | Per provisioning call |
| **Total** | **~$5–15/month** | Scales linearly with agent volume |

## Security

- IAM role uses least-privilege: Bedrock actions scoped to specific operations, DynamoDB scoped to DeployWeave tables only
- No credentials in code; all config via environment variables
- DynamoDB Streams + SQS decoupling isolates cleanup failures with automatic retry and DLQ
