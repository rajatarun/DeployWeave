import logging
import os
import time

import boto3
from boto3.dynamodb.conditions import Attr

from reservation_manager import release_reservation

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-east-1")
CONTRACT_TABLE = os.environ.get("CONTRACT_TABLE", "deployweave-contracts")

STALE_THRESHOLD_SECONDS = 300  # 5 minutes

_dynamodb = None


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=REGION)
    return _dynamodb


def reservation_cleanup_handler(event: dict, context) -> dict:
    """
    Release reservations older than 5 minutes (assumed stuck due to Lambda timeout
    or crash before commit/release).
    """
    table = get_dynamodb().Table(CONTRACT_TABLE)
    cutoff = int(time.time()) - STALE_THRESHOLD_SECONDS
    cleaned = 0

    # Paginate through contracts that have a reservations map
    scan_kwargs = {"FilterExpression": Attr("reservations").exists()}
    while True:
        result = table.scan(**scan_kwargs)
        for contract in result.get("Items", []):
            contract_id = contract["contract_id"]
            reservations = contract.get("reservations", {})
            for reservation_id, reservation in reservations.items():
                created_at = reservation.get("created_at", 0)
                if created_at < cutoff:
                    logger.warning(
                        "Releasing stuck reservation %s on contract %s (age=%ds)",
                        reservation_id,
                        contract_id,
                        int(time.time()) - created_at,
                    )
                    try:
                        release_reservation(contract_id, reservation_id)
                        cleaned += 1
                    except Exception as e:
                        logger.error(
                            "Failed to release reservation %s on %s: %s",
                            reservation_id,
                            contract_id,
                            e,
                        )

        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    logger.info("Reservation cleanup complete: %d released", cleaned)
    return {"cleaned_count": cleaned}
