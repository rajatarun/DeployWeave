# DeployWeave — Architecture

## Table of Contents

1. [System Overview](#system-overview)
2. [Component Map](#component-map)
3. [MCP Tool Layer](#mcp-tool-layer)
4. [Token Enforcement Layer](#token-enforcement-layer)
5. [Reservation Pattern](#reservation-pattern)
6. [TTL Cleanup Pipeline](#ttl-cleanup-pipeline)
7. [Orphan Reconciliation](#orphan-reconciliation)
8. [Threshold Alerting](#threshold-alerting)
9. [DynamoDB Schemas](#dynamodb-schemas)
10. [Infrastructure (SAM)](#infrastructure-sam)
11. [IAM Permissions](#iam-permissions)
12. [CI/CD Pipeline](#cicd-pipeline)
13. [Data Flow Diagrams](#data-flow-diagrams)

---

## System Overview

DeployWeave is a two-layer system:

- **MCP Tool Layer** (`deployweave_mcp.py`) — a FastMCP server that exposes four tools to MCP clients (Claude Code, TeamWeave). Handles model selection, agent provisioning, adapter management, and agent lifecycle.
- **Token Enforcement Layer** — a set of Lambda functions that wrap every Bedrock agent invocation with real-time wallet validation, atomic reservation, and actual-usage deduction.

The two layers share the same DynamoDB tables and IAM role but run independently. The MCP server is a long-running process; the enforcement layer is event-driven Lambda.

---

## Component Map

```
┌─────────────────────────────────────────────────────────────────────┐
│  MCP Clients (Claude Code / TeamWeave)                              │
└────────────────────────────┬────────────────────────────────────────┘
                             │ FastMCP (stdio / SSE)
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  deployweave_mcp.py                                                 │
│  ┌──────────────────┐  ┌─────────────────────────────────────────┐  │
│  │  model_selector  │  │  team_provisioner                       │  │
│  │  (A/B testing,   │  │  (batch create ≤5 agents, rollback,     │  │
│  │   latency gate)  │  │   contract_id tag, LoRA validation)     │  │
│  └────────┬─────────┘  └──────────────────┬──────────────────────┘  │
│           │                               │                          │
│  ┌────────┴─────────┐  ┌──────────────────┴──────────────────────┐  │
│  │ adapter_resolver │  │  agent_lifecycle                        │  │
│  │  (LoRA catalog   │  │  (provision / list / get / delete /     │  │
│  │   CRUD + search) │  │   extend_ttl)                           │  │
│  └──────────────────┘  └─────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
         │                          │
         ▼                          ▼
  DynamoDB tables            AWS Bedrock Agents API
  (model-metrics,            (create / delete / get / list)
   adapter-catalog,
   agent-registry)

┌─────────────────────────────────────────────────────────────────────┐
│  Token Enforcement Layer                                            │
│                                                                     │
│  HTTP caller ──► POST /invoke                                       │
│                      │                                              │
│                      ▼                                              │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  invocation_gateway.py  (Lambda + API Gateway)                │  │
│  │                                                               │  │
│  │  1. Lookup agent + contract (agent-registry, contracts)       │  │
│  │  2. Pre-flight wallet check (remaining field)                 │  │
│  │  3. reserve_tokens() — conditional DynamoDB update            │  │
│  │  4. bedrock-agent-runtime.invoke_agent()                      │  │
│  │  5. commit_tokens() — deduct actual, release reservation      │  │
│  │  6. check_thresholds() — async via daemon thread              │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌────────────────────┐   ┌──────────────────────────────────────┐  │
│  │ reservation_       │   │  orphan_reconciliation.py            │  │
│  │ cleanup.py         │   │  (EventBridge: every 1 hour)         │  │
│  │ (EventBridge:      │   │                                      │  │
│  │  every 5 min)      │   │  Bedrock list_agents ↔ DDB scan      │  │
│  │                    │   │  Delete Bedrock-only orphans         │  │
│  │  Release stale     │   │  Delete DDB-only stale records       │  │
│  │  reservations      │   └──────────────────────────────────────┘  │
│  │  (>5 min old)      │                                             │
│  └────────────────────┘   ┌──────────────────────────────────────┐  │
│                           │  threshold_alerter.py                │  │
│                           │  (called inline by gateway)          │  │
│                           │                                      │  │
│                           │  70% / 90% → SNS publish (once each) │  │
│                           │  100% (depleted) → suspend contract  │  │
│                           └──────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  TTL Cleanup Pipeline                                               │
│                                                                     │
│  DynamoDB TTL expiry                                                │
│      ▼                                                              │
│  DynamoDB Streams (OLD_IMAGE)                                       │
│      ▼                                                              │
│  streams_to_sqs.py  (Lambda, filter: REMOVE events only)           │
│      ▼                                                              │
│  SQS: deployweave-cleanup-queue (visibility=1800s, 24h retention)  │
│      ▼                                                              │
│  cleanup_lambda.py  (batch=10, ReportBatchItemFailures)             │
│      ├── bedrock_agents.delete_agent()  (idempotent on 404)        │
│      └── dynamodb.delete_item()                                     │
│              ▼ (after 3 failures)                                   │
│  SQS: deployweave-cleanup-dlq  (14-day retention)                  │
│  CloudWatch Alarm: ≥1 message in DLQ                               │
└─────────────────────────────────────────────────────────────────────┘
```

---

## MCP Tool Layer

### `model_selector`

**File:** `deployweave_mcp.py` (lines ~101–193)

Selects the best Bedrock model for a given task using configurable A/B testing modes.

**Fast path:** `latency_budget_ms ≤ 500` → always returns Haiku (lowest latency).

**A/B modes:**

| Mode | Behaviour |
|---|---|
| `winner` | Queries `deployweave-model-metrics` and picks the model with the highest composite score (success_rate × accuracy_score). Falls back to Sonnet if no data. |
| `split` | Returns a primary and secondary model at a configurable split ratio (default 0.8/0.2). |
| `metric` | Returns all candidate models with their current metric scores for the caller to decide. |

**Models and pricing (USD per 1k tokens):**

| Model | Input | Output |
|---|---|---|
| Haiku (`anthropic.claude-3-haiku-20240307-v1:0`) | $0.00025 | $0.00125 |
| Sonnet (`anthropic.claude-3-5-sonnet-20241022-v2:0`) | $0.003 | $0.015 |
| Opus (`anthropic.claude-3-opus-20240229-v1:0`) | $0.015 | $0.075 |

### `team_provisioner`

**File:** `deployweave_mcp.py` (lines ~274–320)

Provisions a batch of up to 5 Bedrock agents in a loop, with atomic rollback on failure.

**Transactional semantics:**
- Each agent: `bedrock.create_agent()` → success → `dynamodb.put_item()`.
- If `put_item` fails, the just-created Bedrock agent is immediately deleted before the exception propagates.
- If a later agent in the batch fails, all previously provisioned agents in the batch are deleted from Bedrock.
- DynamoDB records written before the failure are not rolled back (TTL cleanup handles orphaned records).

**Bedrock tags applied to each agent:**

```
created_by = "deployweave"
ttl        = "<unix timestamp>"
contract_id = "<contract_id>"  (only when contract_id is provided)
```

**LoRA validation:** `lora_validator.validate_lora_compatibility(model, adapter_id)` is called before the Bedrock API. Compatible prefixes: `amazon.titan-`, `meta.llama`. Claude and Nova models raise `ValueError`.

### `adapter_resolver`

**File:** `deployweave_mcp.py` (lines ~314–399)

Manages the `deployweave-adapter-catalog` table. Three GSIs enable efficient lookups by `base_model`, `task_type`, and `primary_tag`. Multi-tag searches use the primary-tag GSI then filter remaining tags client-side.

### `agent_lifecycle`

**File:** `deployweave_mcp.py` (lines ~406–540)

Single-agent CRUD with TTL management. `delete_agent` removes from both Bedrock and DynamoDB synchronously; the resulting DynamoDB Streams `REMOVE` event is a no-op in the cleanup pipeline (item already gone).

---

## Token Enforcement Layer

### Invocation Gateway

**File:** `invocation_gateway.py`  
**Trigger:** API Gateway `POST /invoke`  
**Timeout:** 60s | **Memory:** 512 MB

Full request lifecycle:

```
1.  Parse {agent_id, input_text, session_id, max_expected_output_tokens}
2.  get_item(AGENT_TABLE, agent_id)          → extract contract_id, bedrock_agent_id
3.  get_item(CONTRACT_TABLE, contract_id)   → validate status == "active"
4.  estimate_tokens(input_text)             → len(input_text) // 4  (rough heuristic)
5.  Pre-check:
        if remaining_input  < estimated_input       → 402
        if remaining_output < max_expected_output   → 402
6.  reservation_id = uuid4()
7.  reserve_tokens(...)                     → DynamoDB conditional update
        ConditionalCheckFailedException     → 402 (concurrent race lost)
8.  invoke_agent(bedrock_agent_id, ...)    → iterate EventStream
        ClientError                         → release_reservation(); 500
9.  commit_tokens(actual_input, actual_output, cached_input)
10. threading.Thread(target=check_thresholds, daemon=True).start()
        join(timeout=0.5)                   → non-blocking, best-effort
11. Return 200 {body, usage, wallet_remaining}
```

**Token estimation note:** The pre-flight check uses a rough estimate (`len / 4`). Actual tokens are deducted in step 9. Over-reservations are automatically refunded in `commit_tokens`.

**Bedrock EventStream parsing:** `invoke_agent` returns a streaming response. Chunks are accumulated into a string; token usage is extracted from `trace.orchestrationTrace.modelInvocationOutput.metadata.usage` events when present. Falls back to 0 if not provided.

---

## Reservation Pattern

Prevents race conditions when multiple concurrent requests target the same contract wallet.

```
Contract DynamoDB item (simplified):

token_wallet.input.remaining   ← gating field (authoritative)
token_wallet.input.reserved    ← sum of all active reservations
token_wallet.input.consumed    ← cumulative actual usage
token_wallet.output.*          ← same structure

reservations: {
  "<reservation-uuid>": {
    input_reserved:  <int>,
    output_reserved: <int>,
    created_at:      <unix timestamp>,
    status:          "pending"
  }
}
```

### `reserve_tokens`

Atomically decrements `remaining` and increments `reserved` in a single `update_item` with:

```python
ConditionExpression = (
    "token_wallet.#inp.#rem >= :in_tok"
    " AND token_wallet.#out.#rem >= :out_tok"
    " AND #status = :active"
)
```

> **Why `remaining` instead of `allocated - consumed - reserved`?**  
> DynamoDB `ConditionExpression` does not support arithmetic operators. A pre-computed `remaining` field is the only way to do a single-round-trip conditional check. Every write that touches the wallet updates `remaining` atomically.

If two concurrent requests both pass the pre-flight check but only one can win the conditional update, the loser receives `ConditionalCheckFailedException` and the gateway returns `402`.

### `commit_tokens`

Reads the reservation record (`get_item`), then in one `update_item`:
- Increments `consumed` by actual usage
- Refunds `remaining` by `(reserved - actual)` — reclaims over-reservation
- Decrements `reserved` by the originally reserved amount
- Removes the reservation map entry (`REMOVE reservations.#rid`)

### `release_reservation`

Same as `commit_tokens` but does not increment `consumed`. Used when the Bedrock invocation failed and no tokens were actually used.

### Reservation expiry

`reservation_cleanup.py` runs every 5 minutes via EventBridge. It scans contracts with a `reservations` map and calls `release_reservation` for any reservation whose `created_at` is more than 300 seconds old. This handles Lambda timeouts or crashes that prevent the gateway from committing or releasing.

---

## TTL Cleanup Pipeline

```
Agent TTL expires
    │
    ▼  (DynamoDB automatic TTL deletion)
DynamoDB Streams — OLD_IMAGE view
    │  Filter: eventName = "REMOVE" (Lambda service-level filter)
    ▼
streams_to_sqs.py
    │  Deserializes DynamoDB wire format
    │  Extracts {agent_id, bedrock_agent_id} from OldImage
    ▼
SQS: deployweave-cleanup-queue
    │  VisibilityTimeout: 1800s (6× Lambda timeout)
    │  Retention: 24 hours
    │  DLQ after 3 failures
    ▼
cleanup_lambda.py
    │  BatchSize: 10, MaxBatchingWindow: 5s
    │  ReportBatchItemFailures (partial batch support)
    ├── bedrock.delete_agent(skipResourceInUseCheck=True)
    │     └── 404 → treat as success (idempotent)
    └── dynamodb.delete_item(agent_id)
          └── failure → mark record as failed (SQS retry)
    │
    ▼ (after maxReceiveCount = 3)
SQS: deployweave-cleanup-dlq
    │  Retention: 14 days
    ▼
CloudWatch Alarm (≥1 message → alert)
```

**Explicit delete** via `agent_lifecycle(operation="delete_agent")` removes from both Bedrock and DynamoDB synchronously. The subsequent Streams `REMOVE` event will find the agent already gone from Bedrock (404 → no-op) and the DDB record already deleted (no-op), making the pipeline idempotent.

---

## Orphan Reconciliation

**File:** `orphan_reconciliation.py`  
**Trigger:** EventBridge every 1 hour  
**Timeout:** 300s

Compares two sets:

| Source | How collected |
|---|---|
| `bedrock_ids` | Paginated `bedrock-agent.list_agents()` → all `agentId` values |
| `ddb_bedrock_ids` | Scan `deployweave-agent-registry` → all `bedrock_agent_id` values |

**Bedrock-only orphans** (`bedrock_ids − ddb_bedrock_ids`): agents that exist in Bedrock but have no DynamoDB record. Deleted via `delete_agent(skipResourceInUseCheck=True)`.

**DynamoDB-only orphans** (`ddb_bedrock_ids − bedrock_ids`): DynamoDB records pointing to Bedrock agents that no longer exist. The stale DynamoDB record is deleted.

**`should_delete_orphan()`** is a stub returning `True` in v1. In production this can be extended to check agent tags (`created_by == "deployweave"`) to avoid touching unrelated agents.

---

## Threshold Alerting

**File:** `threshold_alerter.py`  
**Called by:** `invocation_gateway.py` (daemon thread, non-blocking)

### Flow

```
check_thresholds(contract_id)
    │
    ├── get_item(CONTRACT_TABLE, contract_id)
    │
    ├── input_used_pct  = consumed / allocated × 100
    │   output_used_pct = consumed / allocated × 100
    │   max_used_pct    = max(input_used_pct, output_used_pct)
    │
    ├── for threshold in contract.alert_thresholds (default [70, 90]):
    │       if max_used_pct >= threshold
    │       AND threshold NOT IN contract.alerts_sent:
    │           sns.publish(ALERTS_TOPIC_ARN, ...)
    │           update_item:
    │               SET alerts_sent = list_append(alerts_sent, [threshold])
    │               ConditionExpression: NOT contains(alerts_sent, threshold)
    │               ← ConditionalCheckFailedException ignored (concurrent write)
    │
    └── if remaining <= 0:
            suspend_contract(contract_id)
            SET status = "suspended"
```

### Deduplication guarantee

The `ConditionExpression = NOT contains(alerts_sent, :threshold)` on the atomic write ensures that even if two concurrent invocations both observe the threshold as uncrossed, only one SNS message is published. The second write receives `ConditionalCheckFailedException` and silently skips.

### SNS message format

```json
{
  "contract_id": "contract-uuid",
  "contract_name": "acme-customer-support-2026",
  "threshold_pct": 70,
  "current_usage_pct": 74.5,
  "input_consumed": 745000,
  "input_allocated": 1000000,
  "output_consumed": 370000,
  "output_allocated": 500000,
  "triggered_at": "2026-04-18T10:00:00+00:00"
}
```

Subscribe any endpoint to `deployweave-alerts-<env>` in the AWS SNS console: email, Lambda, SQS, HTTP, or PagerDuty webhook.

---

## DynamoDB Schemas

### `deployweave-agent-registry`

Primary key: `agent_id` (String, HASH)  
GSI: `status-created_at-index` (status HASH, created_at RANGE)  
TTL: `ttl` attribute  
Streams: `OLD_IMAGE`

```json
{
  "agent_id":         "agent-3f2a1b...",
  "bedrock_agent_id": "ABCDE12345",
  "bedrock_agent_arn": "arn:aws:bedrock:us-east-1:123:agent/ABCDE12345",
  "contract_id":      "contract-uuid",
  "name":             "intake-agent",
  "model":            "anthropic.claude-3-5-sonnet-20241022-v2:0",
  "system_prompt":    "You are a customer support agent...",
  "capabilities":     ["classification", "routing"],
  "status":           "active",
  "lora_applied":     false,
  "created_at":       1713360600,
  "created_by":       "team_provisioner",
  "ttl":              1713619800,
  "expiry_timestamp": 1713619800
}
```

### `deployweave-contracts`

Primary key: `contract_id` (String, HASH)

```json
{
  "contract_id":   "contract-uuid-xxx",
  "contract_name": "acme-customer-support-2026",
  "status":        "active",
  "owner_email":   "ops@acme.com",
  "token_wallet": {
    "input": {
      "allocated": 1000000,
      "consumed":    45230,
      "reserved":     2500,
      "cached":       1200,
      "remaining":  952270
    },
    "output": {
      "allocated": 500000,
      "consumed":   12450,
      "reserved":    1000,
      "remaining": 486550
    }
  },
  "reservations": {
    "reservation-uuid-1": {
      "input_reserved":  1500,
      "output_reserved":  500,
      "created_at":      1713360600,
      "status":          "pending"
    }
  },
  "alert_thresholds": [70, 90],
  "alerts_sent": [70]
}
```

**`remaining` field invariant:**  
`remaining = allocated - consumed - reserved` at all times.  
This field is updated atomically in every wallet-touching write, enabling ConditionExpression-based reservation checks without DynamoDB arithmetic.

### `deployweave-adapter-catalog`

Primary key: `adapter_id` (String, HASH)  
GSIs: `base_model-index`, `task_type-index`, `primary_tag-index`

```json
{
  "adapter_id":  "adapter-uuid",
  "base_model":  "amazon.titan-text-express-v1",
  "task_type":   "classification",
  "s3_path":     "s3://my-bucket/adapters/titan-cls-v2.tar.gz",
  "tags":        ["classification", "customer-support", "v2"],
  "primary_tag": "classification",
  "created_at":  1713360600,
  "metadata": {
    "accuracy": 0.91,
    "training_samples": 50000
  }
}
```

### `deployweave-model-metrics`

Primary key: `model_id` (String, HASH) + `timestamp` (Number, RANGE)

```json
{
  "model_id":      "anthropic.claude-3-5-sonnet-20241022-v2:0",
  "timestamp":     1713360600,
  "task_type":     "reasoning",
  "success_rate":  0.97,
  "accuracy_score": 0.89
}
```

---

## Infrastructure (SAM)

**Template:** `deployment.yaml`

### Resources

| Logical ID | Type | Purpose |
|---|---|---|
| `AgentRegistryTable` | `AWS::DynamoDB::Table` | Agent registry with TTL + Streams |
| `AdapterCatalogTable` | `AWS::DynamoDB::Table` | LoRA adapter catalog |
| `ModelMetricsTable` | `AWS::DynamoDB::Table` | A/B model performance metrics |
| `ContractsTable` | `AWS::DynamoDB::Table` | Token wallets + reservations |
| `CleanupQueue` | `AWS::SQS::Queue` | Agent cleanup work queue |
| `CleanupDLQ` | `AWS::SQS::Queue` | Dead-letter queue (3 failures) |
| `AlertsTopic` | `AWS::SNS::Topic` | Token threshold alert fan-out |
| `DeployWeaveLambdaRole` | `AWS::IAM::Role` | Shared role for all Lambda functions |
| `CleanupLambdaFunction` | `AWS::Serverless::Function` | SQS-triggered agent deletion |
| `StreamsToSQSFunction` | `AWS::Serverless::Function` | DynamoDB Streams → SQS bridge |
| `InvocationGatewayFunction` | `AWS::Serverless::Function` | POST /invoke enforcement gateway |
| `ReservationCleanupFunction` | `AWS::Serverless::Function` | Stale reservation sweep (5 min) |
| `OrphanReconciliationFunction` | `AWS::Serverless::Function` | Bedrock ↔ DDB reconciliation (1 hr) |
| `CleanupDLQAlarm` | `AWS::CloudWatch::Alarm` | Alert on DLQ messages |
| `CleanupLambdaErrorAlarm` | `AWS::CloudWatch::Alarm` | Alert on Lambda errors |

### Parameters

| Parameter | Values | Default |
|---|---|---|
| `Environment` | `sandbox`, `dev`, `staging`, `prod` | `dev` |

### Outputs (exported)

| Export | Value |
|---|---|
| `deployweave-agent-registry-table-<env>` | Table name |
| `deployweave-contracts-table-<env>` | Table name |
| `deployweave-adapter-catalog-table-<env>` | Table name |
| `deployweave-model-metrics-table-<env>` | Table name |
| `deployweave-cleanup-queue-url-<env>` | SQS URL |
| `deployweave-alerts-topic-arn-<env>` | SNS ARN |
| `deployweave-invocation-gateway-url-<env>` | API Gateway URL |
| `deployweave-invocation-gateway-arn-<env>` | Lambda ARN |
| `deployweave-reservation-cleanup-arn-<env>` | Lambda ARN |
| `deployweave-orphan-reconciliation-arn-<env>` | Lambda ARN |
| `deployweave-cleanup-lambda-arn-<env>` | Lambda ARN |
| `deployweave-streams-bridge-lambda-arn-<env>` | Lambda ARN |

---

## IAM Permissions

All Lambda functions share `DeployWeaveLambdaRole`.

| Sid | Actions | Resources |
|---|---|---|
| `BedrockAccess` | `InvokeModel`, `InvokeAgent`, `CreateAgent`, `DeleteAgent`, `GetAgent`, `ListAgents`, `PrepareAgent` | `*` |
| `DynamoDBAccess` | Full CRUD + batch + stream operations | All 4 DeployWeave tables + their GSI/stream ARNs |
| `SNSAccess` | `sns:Publish` | `AlertsTopic` only |
| `SQSAccess` | `SendMessage`, `ReceiveMessage`, `DeleteMessage`, `GetQueueAttributes`, `GetQueueUrl` | `CleanupQueue` + `CleanupDLQ` |
| _(managed)_ | `AWSLambdaBasicExecutionRole` | CloudWatch Logs |

> **`bedrock:InvokeAgent`** requires the `bedrock-agent-runtime` API endpoint (distinct from `bedrock-agent` used for management). Both are covered under `Resource: "*"` for v1. In production, scope `InvokeAgent` to specific agent ARNs.

---

## CI/CD Pipeline

**Workflows:** `.github/workflows/deploy.yml` (production + manual), `.github/workflows/sandbox.yml` (feature branches)

### Authentication

All workflows authenticate to AWS using OIDC via `aws-actions/configure-aws-credentials`. No long-lived access keys are stored in GitHub. The assumed role is `arn:aws:iam::239571291755:role/teamweave-github-actions-sam-deployer`.

### `deploy.yml` — Production and manual deploys

```
push to main  ──or──  workflow_dispatch (env: sandbox|dev|staging|prod)
        │
        ▼
  [test job]
  ├── actions/setup-python@v5 (Python 3.11)
  ├── pip install -r requirements.txt pytest
  └── python -m pytest unit_tests.py -v
        │
        ▼ (needs: test)
  [deploy job]
  ├── actions/setup-python@v5
  ├── aws-actions/setup-sam@v2
  ├── aws-actions/configure-aws-credentials@v4  (OIDC)
  ├── sam build --use-container --template deployment.yaml
  └── sam deploy
        --no-confirm-changeset
        --no-fail-on-empty-changeset
        --resolve-s3
        --stack-name deployweave-<env>
        --parameter-overrides Environment=<env>
        --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM
        --region us-east-1
        │
        ▼ (only when env == sandbox)
  [sandbox-teardown job]
  ├── aws-actions/configure-aws-credentials@v4  (OIDC)
  ├── sleep 3600  (hold runner for 1 hour)
  └── sam delete --stack-name deployweave-sandbox --no-prompts
```

### Artifact upload via `--resolve-s3`

SAM must upload Lambda deployment packages (ZIP files of the Python source) to S3 before CloudFormation can reference them. `--resolve-s3` causes SAM to automatically create and manage a bucket named `aws-sam-cli-managed-default-samclisourcebucket-<hash>` in the deploying account and region. SAM deduplicates uploads by content hash, so unchanged functions are skipped on subsequent deployments.

### Environment matrix

| Environment | Trigger | Stack name | Auto-teardown |
|---|---|---|---|
| `prod` | push to `main` | `deployweave-prod` | No |
| `staging` | manual dispatch | `deployweave-staging` | No |
| `dev` | manual dispatch | `deployweave-dev` | No |
| `sandbox` | manual dispatch or feature branch | `deployweave-sandbox` | Yes, 1 hour TTL |

### Stack outputs available post-deploy

The CloudFormation stack exports 14 values (see [Infrastructure (SAM)](#infrastructure-sam) — Outputs table). Downstream stacks can reference them via `Fn::ImportValue: deployweave-<resource>-<env>`.

---

## Data Flow Diagrams

### Happy Path — Token-Gated Invocation

```
HTTP Client                 invocation_gateway        DynamoDB (contracts)    Bedrock
    │                               │                         │                  │
    │─── POST /invoke ──────────────►│                         │                  │
    │                               │── get_item(agent) ──────►│                  │
    │                               │◄── {contract_id, bid} ───│                  │
    │                               │── get_item(contract) ───►│                  │
    │                               │◄── {wallet, status} ─────│                  │
    │                               │                          │                  │
    │                               │── reserve_tokens() ──────►│                  │
    │                               │   (conditional update)   │                  │
    │                               │◄── OK (remaining -= est) ─│                  │
    │                               │                          │                  │
    │                               │─────────────────────────────── invoke_agent ►│
    │                               │◄──────────────────────────── EventStream ───│
    │                               │                          │                  │
    │                               │── commit_tokens() ───────►│                  │
    │                               │   (actual deduction)     │                  │
    │                               │◄── OK ────────────────────│                  │
    │                               │                          │                  │
    │◄── 200 {body, usage} ─────────│                          │                  │
    │                                    [daemon thread]        │                  │
    │                               │── check_thresholds() ────►│                  │
    │                               │   (SNS if crossed)       │                  │
```

### Insufficient Tokens — 402 Path

```
HTTP Client             invocation_gateway          DynamoDB (contracts)
    │                           │                           │
    │─── POST /invoke ──────────►│                           │
    │                           │── pre-check remaining ───►│
    │                           │◄── remaining < required ───│
    │◄── 402 Insufficient ──────│                           │
    │                           │  (no reservation created) │
```

### Concurrent Race — Conditional Failure

```
Request A               Request B               DynamoDB
    │                       │                       │
    │── reserve(500) ───────────────────────────────►│ OK  (remaining: 1000 → 500)
    │── invoke Bedrock ──►  │                       │
    │                       │── reserve(600) ───────►│ FAIL (remaining=500 < 600)
    │                       │◄── ConditionalCheck ───│
    │                       │── return 402 ─────────►│
```

### Stuck Reservation Cleanup

```
reservation_cleanup (EventBridge, 5 min)
    │
    │── scan(FilterExpression: attribute_exists(reservations))
    │       for each contract:
    │           for each reservation:
    │               if now - created_at > 300s:
    │                   release_reservation()
    │                   (remaining += reserved, remove map entry)
```

### Orphan Reconciliation

```
orphan_reconciliation (EventBridge, 1 hour)
    │
    ├── list_agents() → {BID-A, BID-B, BID-orphan}
    ├── scan(agent-registry) → {BID-A → a1, BID-B → a2, BID-gone → a3}
    │
    ├── Bedrock-only: {BID-orphan}
    │       └── delete_agent(BID-orphan)
    │
    └── DDB-only: {BID-gone → a3}
            └── delete_item(agent_id=a3)
```
