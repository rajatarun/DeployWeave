#!/usr/bin/env python3
"""Offline synthetic benchmark for observatory model scoring.

Does not call AWS. Prints JSON summary to stdout for maintainers documenting
scoring behavior. Label all reported numbers as SYNTHETIC.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import observatory_metrics  # noqa: E402

MODELS = [
    "anthropic.claude-3-haiku-20240307-v1:0",
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "anthropic.claude-3-opus-20240229-v1:0",
]


def _synthetic_spans(n_per_model: int = 50, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    spans: list[dict] = []
    for model in MODELS:
        for i in range(n_per_model):
            start = base + timedelta(minutes=i)
            latency_ms = rng.randint(200, 2500)
            end = start + timedelta(milliseconds=latency_ms)
            spans.append(
                {
                    "model_id": model,
                    "operation": "invoke_model",
                    "composite_risk_score": round(rng.uniform(0.05, 0.45), 4),
                    "cost_usd": round(rng.uniform(0.0001, 0.01), 6),
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                }
            )
    return spans


def main() -> None:
    spans = _synthetic_spans()
    scores = observatory_metrics.score_models(
        spans, latency_budget_ms=1500, candidates=MODELS
    )
    ranked = sorted(
        (
            {
                "model": m,
                "score": round(scores[m]["score"], 4),
                "mean_latency_ms": scores[m].get("mean_latency_ms"),
                "mean_cost_usd": scores[m].get("mean_cost_usd"),
            }
            for m in MODELS
            if m in scores
        ),
        key=lambda x: x["score"],
        reverse=True,
    )
    out = {
        "label": "SYNTHETIC",
        "span_count": len(spans),
        "latency_budget_ms": 1500,
        "ranked_models": ranked,
    }
    print(json.dumps(out, indent=2))
    print(
        "\n# DISCLAIMER: SYNTHETIC DATA ONLY — not representative of production telemetry.",
        file=__import__("sys").stderr,
    )


if __name__ == "__main__":
    main()
