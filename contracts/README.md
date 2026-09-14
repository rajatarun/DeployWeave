# Vendored contract: `observatory_metrics_item`

`observatory_metrics_item.json` and `conformance.py` in this directory are an
unmodified copy vendored from **mcp-observatory**, the canonical home of the
shared `OBSERVATORY_METRICS` DynamoDB table contract:

https://github.com/rajatarun/mcp-observatory/blob/main/contracts/observatory_metrics_item.json

DeployWeave is a **reader** on that table (`observatory_metrics.py`, which
queries the `OBSERVATORY#invoke_model` partition for model-scoring spans).
`test_shared_table_contract.py` checks, from this vendored file rather than
from retyped constants, that the pk DeployWeave queries still parses against
the contract and that the contract still lists DeployWeave as a reader of
that namespace — so the two sides of this interface cannot silently drift
apart.

## Keeping this in sync

The table has several writers and readers across repositories that cannot see
each other's code, so this file's `version` field is the only thing they can
agree on. When mcp-observatory bumps `observatory_metrics_item.json`'s
version:

1. Update mcp-observatory first (it is canonical).
2. Copy both files here again, unmodified, matching the new version.
3. Do the same in every other repo that vendors this contract (at minimum:
   DeviceWeave, and any other writer/reader of `OBSERVATORY_METRICS`).
4. Re-run each repo's conformance test — a version bump is exactly the kind
   of change that is supposed to break this test if the local writer/reader
   hasn't been updated to match.

Do not hand-edit these files in this repo. If DeployWeave's reader needs a
change the contract doesn't allow, that is a contract change to propose in
mcp-observatory, not a local fork.

`conformance.py` is deliberately dependency-free (no `jsonschema`, no
`boto3`) so it runs anywhere without pulling in extra requirements.
