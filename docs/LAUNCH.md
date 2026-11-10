# Launch draft (not published)

Use this as a starting point for a blog post, README announcement, or community post. **Do not treat performance numbers below as production benchmarks** unless copied from a labeled synthetic run.

## Draft post

### Title (working)

**DeployWeave: MCP tools and token wallets for Amazon Bedrock Agents**

### Problem

Teams running Bedrock Agents need repeatable ways to:

- Pick models under latency and cost constraints
- Provision short-lived agents with TTL and cleanup
- Attach LoRA adapters from a catalog
- Enforce token budgets so concurrent callers cannot overspend a shared contract

Orchestration frameworks handle *what* to run; something still has to handle *where agents live*, *how they expire*, and *how spend is reserved and committed*.

### What DeployWeave does

DeployWeave provides:

1. A **FastMCP server** with four tools for model selection, batch provisioning, adapter catalog CRUD, and agent lifecycle.
2. A **token enforcement gateway** (`POST /invoke`) that reserves wallet balance, invokes Bedrock Agent Runtime, commits actual usage, and alerts on thresholds.
3. **AWS SAM** infrastructure: DynamoDB tables, Lambdas, SQS cleanup pipeline, EventBridge maintenance jobs.

It does **not** replace a full agent orchestrator (e.g. TeamWeave). It focuses on deployment and enforcement on AWS.

### Who it's for

- Platform engineers building on **Amazon Bedrock Agents**
- Teams already using **MCP** who want provisioning tools beside their IDE
- Operators who need **wallet-style token caps** on agent invocations

### Honest limits

- **Requires AWS** (Bedrock, DynamoDB, Lambda). Local work is limited to unit tests and MCP import without live calls.
- LoRA attach is validated only for **Titan and Llama** base model prefixes today.
- Training flywheel steps through TrainWeave include **manual** promotion and config edits (see README).
- Split A/B mode does not yet use observatory scores for primary/secondary pairing.

### Try it

```bash
pip install -r requirements.txt pytest
python3 -m pytest -q
```

Deploy with SAM when you have an AWS account with Bedrock enabled (see README).

### Screenshot / demo

> **Placeholder:** Add a short screen recording (MCP tool call → model selection result, or pytest run) as `docs/assets/deployweave-demo.gif` before launch.

## Communities where this fits

| Community | Angle |
|-----------|--------|
| **AWS Bedrock / AgentCore** users | Agent TTL, gateway pattern, SAM reference |
| **MCP** ecosystem | Example FastMCP server with real infra backing |
| **Platform / finops** | Token reservation pattern on DynamoDB |
| **Open-source ML ops** | Adapter catalog + LoRA validation hook |

## Synthetic evaluation (optional harness)

If maintainers run the offline harness:

```bash
python3 benchmarks/observatory_scoring_harness.py
```

Results are **synthetic only** (generated spans, not production traffic). See harness output footer for the disclaimer.
