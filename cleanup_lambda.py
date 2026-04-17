import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_TABLE = os.environ.get("AGENT_TABLE", "deployweave-agent-registry")

_bedrock_agents = None
_dynamodb = None


def get_bedrock_agents():
    global _bedrock_agents
    if _bedrock_agents is None:
        _bedrock_agents = boto3.client("bedrock-agent", region_name=REGION)
    return _bedrock_agents


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=REGION)
    return _dynamodb


def _delete_bedrock_agent(bedrock_agent_id: str) -> None:
    """Delete a Bedrock agent. Treats 404 (already gone) as success."""
    try:
        get_bedrock_agents().delete_agent(
            agentId=bedrock_agent_id,
            skipResourceInUseCheck=True,
        )
        logger.info(f"Deleted Bedrock agent {bedrock_agent_id}")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ResourceNotFoundException",):
            logger.info(f"Bedrock agent {bedrock_agent_id} already deleted (idempotent)")
        else:
            raise


def _delete_dynamodb_record(agent_id: str) -> None:
    table = get_dynamodb().Table(AGENT_TABLE)
    table.delete_item(Key={"agent_id": agent_id})
    logger.info(f"Deleted DynamoDB record for agent {agent_id}")


def process_record(record: dict) -> None:
    """Process one SQS record. Raises on failure so the caller can report it."""
    body = json.loads(record["body"])
    agent_id = body["agent_id"]
    bedrock_agent_id = body["bedrock_agent_id"]

    logger.info(f"Cleaning up agent {agent_id} (Bedrock ID: {bedrock_agent_id})")

    # Delete Bedrock agent first (idempotent on 404)
    _delete_bedrock_agent(bedrock_agent_id)

    # Delete DynamoDB record last — failure here retains the SQS message for retry
    _delete_dynamodb_record(agent_id)


def lambda_handler(event: dict, context) -> dict:
    """
    SQS-triggered cleanup handler (pay-per-request).

    Each SQS message contains a single expired agent's {agent_id, bedrock_agent_id}.
    Uses ReportBatchItemFailures so only failed messages return to the queue.
    """
    records = event.get("Records", [])
    successful = 0
    failed = 0
    batch_item_failures = []

    for record in records:
        message_id = record["messageId"]
        try:
            process_record(record)
            successful += 1
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Malformed message {message_id}: {e}")
            failed += 1
            batch_item_failures.append({"itemIdentifier": message_id})
        except ClientError as e:
            logger.error(f"AWS error processing {message_id}: {e}")
            failed += 1
            batch_item_failures.append({"itemIdentifier": message_id})
        except Exception as e:
            logger.error(f"Unexpected error processing {message_id}: {e}")
            failed += 1
            batch_item_failures.append({"itemIdentifier": message_id})

    return {
        "statusCode": 200,
        "body": {
            "processed_messages": len(records),
            "successful_cleanups": successful,
            "failed_cleanups": failed,
        },
        "batchItemFailures": batch_item_failures,
    }
