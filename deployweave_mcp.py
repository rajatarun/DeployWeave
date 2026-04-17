import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from fastmcp import FastMCP

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# --- Constants ---

REGION = os.environ.get("AWS_REGION", "us-east-1")
ADAPTER_TABLE = os.environ.get("ADAPTER_TABLE", "deployweave-adapter-catalog")
METRICS_TABLE = os.environ.get("METRICS_TABLE", "deployweave-model-metrics")
AGENT_TABLE = os.environ.get("AGENT_TABLE", "deployweave-agent-registry")

MODEL_HAIKU = "anthropic.claude-3-haiku-20240307-v1:0"
MODEL_SONNET = "anthropic.claude-3-5-sonnet-20241022-v2:0"
MODEL_OPUS = "anthropic.claude-3-opus-20240229-v1:0"

# Cost per 1k tokens (input, output) in USD
MODEL_COST_MAP: dict[str, dict[str, float]] = {
    MODEL_HAIKU:  {"input": 0.00025, "output": 0.00125},
    MODEL_SONNET: {"input": 0.003,   "output": 0.015},
    MODEL_OPUS:   {"input": 0.015,   "output": 0.075},
}

CANDIDATE_MODELS = [MODEL_HAIKU, MODEL_SONNET, MODEL_OPUS]
DEFAULT_TTL_HOURS = 72
MAX_AGENTS = 5

# --- Lazy boto3 clients ---

_dynamodb = None
_bedrock_runtime = None
_bedrock_agents = None


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=REGION)
    return _dynamodb


def get_bedrock_runtime():
    global _bedrock_runtime
    if _bedrock_runtime is None:
        _bedrock_runtime = boto3.client("bedrock-runtime", region_name=REGION)
    return _bedrock_runtime


def get_bedrock_agents():
    global _bedrock_agents
    if _bedrock_agents is None:
        _bedrock_agents = boto3.client("bedrock-agent", region_name=REGION)
    return _bedrock_agents


# --- FastMCP server ---

mcp = FastMCP("DeployWeave")


# ============================================================
# Tool 1: model_selector
# ============================================================

def _query_model_metrics(model_id: str, task_type: str, limit: int = 10) -> list[dict]:
    table = get_dynamodb().Table(METRICS_TABLE)
    resp = table.query(
        KeyConditionExpression=Key("model_id").eq(model_id),
        FilterExpression=Attr("task_type").eq(task_type),
        ScanIndexForward=False,
        Limit=limit,
    )
    return resp.get("Items", [])


def _score_model(items: list[dict]) -> float:
    if not items:
        return 0.0
    scores = [
        float(item.get("success_rate", 0)) * 0.6 + float(item.get("accuracy_score", 0)) * 0.4
        for item in items
    ]
    return sum(scores) / len(scores)


def _ts_to_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@mcp.tool()
async def model_selector(
    task_type: str,
    latency_budget_ms: int = 1000,
    token_budget: int = 4096,
    ab_mode: str = "winner",
    ab_split_ratio: float = 0.8,
) -> dict:
    """Select the optimal Bedrock model based on task constraints and A/B testing mode."""
    if ab_mode not in ("winner", "split", "metric"):
        raise ValueError(f"ab_mode must be 'winner', 'split', or 'metric'; got '{ab_mode}'")

    ab_split_ratio = max(0.0, min(1.0, ab_split_ratio))

    # Fast path: latency gate
    if latency_budget_ms <= 500:
        cost = (token_budget / 1000) * MODEL_COST_MAP[MODEL_HAIKU]["input"]
        return {
            "selected_model": MODEL_HAIKU,
            "primary_model": MODEL_HAIKU,
            "secondary_model": None,
            "reasoning": f"latency_budget_ms={latency_budget_ms} <= 500: Haiku selected for low latency",
            "ab_mode": ab_mode,
            "estimated_cost_usd": round(cost, 8),
            "metrics": MODEL_COST_MAP[MODEL_HAIKU],
        }

    if ab_mode == "winner":
        scores: dict[str, float] = {}
        for model in CANDIDATE_MODELS:
            items = _query_model_metrics(model, task_type)
            scores[model] = _score_model(items)

        best = max(scores, key=lambda m: scores[m])
        if scores[best] == 0.0:
            best = MODEL_SONNET
            reasoning = "No historical metrics; defaulting to Sonnet"
        else:
            reasoning = f"Highest composite score ({scores[best]:.3f}) across recent {task_type} runs"

        cost = (token_budget / 1000) * MODEL_COST_MAP[best]["input"]
        return {
            "selected_model": best,
            "primary_model": best,
            "secondary_model": None,
            "reasoning": reasoning,
            "ab_mode": "winner",
            "estimated_cost_usd": round(cost, 8),
            "metrics": MODEL_COST_MAP[best],
        }

    if ab_mode == "split":
        primary = MODEL_SONNET
        secondary = MODEL_HAIKU
        cost = (token_budget / 1000) * (
            ab_split_ratio * MODEL_COST_MAP[primary]["input"]
            + (1 - ab_split_ratio) * MODEL_COST_MAP[secondary]["input"]
        )
        return {
            "selected_model": primary,
            "primary_model": primary,
            "secondary_model": secondary,
            "split_ratio": {"primary": ab_split_ratio, "secondary": round(1 - ab_split_ratio, 4)},
            "reasoning": f"Split traffic {ab_split_ratio:.0%}/{1-ab_split_ratio:.0%} between Sonnet and Haiku",
            "ab_mode": "split",
            "estimated_cost_usd": round(cost, 8),
            "metrics": MODEL_COST_MAP[primary],
        }

    # ab_mode == "metric"
    candidates = []
    for model in CANDIDATE_MODELS:
        items = _query_model_metrics(model, task_type)
        candidates.append({
            "model": model,
            "score": round(_score_model(items), 4),
            "sample_count": len(items),
            "cost_per_1k_input": MODEL_COST_MAP[model]["input"],
            "cost_per_1k_output": MODEL_COST_MAP[model]["output"],
        })
    candidates.sort(key=lambda c: c["score"], reverse=True)
    best = candidates[0]["model"] if candidates[0]["score"] > 0 else MODEL_SONNET
    cost = (token_budget / 1000) * MODEL_COST_MAP[best]["input"]
    return {
        "selected_model": best,
        "primary_model": best,
        "secondary_model": None,
        "reasoning": "All candidates returned with historical metrics for inspection",
        "ab_mode": "metric",
        "candidates": candidates,
        "estimated_cost_usd": round(cost, 8),
        "metrics": MODEL_COST_MAP[best],
    }


# ============================================================
# Tool 2: team_provisioner
# ============================================================

def _provision_single_agent(
    spec: dict,
    ttl_hours: int,
    use_lora: bool,
) -> dict:
    name = spec.get("name", f"agent-{uuid.uuid4().hex[:8]}")
    system_prompt = spec.get("system_prompt", "")
    model = spec.get("model", MODEL_SONNET)
    capabilities = spec.get("capabilities", [])

    lora_applied = False
    if use_lora:
        # Best-effort adapter lookup; failure is non-fatal
        try:
            task_type = capabilities[0] if capabilities else "general"
            adapter_result = _search_adapters_by_task(task_type, model)
            lora_applied = bool(adapter_result)
        except Exception:
            logger.warning("LoRA adapter lookup failed; continuing without adapter")

    bedrock_resp = get_bedrock_agents().create_agent(
        agentName=f"deployweave-{name}-{uuid.uuid4().hex[:6]}",
        foundationModel=model,
        description=f"DeployWeave provisioned agent: {name}",
        instruction=system_prompt,
    )
    agent_info = bedrock_resp["agent"]
    bedrock_agent_id = agent_info["agentId"]
    bedrock_agent_arn = agent_info["agentArn"]

    now = int(time.time())
    ttl = now + ttl_hours * 3600
    agent_id = f"agent-{uuid.uuid4()}"

    item = {
        "agent_id": agent_id,
        "bedrock_agent_id": bedrock_agent_id,
        "bedrock_agent_arn": bedrock_agent_arn,
        "name": name,
        "model": model,
        "system_prompt": system_prompt,
        "capabilities": capabilities,
        "status": "active",
        "lora_applied": lora_applied,
        "created_at": now,
        "created_by": "team_provisioner",
        "ttl": ttl,
        "expiry_timestamp": ttl,
    }
    get_dynamodb().Table(AGENT_TABLE).put_item(Item=item)

    return {
        "agent_id": agent_id,
        "name": name,
        "bedrock_agent_id": bedrock_agent_id,
        "bedrock_agent_arn": bedrock_agent_arn,
        "model": model,
        "lora_applied": lora_applied,
        "created_at": _ts_to_iso(now),
        "expires_at": _ts_to_iso(ttl),
    }


def _search_adapters_by_task(task_type: str, base_model: str) -> list[dict]:
    table = get_dynamodb().Table(ADAPTER_TABLE)
    resp = table.query(
        IndexName="task_type-index",
        KeyConditionExpression=Key("task_type").eq(task_type),
        FilterExpression=Attr("base_model").eq(base_model),
        Limit=1,
    )
    return resp.get("Items", [])


@mcp.tool()
async def team_provisioner(
    agent_specs: list,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    use_lora: bool = False,
) -> dict:
    """Provision a set of Bedrock agents from caller-provided specs and store them with a TTL."""
    if not agent_specs:
        raise ValueError("agent_specs must be a non-empty list")

    specs = agent_specs[:MAX_AGENTS]
    provisioned: list[dict] = []
    created_bedrock_ids: list[str] = []

    try:
        for spec in specs:
            result = _provision_single_agent(spec, ttl_hours, use_lora)
            provisioned.append(result)
            created_bedrock_ids.append(result["bedrock_agent_id"])
    except Exception as exc:
        # Rollback: delete all agents provisioned so far
        logger.error(f"Provisioning failed; rolling back {len(created_bedrock_ids)} agents: {exc}")
        for bid in created_bedrock_ids:
            try:
                get_bedrock_agents().delete_agent(agentId=bid, skipResourceInUseCheck=True)
            except ClientError as rollback_err:
                logger.warning(f"Rollback delete of {bid} failed: {rollback_err}")
        raise

    return {
        "provisioned_agents": provisioned,
        "provision_status": "success",
        "total_agents_provisioned": len(provisioned),
    }


# ============================================================
# Tool 3: adapter_resolver
# ============================================================

@mcp.tool()
async def adapter_resolver(
    operation: str,
    task_type: Optional[str] = None,
    tags: Optional[list] = None,
    adapter_id: Optional[str] = None,
    base_model: Optional[str] = None,
    adapter_metadata: Optional[dict] = None,
) -> dict:
    """Manage the LoRA adapter catalog: list, get, register, or search adapters."""
    valid_ops = ("list_adapters", "get_adapter", "register_adapter", "search_by_tags")
    if operation not in valid_ops:
        raise ValueError(f"operation must be one of {valid_ops}; got '{operation}'")

    table = get_dynamodb().Table(ADAPTER_TABLE)

    if operation == "list_adapters":
        if task_type:
            resp = table.query(
                IndexName="task_type-index",
                KeyConditionExpression=Key("task_type").eq(task_type),
            )
        else:
            resp = table.scan(Limit=100)
        items = resp.get("Items", [])
        if base_model:
            items = [i for i in items if i.get("base_model") == base_model]
        return {"operation": operation, "adapters": items, "count": len(items)}

    if operation == "get_adapter":
        if not adapter_id:
            raise ValueError("adapter_id is required for get_adapter")
        resp = table.get_item(Key={"adapter_id": adapter_id})
        item = resp.get("Item")
        if not item:
            raise ValueError(f"Adapter '{adapter_id}' not found")
        return {"operation": operation, "adapters": [item], "count": 1}

    if operation == "register_adapter":
        if not adapter_metadata:
            raise ValueError("adapter_metadata is required for register_adapter")
        if not adapter_metadata.get("s3_path"):
            raise ValueError("adapter_metadata.s3_path is required")
        if not adapter_metadata.get("base_model"):
            raise ValueError("adapter_metadata.base_model is required")

        new_id = str(uuid.uuid4())
        tag_list: list[str] = adapter_metadata.get("tags", [])
        primary_tag = tag_list[0] if tag_list else "untagged"
        now = int(time.time())

        item: dict[str, Any] = {
            "adapter_id": new_id,
            "base_model": adapter_metadata["base_model"],
            "task_type": adapter_metadata.get("task_type", "general"),
            "s3_path": adapter_metadata["s3_path"],
            "tags": set(tag_list) if tag_list else set(),
            "primary_tag": primary_tag,
            "created_at": now,
            "metadata": adapter_metadata,
        }
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(adapter_id)",
        )
        return {"operation": operation, "adapters": [], "count": 0, "adapter_id": new_id}

    # operation == "search_by_tags"
    if not tags:
        raise ValueError("tags must be a non-empty list for search_by_tags")

    primary = tags[0]
    resp = table.query(
        IndexName="primary_tag-index",
        KeyConditionExpression=Key("primary_tag").eq(primary),
    )
    items = resp.get("Items", [])

    # Client-side filter for additional tags
    for extra_tag in tags[1:]:
        items = [i for i in items if extra_tag in (i.get("tags") or set())]

    if base_model:
        items = [i for i in items if i.get("base_model") == base_model]

    return {"operation": operation, "adapters": items, "count": len(items)}


# ============================================================
# Tool 4: agent_lifecycle
# ============================================================

@mcp.tool()
async def agent_lifecycle(
    operation: str,
    agent_name: Optional[str] = None,
    system_prompt: Optional[str] = None,
    model: Optional[str] = None,
    agent_id: Optional[str] = None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> dict:
    """Manage individual Bedrock agent lifecycle: provision, list, get, delete, or extend TTL."""
    valid_ops = ("provision", "list_agents", "get_agent", "delete_agent", "extend_ttl")
    if operation not in valid_ops:
        raise ValueError(f"operation must be one of {valid_ops}; got '{operation}'")

    table = get_dynamodb().Table(AGENT_TABLE)
    now = int(time.time())

    if operation == "provision":
        if not agent_name:
            raise ValueError("agent_name is required for provision")
        if not system_prompt:
            raise ValueError("system_prompt is required for provision")
        effective_model = model or MODEL_SONNET
        bedrock_resp = get_bedrock_agents().create_agent(
            agentName=f"deployweave-{agent_name}-{uuid.uuid4().hex[:6]}",
            foundationModel=effective_model,
            description=f"DeployWeave agent: {agent_name}",
            instruction=system_prompt,
        )
        agent_info = bedrock_resp["agent"]
        bedrock_agent_id = agent_info["agentId"]
        bedrock_agent_arn = agent_info["agentArn"]
        new_agent_id = f"agent-{uuid.uuid4()}"
        ttl = now + ttl_hours * 3600
        item = {
            "agent_id": new_agent_id,
            "bedrock_agent_id": bedrock_agent_id,
            "bedrock_agent_arn": bedrock_agent_arn,
            "name": agent_name,
            "model": effective_model,
            "system_prompt": system_prompt,
            "status": "active",
            "lora_applied": False,
            "created_at": now,
            "created_by": "agent_lifecycle",
            "ttl": ttl,
            "expiry_timestamp": ttl,
        }
        table.put_item(Item=item)
        return {
            "agent_id": new_agent_id,
            "bedrock_agent_id": bedrock_agent_id,
            "bedrock_agent_arn": bedrock_agent_arn,
            "name": agent_name,
            "model": effective_model,
            "status": "provisioned",
            "created_at": _ts_to_iso(now),
            "expires_at": _ts_to_iso(ttl),
        }

    if operation == "list_agents":
        resp = table.scan()
        items = resp.get("Items", [])
        active, expired = [], []
        for item in items:
            exp = int(item.get("expiry_timestamp", 0))
            entry = {
                "agent_id": item["agent_id"],
                "bedrock_agent_id": item.get("bedrock_agent_id"),
                "name": item.get("name"),
                "model": item.get("model"),
                "status": "expired" if exp < now else "active",
                "created_at": _ts_to_iso(int(item["created_at"])) if item.get("created_at") else None,
                "expires_at": _ts_to_iso(exp) if exp else None,
            }
            (expired if exp < now else active).append(entry)
        return {
            "total_agents": len(items),
            "active_agents": len(active),
            "expired_agents": len(expired),
            "agents": active + expired,
        }

    if operation == "get_agent":
        if not agent_id:
            raise ValueError("agent_id is required for get_agent")
        resp = table.get_item(Key={"agent_id": agent_id})
        item = resp.get("Item")
        if not item:
            raise ValueError(f"Agent '{agent_id}' not found")
        exp = int(item.get("expiry_timestamp", 0))
        item["status"] = "expired" if exp < now else "active"
        item["expires_at"] = _ts_to_iso(exp) if exp else None
        return item

    if operation == "delete_agent":
        if not agent_id:
            raise ValueError("agent_id is required for delete_agent")
        resp = table.get_item(Key={"agent_id": agent_id})
        item = resp.get("Item")
        if not item:
            raise ValueError(f"Agent '{agent_id}' not found")
        bedrock_agent_id = item.get("bedrock_agent_id")
        try:
            get_bedrock_agents().delete_agent(
                agentId=bedrock_agent_id, skipResourceInUseCheck=True
            )
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("ResourceNotFoundException",):
                raise
        # delete_item fires the DynamoDB Streams REMOVE event → streams_to_sqs → cleanup_lambda
        table.delete_item(Key={"agent_id": agent_id})
        return {
            "agent_id": agent_id,
            "bedrock_agent_id": bedrock_agent_id,
            "status": "deleted",
        }

    # operation == "extend_ttl"
    if not agent_id:
        raise ValueError("agent_id is required for extend_ttl")
    new_ttl = now + ttl_hours * 3600
    table.update_item(
        Key={"agent_id": agent_id},
        UpdateExpression="SET #ttl = :ttl, expiry_timestamp = :exp",
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={":ttl": new_ttl, ":exp": new_ttl},
        ConditionExpression="attribute_exists(agent_id)",
    )
    return {
        "agent_id": agent_id,
        "status": "ttl_extended",
        "new_expires_at": _ts_to_iso(new_ttl),
        "ttl_hours_added": ttl_hours,
    }


if __name__ == "__main__":
    mcp.run()
