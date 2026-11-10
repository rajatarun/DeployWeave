# Deployment configuration (GitHub Actions)

GitHub Actions workflows deploy this stack with OIDC. No long-lived AWS access keys are stored in the repository. Configure the items below in **Settings → Secrets and variables → Actions** before running deploy workflows.

> **Git history:** Older commits may still contain real account IDs or ARNs. This document uses placeholder values only. Rotate or review any credentials that were ever committed.

## Repository secrets

| Name | Purpose | Example placeholder |
|------|---------|---------------------|
| `AWS_DEPLOY_ROLE_ARN` | IAM role ARN assumed by `aws-actions/configure-aws-credentials` (trust policy must allow GitHub OIDC for this repository) | `arn:aws:iam::123456789012:role/example-deploy-role` |

## Repository variables

| Name | Purpose | Example placeholder |
|------|---------|---------------------|
| `AWS_REGION` | AWS region for SAM build/deploy/delete and for the `configure-aws-credentials` step | `us-east-1` |

## GitHub Environments

Workflows use GitHub **Environments** (`prod`, `staging`, `dev`, `sandbox`) for approval rules and environment-scoped secrets/variables if you split configuration per stage. Repository-level secrets and variables above are sufficient if every environment shares the same AWS account and role.

## Stack naming (no extra configuration)

| Workflow | Stack name pattern |
|----------|-------------------|
| `deploy.yml` | `deployweave-<Environment>` where `<Environment>` is `sandbox`, `dev`, `staging`, or `prod` (default `prod` on push to `main`) |
| `sandbox.yml` | `deployweave-sb-<branch-slug>` derived from the feature branch name |

## SAM parameters (`deployment.yaml`)

Passed via `--parameter-overrides` in CI:

| Parameter | Set by CI | Description |
|-----------|-----------|-------------|
| `Environment` | Yes | `sandbox`, `dev`, `staging`, or `prod` |
| `ResourceSuffix` | Sandbox workflow only | Suffix for globally named tables/queues to avoid collisions |
| `ObservatoryMetricsTableName` | No (optional) | Shared telemetry DynamoDB table name; leave empty to use DeployWeave-local metrics only |

Local deploy example (replace placeholders):

```bash
sam deploy \
  --resolve-s3 \
  --stack-name deployweave-dev \
  --parameter-overrides Environment=dev \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --region us-east-1
```

## Local development (`.env.example`)

Copy `.env.example` to `.env` for local MCP or scripts. Lambda functions receive table names and ARNs from the SAM stack at deploy time, not from `.env`.
