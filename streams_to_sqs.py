import json
import logging
import os

import boto3
from boto3.dynamodb.types import TypeDeserializer

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CLEANUP_QUEUE_URL = os.environ.get("CLEANUP_QUEUE_URL", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")

_sqs = None
_deserializer = TypeDeserializer()


def get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs", region_name=REGION)
    return _sqs


def deserialize_dynamodb_item(raw_item: dict) -> dict:
    """Convert DynamoDB low-level wire format to a plain Python dict."""
    return {k: _deserializer.deserialize(v) for k, v in raw_item.items()}


def build_cleanup_message(item: dict) -> dict:
    return {
        "agent_id": item["agent_id"],
        "bedrock_agent_id": item["bedrock_agent_id"],
    }


def lambda_handler(event: dict, context) -> dict:
    """
    DynamoDB Streams → SQS bridge (pay-per-request).

    Receives REMOVE events from deployweave-agent-registry stream and enqueues
    a cleanup message for each expired/deleted agent.

    Individual record errors are caught and logged (not re-raised) because
    DynamoDB Streams has no partial-batch response — a raised exception retries
    the entire batch repeatedly until it expires.
    """
    records = event.get("Records", [])
    sent = 0
    skipped = 0

    for record in records:
        event_name = record.get("eventName", "")

        if event_name != "REMOVE":
            skipped += 1
            continue

        old_image = record.get("dynamodb", {}).get("OldImage")
        if not old_image:
            logger.warning("REMOVE event missing OldImage; skipping")
            skipped += 1
            continue

        try:
            item = deserialize_dynamodb_item(old_image)
            message = build_cleanup_message(item)

            get_sqs().send_message(
                QueueUrl=CLEANUP_QUEUE_URL,
                MessageBody=json.dumps(message),
                MessageAttributes={
                    "agent_id": {
                        "DataType": "String",
                        "StringValue": message["agent_id"],
                    }
                },
            )
            sent += 1
            logger.info(f"Queued cleanup for agent {message['agent_id']}")

        except KeyError as e:
            # OldImage is missing expected fields (agent_id or bedrock_agent_id)
            logger.error(f"Missing field in stream record OldImage: {e}; skipping")
            skipped += 1
        except Exception as e:
            logger.error(f"Failed to process stream record: {e}; skipping")
            skipped += 1

    return {
        "statusCode": 200,
        "sent_to_sqs": sent,
        "skipped": skipped,
    }
