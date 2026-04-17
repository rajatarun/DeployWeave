import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_TABLE = os.environ.get("AGENT_TABLE", "deployweave-agent-registry")

_dynamodb = None
_bedrock_agents = None


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=REGION)
    return _dynamodb


def get_bedrock_agents():
    global _bedrock_agents
    if _bedrock_agents is None:
        _bedrock_agents = boto3.client("bedrock-agent", region_name=REGION)
    return _bedrock_agents


def should_delete_orphan(bedrock_agent_id: str) -> bool:  # noqa: ARG001
    return True


def _list_all_bedrock_agent_ids() -> set:
    """Paginate bedrock list_agents and return a set of all agentIds."""
    client = get_bedrock_agents()
    agent_ids = set()
    kwargs = {}
    while True:
        resp = client.list_agents(**kwargs)
        for summary in resp.get("agentSummaries", []):
            agent_ids.add(summary["agentId"])
        next_token = resp.get("nextToken")
        if not next_token:
            break
        kwargs["nextToken"] = next_token
    return agent_ids


def _list_all_dynamodb_agents() -> dict:
    """Return {bedrock_agent_id: agent_id} for all records in the agent registry."""
    table = get_dynamodb().Table(AGENT_TABLE)
    mapping = {}
    kwargs = {"ProjectionExpression": "agent_id, bedrock_agent_id"}
    while True:
        result = table.scan(**kwargs)
        for item in result.get("Items", []):
            bid = item.get("bedrock_agent_id")
            if bid:
                mapping[bid] = item["agent_id"]
        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return mapping


def orphan_reconciliation_handler(event: dict, context) -> dict:
    """
    Hourly sweep that reconciles Bedrock agents against DynamoDB records.

    Bedrock-only orphans: exist in Bedrock but not in DynamoDB — deleted from Bedrock.
    DynamoDB-only orphans: bedrock_agent_id in DynamoDB but agent gone from Bedrock — record deleted.
    """
    bedrock_ids = _list_all_bedrock_agent_ids()
    ddb_map = _list_all_dynamodb_agents()  # {bedrock_agent_id: agent_id}
    ddb_bedrock_ids = set(ddb_map.keys())

    bedrock_orphans_deleted = 0
    dynamo_stale_deleted = 0

    # Bedrock-only orphans (not tracked in DynamoDB)
    for bid in bedrock_ids - ddb_bedrock_ids:
        logger.warning("Orphan found in Bedrock (not in DynamoDB): %s", bid)
        if should_delete_orphan(bid):
            try:
                get_bedrock_agents().delete_agent(agentId=bid, skipResourceInUseCheck=True)
                bedrock_orphans_deleted += 1
                logger.info("Deleted Bedrock orphan: %s", bid)
            except ClientError as e:
                logger.error("Failed to delete Bedrock orphan %s: %s", bid, e)

    # DynamoDB-only orphans (bedrock_agent_id not found in Bedrock)
    table = get_dynamodb().Table(AGENT_TABLE)
    for bid, agent_id in ddb_map.items():
        if bid not in bedrock_ids:
            logger.warning("Stale DynamoDB record: agent_id=%s bedrock_agent_id=%s", agent_id, bid)
            try:
                table.delete_item(Key={"agent_id": agent_id})
                dynamo_stale_deleted += 1
                logger.info("Deleted stale DynamoDB record: %s", agent_id)
            except ClientError as e:
                logger.error("Failed to delete stale DDB record %s: %s", agent_id, e)

    logger.info(
        "Orphan reconciliation complete: bedrock_deleted=%d dynamo_deleted=%d",
        bedrock_orphans_deleted,
        dynamo_stale_deleted,
    )
    return {
        "bedrock_orphans_deleted": bedrock_orphans_deleted,
        "dynamo_stale_records_deleted": dynamo_stale_deleted,
    }
