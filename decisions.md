# DeployWeave — Architecture Decision Records

Decisions are listed roughly in the order they were made. Each entry captures what was decided, why, and what was explicitly ruled out.

---

## ADR-001 — PAY_PER_REQUEST billing for all DynamoDB tables

**Decision:** All four DynamoDB tables use `BillingMode: PAY_PER_REQUEST`.

**Reason:** DeployWeave is a deployment-tooling layer used intermittently by MCP clients and CI pipelines. Traffic is bursty and unpredictable. Provisioned throughput would require capacity planning up-front and risks throttling on spikes or wasted spend during idle periods.

**Rejected:** Provisioned throughput with auto-scaling — adds operational overhead with no benefit at this scale.

---

## ADR-002 — DynamoDB Streams + SQS for the TTL cleanup pipeline

**Decision:** Agent TTL expiry triggers a DynamoDB Streams `REMOVE` event. A bridge Lambda (`streams_to_sqs.py`) forwards the deleted item's data to SQS, which drives the actual Bedrock deletion Lambda (`cleanup_lambda.py`).

**Reason:** Decoupling the deletion trigger from the deletion action gives the pipeline retry semantics and inspectability:
- SQS retries failed deletions up to 3 times before routing to the DLQ.
- The DLQ retains failed messages for 14 days for manual investigation.
- The bridge Lambda is lightweight and stateless; the heavy Bedrock API call is isolated in the cleanup Lambda.

**Rejected:**
- Direct invocation from Streams to Bedrock — no retry buffer, no dead-letter inspection.
- Polling the agent registry on a schedule — races between expiry and the next poll, and requires a timestamp-based secondary scan.

---

## ADR-003 — OLD_IMAGE stream view type on `agent-registry`

**Decision:** `StreamViewType: OLD_IMAGE` on `deployweave-agent-registry`.

**Reason:** When a TTL expiry fires a `REMOVE` event, the item no longer exists in DynamoDB. The only way to recover `agent_id` and `bedrock_agent_id` for the cleanup message is from the stream's `OldImage`. `NEW_IMAGE` and `NEW_AND_OLD_IMAGES` would carry a null new image on deletions; `KEYS_ONLY` would only give the primary key, losing `bedrock_agent_id`.

**Rejected:** `KEYS_ONLY` — `agent_id` alone is sufficient to look up the item, but the item is already deleted by the time the stream event arrives.

---

## ADR-004 — Lambda service-level event filter for REMOVE events on Streams

**Decision:** `FilterCriteria: {Filters: [{Pattern: '{"eventName": ["REMOVE"]}'}]}` on `StreamsToSQSFunction`.

**Reason:** The agent registry receives `INSERT` events on provisioning and `MODIFY` events on TTL extensions. Filtering at the Lambda service level prevents the bridge function from being invoked at all for non-REMOVE records, reducing Lambda invocations and simplifying the function code.

**Rejected:** Filtering inside the Lambda handler — works, but wastes invocations and requires a code path to handle and skip INSERT/MODIFY records.

---

## ADR-005 — `ReportBatchItemFailures` for the SQS cleanup Lambda

**Decision:** `FunctionResponseTypes: [ReportBatchItemFailures]` on `CleanupLambdaFunction`. The handler returns `batchItemFailures` listing only the message IDs that failed.

**Reason:** Without partial batch reporting, any single failed deletion in a batch of 10 causes all 10 messages to return to the queue and retry. This would re-attempt successful deletions, hitting Bedrock's delete API unnecessarily. Partial failure reporting means only genuinely failed messages are retried.

**Rejected:** Synchronous one-at-a-time processing — would work but forfeits the throughput benefit of batching.

---

## ADR-006 — `BisectBatchOnFunctionError` for the DynamoDB Streams bridge

**Decision:** `BisectBatchOnFunctionError: true` on `StreamsToSQSFunction`.

**Reason:** DynamoDB Streams does not support partial batch responses (`ReportBatchItemFailures`). A raised exception retries the entire batch indefinitely until it expires. `BisectBatchOnFunctionError` halves the batch on failure to isolate the problematic record. The bridge Lambda itself swallows per-record errors (logs and skips) rather than raising, so this flag acts as a safety net for unexpected panics.

**Rejected:** Raising on individual record errors — would block the entire shard and delay all subsequent cleanup events until the poisoned record's retention period expires.

---

## ADR-007 — Pre-computed `remaining` field in the token wallet

**Decision:** The `token_wallet` map in `deployweave-contracts` maintains a `remaining` field that is updated atomically in every write: `remaining = allocated - consumed - reserved`.

**Reason:** DynamoDB `ConditionExpression` does not support arithmetic operators. To atomically check `remaining >= required` in a single round trip, the gating value must already exist as a stored attribute. Without `remaining`, a check would require a read followed by a conditional write — a two-round-trip optimistic lock that is harder to reason about and more expensive.

**Rejected:** Read-then-conditional-write (optimistic locking) — two round trips per reservation; higher latency and more complex retry logic.

---

## ADR-008 — Token reservation before Bedrock invocation

**Decision:** The invocation gateway atomically reserves tokens (decrements `remaining`, increments `reserved`) before calling Bedrock, then commits actual usage after the response is received.

**Reason:** Bedrock calls are unpredictable in duration and token consumption. Without a reservation, two concurrent requests on the same contract could both pass the wallet pre-check and collectively overspend. The reservation holds capacity for the duration of the Bedrock call; `commit_tokens` refunds any over-reservation.

**Rejected:** Post-hoc deduction only — creates a window where concurrent requests can overdraw the wallet. No mitigation for abandoned calls (Lambda crash, timeout).

---

## ADR-009 — 5-minute reservation expiry swept by `reservation_cleanup.py`

**Decision:** A scheduled Lambda runs every 5 minutes and releases any reservation whose `created_at` is more than 300 seconds old.

**Reason:** If the invocation gateway Lambda times out, crashes, or is killed before it can call `commit_tokens` or `release_reservation`, the reservation stays in the wallet map indefinitely, blocking future calls from seeing the correct `remaining` value. The 5-minute threshold matches the gateway's 60-second timeout with a comfortable margin.

**Rejected:** DynamoDB TTL on reservations — TTL granularity is typically several minutes and not guaranteed to fire exactly at the expiry time; it is unsuitable as a hard financial control.

---

## ADR-010 — Daemon thread for post-invocation threshold alerting

**Decision:** `threshold_alerter.check_thresholds()` is called in a daemon thread with `join(timeout=0.5)` after the Bedrock response is committed.

**Reason:** Threshold checks require a DynamoDB read and potentially an SNS publish. Running this synchronously before returning would add 50–200 ms to every invocation response time. Making it a daemon thread with a 0.5s join means the gateway returns immediately in the common case (threshold not crossed) while still completing the alert write if it finishes quickly.

**Trade-off:** If the Lambda container is torn down before the thread completes, the alert may not fire. This is acceptable — alerts are informational, and the check will run again on the next invocation. The atomic deduplication write (`NOT contains(alerts_sent, threshold)`) prevents double-alerting.

**Rejected:** Asynchronous Lambda invocation — adds IAM permissions and a second Lambda cold start for a best-effort operation. SQS fan-out — excessive infrastructure for a sub-second check.

---

## ADR-011 — Atomic alert deduplication via conditional DynamoDB write

**Decision:** After publishing an SNS alert for a threshold, the gateway writes the threshold value to `alerts_sent` with `ConditionExpression: NOT contains(alerts_sent, :threshold)`. `ConditionalCheckFailedException` is silently ignored.

**Reason:** Concurrent invocations may both observe the threshold as uncrossed (before either has written). Without deduplication, both would publish to SNS, creating duplicate alerts. The conditional write ensures exactly one SNS message per threshold per contract lifetime.

**Rejected:** Application-level locking — requires a distributed lock, which is more complex than a single conditional DynamoDB write.

---

## ADR-012 — Transactional rollback for `team_provisioner` batch failures

**Decision:** If any agent in a `team_provisioner` batch fails during provisioning, all previously created Bedrock agents in the batch are deleted immediately.

**Reason:** Bedrock agents cost money even when idle. Leaving partially-provisioned agents in Bedrock without corresponding DynamoDB records makes them invisible to DeployWeave and creates orphans that the hourly reconciliation would have to clean up. Rolling back immediately keeps the system consistent and avoids surprise charges.

**Rejected:** Best-effort — leaves orphaned Bedrock agents until the next reconciliation sweep; caller has no way to know which agents succeeded.

---

## ADR-013 — `should_delete_orphan()` stub in orphan reconciliation

**Decision:** `orphan_reconciliation.py` calls a `should_delete_orphan(bedrock_agent_id)` function that currently returns `True` unconditionally.

**Reason:** In production, DeployWeave may share an AWS account with other tooling that also creates Bedrock agents. A hook that checks agent tags (`created_by == "deployweave"`) before deletion prevents reconciliation from destroying unrelated agents. The stub keeps the code path clean for v1 while making the extension point explicit.

---

## ADR-014 — `--resolve-s3` for SAM deployments

**Decision:** The `sam deploy` command in CI uses `--resolve-s3` rather than a hardcoded `--s3-bucket`.

**Reason:** SAM needs an S3 bucket to upload Lambda deployment packages before CloudFormation can reference them. `--resolve-s3` creates and manages a bucket automatically (`aws-sam-cli-managed-default-samclisourcebucket-<hash>`), scoped to the deploying account and region. This avoids requiring the repository or CI configuration to know or create a bucket name.

**Rejected:** Hardcoded `--s3-bucket` — requires pre-creating the bucket and managing its name across environments and accounts. `sam deploy --guided` — interactive and not compatible with CI pipelines.

---

## ADR-015 — `AWS_REGION` not set as a Lambda environment variable

**Decision:** `AWS_REGION` is not declared in the SAM `Globals.Function.Environment.Variables` block.

**Reason:** `AWS_REGION` is a reserved Lambda environment variable injected automatically by the runtime. Explicitly setting it causes a `400 InvalidRequest` from the Lambda API (`Reserved keys used in this request: AWS_REGION`), blocking all function creation and rolling back the entire CloudFormation stack. Code that reads `os.environ.get("AWS_REGION", "us-east-1")` works correctly without the explicit declaration.

---

## ADR-016 — SQS visibility timeout set to 1800s (6× Lambda timeout)

**Decision:** `CleanupQueue.VisibilityTimeout = 1800` seconds.

**Reason:** AWS recommends setting the SQS visibility timeout to at least 6× the consumer Lambda's timeout. The cleanup Lambda has a 300-second timeout, so the minimum recommended value is 1800 seconds. If the visibility timeout is shorter than the Lambda execution time, SQS will make the message visible again while Lambda is still processing it, causing duplicate processing.

---

## ADR-017 — GitHub Actions OIDC for AWS authentication

**Decision:** The deployment workflow uses `aws-actions/configure-aws-credentials` with `role-to-assume` (OIDC) rather than stored `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` secrets.

**Reason:** Long-lived access keys are a persistent credential that can be leaked through log output, secret scanning failures, or GitHub secret exposure events. OIDC issues short-lived tokens scoped to a specific workflow run and IAM role, eliminating the need to rotate static credentials.

---

## ADR-018 — Sandbox stacks auto-teardown after 1 hour

**Decision:** The `sandbox.yml` (and `deploy.yml` sandbox path) include a `sandbox-teardown` job that waits 60 minutes then runs `sam delete` on the sandbox stack.

**Reason:** Sandbox stacks are ephemeral test environments created from feature branches. Leaving them running indefinitely accumulates AWS costs. The 1-hour TTL gives developers enough time to manually test the deployed stack while ensuring cleanup happens automatically even if the developer forgets.

**Trade-off:** The teardown job holds a GitHub Actions runner for the full 60 minutes, consuming runner minutes. This is an acceptable cost for guaranteed cleanup.
