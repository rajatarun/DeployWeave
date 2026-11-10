# Security notes and threat model

DeployWeave splits **control plane** (MCP tools provisioning agents and adapters) from **data plane** (`POST /invoke` with token wallets). Both share DynamoDB tables and IAM roles defined in `deployment.yaml`.

## Assets

| Asset | Location | Sensitivity |
|-------|----------|-------------|
| Token wallets (`deployweave-contracts`) | DynamoDB | High — spend limits and reservations |
| Agent registry | DynamoDB + Bedrock | Medium — prompts, model IDs, TTL |
| Adapter catalog | DynamoDB + S3 paths | Medium — training artifact locations |
| Invocation gateway URL | API Gateway | Medium — abuse can consume wallet tokens |
| MCP server process | Operator workstation / sidecar | Medium — can provision/delete agents if credentials allow |

## Trust boundaries

```
[MCP client] --stdio/SSE--> [deployweave_mcp.py] --IAM--> [Bedrock Agent API, DynamoDB]
[HTTP client] --HTTPS-->   [API Gateway] --> [invocation_gateway] --> [Bedrock Agent Runtime]
```

- **MCP clients** are trusted to call provisioning tools; restrict who can run the server and which IAM principal it uses.
- **HTTP callers** of `/invoke` must be authenticated at the edge (API keys, IAM authorizer, or private API). The repository ships API Gateway without a built-in API key requirement; operators should add authorizers for production.

## Threats and mitigations

| Threat | Mitigation in code / infra |
|--------|----------------------------|
| Concurrent overspend on a contract | `reservation_manager.py` conditional updates on `remaining`; failed races return HTTP 402 |
| Partial failure after Bedrock invoke | `commit_tokens` / `release_reservation` in gateway; `reservation_cleanup.py` sweeps stale reservations (>5 min) |
| Wallet alert spam | `threshold_alerter.py` uses conditional `alerts_sent` append |
| Orphan Bedrock agents after TTL | Streams → SQS → `cleanup_lambda.py` with DLQ alarm |
| Reconciliation deletes unrelated agents | `should_delete_orphan()` is a stub (always delete); **extend before multi-tenant accounts** — see `orphan_reconciliation.py` |
| Secret leakage | No secrets in source; env vars and OIDC for CI |
| IAM over-permission | Bedrock `Resource: "*"` on management APIs in v1 template — tighten to agent ARNs in hardened deployments |

## Local development

- `python3 -m pytest` runs entirely with mocks — no cloud account required for tests.
- Running MCP tools against real AWS requires credentials with Bedrock and DynamoDB access scoped to DeployWeave tables.

## What this document does not cover

Internal authorization-token designs, employer-specific policy systems, or proprietary signing schemes are **out of scope** for this open-source repository. Token enforcement here is **contract wallet balances** in DynamoDB, not per-request cryptographic authorization tokens.
