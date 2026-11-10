# Security policy

## Supported versions

Security fixes are applied on the default branch and released through the normal merge/deploy process. Only the latest revision on `main` is considered actively supported for production SAM stacks named `deployweave-<env>`.

| Component | Supported |
|-----------|-----------|
| MCP server (`deployweave_mcp.py`) | Latest on `main` |
| SAM template (`deployment.yaml`) | Latest on `main` |
| Lambda functions in this repo | Latest on `main` |

Older deployed stacks should be updated by redeploying from `main` after pulling security fixes.

## Reporting a vulnerability

**Please do not open a public GitHub issue for security problems.**

Instead, report privately through one of:

1. **GitHub Security Advisories** — use [Report a vulnerability](https://github.com/OWNER/DeployWeave/security/advisories/new) on the repository (replace `OWNER` with the org/user that owns the fork you are using).
2. **Maintainer contact** — if you have an existing private channel with the maintainers, use that.

Include:

- Description of the issue and likely impact
- Steps to reproduce (proof-of-concept if possible)
- Affected paths (MCP tool, Lambda, IAM policy, etc.)
- DeployWeave version or git commit SHA

We aim to acknowledge reports within **5 business days**. We will coordinate disclosure timing with you when a fix is ready.

## Scope

In scope:

- This repository’s application code, SAM/IAM templates, and documented deployment flows
- Token wallet reservation/commit logic and API Gateway `POST /invoke` handler
- Credential handling in CI (OIDC) as configured in `.github/workflows/`

Out of scope:

- Vulnerabilities in AWS Bedrock, DynamoDB, or other AWS services themselves (report those to AWS)
- Misconfigurations in your own AWS accounts (overly broad IAM, public API keys, etc.)

## Safe deployment practices

- DeployWeave **requires an AWS account** with Bedrock model access; it is not a fully offline library.
- Unit tests use mocks and **do not need live AWS credentials**.
- Do not commit access keys, session tokens, or real contract wallet data to the repository.

Further design notes: [docs/design/security-notes.md](design/security-notes.md).
