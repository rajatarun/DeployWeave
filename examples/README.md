# Examples

Runnable examples assume Python 3.11+ and repository root on `PYTHONPATH`.

## Offline (no AWS)

| Example | Command |
|---------|---------|
| Full unit test suite | `python3 -m pytest -q` |
| Observatory scoring harness | `python3 benchmarks/observatory_scoring_harness.py` |
| Model selector latency gate | `python3 examples/model_selector_latency_gate.py` |

## With AWS (requires credentials)

| Example | Notes |
|---------|--------|
| MCP server | `python3 deployweave_mcp.py` — register in your MCP host config |
| SAM deploy | See [README](../README.md#deploy-to-aws) |

Examples do not create real Bedrock agents unless you run provisioning tools against live credentials.
