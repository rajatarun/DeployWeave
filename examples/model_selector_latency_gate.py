#!/usr/bin/env python3
"""Demonstrate model_selector fast path (latency <= 500 ms) without AWS."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import deployweave_mcp as m  # noqa: E402


async def main() -> None:
    result = await m.model_selector(
        task_type="classification",
        latency_budget_ms=400,
        token_budget=1024,
        ab_mode="winner",
    )
    print("selected_model:", result["selected_model"])
    print("reasoning:", result["reasoning"])
    assert result["selected_model"] == m.MODEL_HAIKU


if __name__ == "__main__":
    asyncio.run(main())
    print("OK — Haiku selected for low latency budget (no DynamoDB calls).")
