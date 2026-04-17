import json
import logging
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-east-1")
CONTRACT_TABLE = os.environ.get("CONTRACT_TABLE", "deployweave-contracts")
ALERTS_TOPIC_ARN = os.environ.get("ALERTS_TOPIC_ARN", "")

_dynamodb = None
_sns = None


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=REGION)
    return _dynamodb


def get_sns():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns", region_name=REGION)
    return _sns


def suspend_contract(contract_id: str) -> None:
    table = get_dynamodb().Table(CONTRACT_TABLE)
    table.update_item(
        Key={"contract_id": contract_id},
        UpdateExpression="SET #status = :suspended",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":suspended": "suspended"},
    )
    logger.warning("Contract %s suspended due to wallet depletion", contract_id)


def _publish_threshold_alert(contract: dict, threshold: int, usage_pct: float) -> None:
    """Publish threshold alert to SNS topic."""
    if not ALERTS_TOPIC_ARN:
        logger.warning("ALERTS_TOPIC_ARN not set; skipping SNS publish for %s", contract["contract_id"])
        return

    wallet = contract.get("token_wallet", {})
    message = json.dumps({
        "contract_id": contract["contract_id"],
        "contract_name": contract.get("contract_name", ""),
        "threshold_pct": threshold,
        "current_usage_pct": round(usage_pct, 2),
        "input_consumed": wallet.get("input", {}).get("consumed", 0),
        "input_allocated": wallet.get("input", {}).get("allocated", 0),
        "output_consumed": wallet.get("output", {}).get("consumed", 0),
        "output_allocated": wallet.get("output", {}).get("allocated", 0),
        "triggered_at": datetime.now(timezone.utc).isoformat(),
    })
    get_sns().publish(
        TopicArn=ALERTS_TOPIC_ARN,
        Subject=f"DeployWeave: {threshold}% token threshold crossed for {contract['contract_id']}",
        Message=message,
    )
    logger.info("Published %d%% threshold alert for contract %s", threshold, contract["contract_id"])


def check_thresholds(contract_id: str) -> None:
    """Check token usage thresholds and send deduplicated SNS alerts."""
    table = get_dynamodb().Table(CONTRACT_TABLE)
    resp = table.get_item(Key={"contract_id": contract_id})
    contract = resp.get("Item")

    if not contract:
        logger.warning("Contract %s not found during threshold check", contract_id)
        return

    if contract.get("status") == "suspended":
        return

    wallet = contract.get("token_wallet", {})
    inp = wallet.get("input", {})
    out = wallet.get("output", {})

    inp_allocated = inp.get("allocated", 0)
    out_allocated = out.get("allocated", 0)
    if inp_allocated == 0 or out_allocated == 0:
        return

    inp_consumed = inp.get("consumed", 0)
    out_consumed = out.get("consumed", 0)
    input_used_pct = (inp_consumed / inp_allocated) * 100
    output_used_pct = (out_consumed / out_allocated) * 100
    max_used_pct = max(input_used_pct, output_used_pct)

    alerts_sent = contract.get("alerts_sent", [])
    for threshold in contract.get("alert_thresholds", [70, 90]):
        if max_used_pct >= threshold and threshold not in alerts_sent:
            _publish_threshold_alert(contract, threshold, max_used_pct)
            # Atomic deduplication: only write if threshold not already in list
            try:
                table.update_item(
                    Key={"contract_id": contract_id},
                    UpdateExpression="SET alerts_sent = list_append(if_not_exists(alerts_sent, :empty), :threshold)",
                    ConditionExpression="NOT contains(alerts_sent, :threshold_val)",
                    ExpressionAttributeValues={
                        ":threshold": [threshold],
                        ":threshold_val": threshold,
                        ":empty": [],
                    },
                )
            except Exception as e:
                # ConditionalCheckFailedException means a concurrent call already wrote it — safe to ignore
                logger.info("Threshold %d already recorded for %s (concurrent write): %s", threshold, contract_id, e)

    # Auto-suspend if wallet fully depleted
    if inp.get("remaining", inp_allocated - inp_consumed) <= 0 or \
       out.get("remaining", out_allocated - out_consumed) <= 0:
        suspend_contract(contract_id)
