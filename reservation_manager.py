import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-east-1")
CONTRACT_TABLE = os.environ.get("CONTRACT_TABLE", "deployweave-contracts")

_dynamodb = None


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=REGION)
    return _dynamodb


def reserve_tokens(
    contract_id: str,
    input_tokens: int,
    output_tokens: int,
    reservation_id: str,
) -> None:
    """
    Atomically reserve input and output tokens on a contract wallet.

    Uses the pre-computed `remaining` field as the gate value (DynamoDB
    ConditionExpression does not support arithmetic, so we keep `remaining`
    updated in every write that touches the wallet).

    Raises ClientError (ConditionalCheckFailedException) when the wallet has
    insufficient remaining tokens or the contract is not active.
    """
    table = get_dynamodb().Table(CONTRACT_TABLE)
    table.update_item(
        Key={"contract_id": contract_id},
        UpdateExpression=(
            "SET token_wallet.#inp.reserved = token_wallet.#inp.reserved + :in_tok,"
            "    token_wallet.#inp.#rem = token_wallet.#inp.#rem - :in_tok,"
            "    token_wallet.#out.reserved = token_wallet.#out.reserved + :out_tok,"
            "    token_wallet.#out.#rem = token_wallet.#out.#rem - :out_tok,"
            "    reservations.#rid = :res_data"
        ),
        ConditionExpression=(
            "token_wallet.#inp.#rem >= :in_tok"
            " AND token_wallet.#out.#rem >= :out_tok"
            " AND #status = :active"
        ),
        ExpressionAttributeNames={
            "#inp": "input",
            "#out": "output",
            "#rem": "remaining",
            "#rid": reservation_id,
            "#status": "status",
        },
        ExpressionAttributeValues={
            ":in_tok": input_tokens,
            ":out_tok": output_tokens,
            ":active": "active",
            ":res_data": {
                "input_reserved": input_tokens,
                "output_reserved": output_tokens,
                "created_at": int(time.time()),
                "status": "pending",
            },
        },
    )


def commit_tokens(
    contract_id: str,
    reservation_id: str,
    actual_input: int,
    actual_output: int,
    cached_input: int = 0,
) -> None:
    """
    Release a reservation and deduct the actual tokens consumed.

    Reads the reserved amounts first so the over-reservation delta can be
    refunded back to `remaining` before removing the reservation record.
    """
    table = get_dynamodb().Table(CONTRACT_TABLE)

    resp = table.get_item(Key={"contract_id": contract_id})
    contract = resp.get("Item", {})
    reservation = contract.get("reservations", {}).get(reservation_id, {})
    reserved_input = reservation.get("input_reserved", actual_input)
    reserved_output = reservation.get("output_reserved", actual_output)

    # Refund = what was reserved but not used (can be 0 or positive)
    refund_input = max(0, reserved_input - actual_input)
    refund_output = max(0, reserved_output - actual_output)

    table.update_item(
        Key={"contract_id": contract_id},
        UpdateExpression=(
            "SET token_wallet.#inp.consumed = token_wallet.#inp.consumed + :actual_in,"
            "    token_wallet.#inp.reserved = token_wallet.#inp.reserved - :res_in,"
            "    token_wallet.#inp.#rem = token_wallet.#inp.#rem + :refund_in,"
            "    token_wallet.#inp.cached = token_wallet.#inp.cached + :cached_in,"
            "    token_wallet.#out.consumed = token_wallet.#out.consumed + :actual_out,"
            "    token_wallet.#out.reserved = token_wallet.#out.reserved - :res_out,"
            "    token_wallet.#out.#rem = token_wallet.#out.#rem + :refund_out"
            " REMOVE reservations.#rid"
        ),
        ExpressionAttributeNames={
            "#inp": "input",
            "#out": "output",
            "#rem": "remaining",
            "#rid": reservation_id,
        },
        ExpressionAttributeValues={
            ":actual_in": actual_input,
            ":actual_out": actual_output,
            ":cached_in": cached_input,
            ":res_in": reserved_input,
            ":res_out": reserved_output,
            ":refund_in": refund_input,
            ":refund_out": refund_output,
        },
    )


def release_reservation(contract_id: str, reservation_id: str) -> None:
    """Release a reservation without deducting (used on Bedrock error or cleanup)."""
    table = get_dynamodb().Table(CONTRACT_TABLE)

    resp = table.get_item(Key={"contract_id": contract_id})
    contract = resp.get("Item", {})
    reservation = contract.get("reservations", {}).get(reservation_id)

    if not reservation:
        logger.warning("Reservation %s not found on %s; skipping release", reservation_id, contract_id)
        return

    reserved_input = reservation.get("input_reserved", 0)
    reserved_output = reservation.get("output_reserved", 0)

    table.update_item(
        Key={"contract_id": contract_id},
        UpdateExpression=(
            "SET token_wallet.#inp.reserved = token_wallet.#inp.reserved - :res_in,"
            "    token_wallet.#inp.#rem = token_wallet.#inp.#rem + :res_in,"
            "    token_wallet.#out.reserved = token_wallet.#out.reserved - :res_out,"
            "    token_wallet.#out.#rem = token_wallet.#out.#rem + :res_out"
            " REMOVE reservations.#rid"
        ),
        ExpressionAttributeNames={
            "#inp": "input",
            "#out": "output",
            "#rem": "remaining",
            "#rid": reservation_id,
        },
        ExpressionAttributeValues={
            ":res_in": reserved_input,
            ":res_out": reserved_output,
        },
    )
