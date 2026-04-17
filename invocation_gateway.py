import json
import logging
import os
import threading
import uuid

import boto3
from botocore.exceptions import ClientError

from reservation_manager import commit_tokens, release_reservation, reserve_tokens
from threshold_alerter import check_thresholds

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_TABLE = os.environ.get("AGENT_TABLE", "deployweave-agent-registry")
CONTRACT_TABLE = os.environ.get("CONTRACT_TABLE", "deployweave-contracts")

# Pricing per 1k tokens (USD) — used for cost summary in the response
_COST_PER_1K = {
    "input": 0.003,
    "output": 0.015,
    "cached": 0.0003,
}

_dynamodb = None
_bedrock_agent_runtime = None


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=REGION)
    return _dynamodb


def get_bedrock_agent_runtime():
    global _bedrock_agent_runtime
    if _bedrock_agent_runtime is None:
        _bedrock_agent_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)
    return _bedrock_agent_runtime


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 characters per token."""
    return max(1, len(text) // 4)


def calculate_cost(input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    cost = (
        (input_tokens / 1000) * _COST_PER_1K["input"]
        + (output_tokens / 1000) * _COST_PER_1K["output"]
        + (cached_tokens / 1000) * _COST_PER_1K["cached"]
    )
    return round(cost, 8)


def get_wallet_remaining(contract_id: str) -> dict:
    table = get_dynamodb().Table(CONTRACT_TABLE)
    resp = table.get_item(Key={"contract_id": contract_id})
    contract = resp.get("Item", {})
    wallet = contract.get("token_wallet", {})
    return {
        "input_remaining": wallet.get("input", {}).get("remaining", 0),
        "output_remaining": wallet.get("output", {}).get("remaining", 0),
    }


def _collect_bedrock_response(event_stream) -> tuple[str, int, int, int]:
    """Iterate EventStream, returning (completion_text, input_tokens, output_tokens, cached_tokens)."""
    completion = ""
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    for event in event_stream:
        if "chunk" in event:
            chunk_bytes = event["chunk"].get("bytes", b"")
            completion += chunk_bytes.decode("utf-8", errors="replace")
        if "trace" in event:
            trace = event["trace"]
            orch = trace.get("orchestrationTrace", {})
            model_output = orch.get("modelInvocationOutput", {})
            usage = model_output.get("metadata", {}).get("usage", {})
            input_tokens += usage.get("inputTokens", 0)
            output_tokens += usage.get("outputTokens", 0)
            cached_tokens += usage.get("cachedInputTokens", 0)
    return completion, input_tokens, output_tokens, cached_tokens


def invocation_gateway_handler(event: dict, context) -> dict:
    """
    Real-time token enforcement wrapper for Bedrock agent invocations.

    Validates the contract wallet, reserves tokens, invokes Bedrock, then
    commits actual usage atomically. Returns 402 when the wallet is
    insufficient and 500 on Bedrock errors (releasing the reservation first).
    """
    # --- Parse input ---
    agent_id = event.get("agent_id")
    input_text = event.get("input_text", "")
    session_id = event.get("session_id")
    max_expected_output_tokens = event.get("max_expected_output_tokens", 500)

    if not agent_id or not input_text or not session_id:
        return {"statusCode": 400, "body": "Missing required fields: agent_id, input_text, session_id"}

    ddb = get_dynamodb()

    # --- Lookup agent ---
    agent_resp = ddb.Table(AGENT_TABLE).get_item(Key={"agent_id": agent_id})
    agent = agent_resp.get("Item")
    if not agent:
        return {"statusCode": 404, "body": f"Agent '{agent_id}' not found"}

    contract_id = agent.get("contract_id")
    if not contract_id:
        return {"statusCode": 400, "body": "Agent has no associated contract_id"}

    bedrock_agent_id = agent.get("bedrock_agent_id")

    # --- Lookup contract ---
    contract_resp = ddb.Table(CONTRACT_TABLE).get_item(Key={"contract_id": contract_id})
    contract = contract_resp.get("Item")
    if not contract:
        return {"statusCode": 404, "body": f"Contract '{contract_id}' not found"}

    if contract.get("status") != "active":
        return {"statusCode": 402, "body": f"Contract is not active (status={contract.get('status')})"}

    # --- Pre-flight wallet check ---
    wallet = contract.get("token_wallet", {})
    inp_remaining = wallet.get("input", {}).get("remaining", 0)
    out_remaining = wallet.get("output", {}).get("remaining", 0)
    estimated_input = estimate_tokens(input_text)

    if inp_remaining < estimated_input:
        return {"statusCode": 402, "body": "Insufficient input tokens"}
    if out_remaining < max_expected_output_tokens:
        return {"statusCode": 402, "body": "Insufficient output tokens"}

    # --- Reserve tokens ---
    reservation_id = str(uuid.uuid4())
    try:
        reserve_tokens(contract_id, estimated_input, max_expected_output_tokens, reservation_id)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return {"statusCode": 402, "body": "Concurrent invocation — wallet insufficient or contract inactive"}
        raise

    # --- Invoke Bedrock agent ---
    try:
        response = get_bedrock_agent_runtime().invoke_agent(
            agentId=bedrock_agent_id,
            agentAliasId="TSTALIASID",
            sessionId=session_id,
            inputText=input_text,
        )
        completion, actual_input, actual_output, cached_input = _collect_bedrock_response(
            response["completion"]
        )
    except ClientError as e:
        logger.error("Bedrock invocation failed: %s", e)
        release_reservation(contract_id, reservation_id)
        return {"statusCode": 500, "body": str(e)}

    # --- Commit actual usage ---
    commit_tokens(contract_id, reservation_id, actual_input, actual_output, cached_input)

    # --- Async threshold check (daemon thread — non-blocking) ---
    t = threading.Thread(target=check_thresholds, args=(contract_id,), daemon=True)
    t.start()
    t.join(timeout=0.5)

    return {
        "statusCode": 200,
        "body": completion,
        "usage": {
            "input_tokens": actual_input,
            "output_tokens": actual_output,
            "cached_input_tokens": cached_input,
            "cost_usd": calculate_cost(actual_input, actual_output, cached_input),
        },
        "wallet_remaining": get_wallet_remaining(contract_id),
    }
