# Contributing to DeployWeave

Thank you for improving DeployWeave. This project is Apache-2.0 licensed (see [LICENSE](LICENSE)).

## What you're contributing to

- **MCP layer:** `deployweave_mcp.py` (FastMCP tools)
- **Enforcement layer:** Lambdas (`invocation_gateway.py`, `reservation_manager.py`, etc.)
- **Infrastructure:** `deployment.yaml` (AWS SAM)
- **Contracts:** `contracts/` (shared observatory telemetry shape)

Architecture background: [docs/architecture.md](docs/architecture.md). Design decisions: [decisions.md](decisions.md).

## Development setup

**Requirements:** Python 3.11+, pip.

```bash
git clone <your-fork-url>
cd DeployWeave
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt pytest
```

Optional for deployment work:

- AWS CLI configured for an account with Bedrock access
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)

## Running tests

```bash
python3 -m pytest -q          # full suite (132+ tests, mocked AWS)
python3 -m pytest -v unit_tests.py
python3 -m pytest -v test_observatory_metrics.py
```

CI runs the same `python3 -m pytest -v` on push (see `.github/workflows/`).

## Style and conventions

- Match existing patterns: lazy boto3 clients, env-var table names, structured logging.
- MCP tools are `async def` functions registered with `@mcp.tool()`.
- Lambda handlers end in `_handler` where applicable.
- Prefer extending `should_delete_orphan`, `lora_validator`, or observatory scoring over duplicating logic.

## Pull requests

1. Branch from `main` using a descriptive name.
2. Keep changes focused; update docs when behavior changes.
3. Ensure `python3 -m pytest` passes.
4. Open a PR with: summary, test results, and any deployment/IAM impact.

Do not commit secrets, real AWS account identifiers in new documentation, or customer data.

## Adding a new MCP tool or Lambda

See [docs/EXTENDING.md](docs/EXTENDING.md).

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Be respectful and constructive.
