"""Feasibility eval: does the agent recognise what the product already does?

    uv run python -m evaluation.run_feasibility_eval            # dry run
    uv run python -m evaluation.run_feasibility_eval --run      # needs the Knowledge Brain + API key

24 requirements with accepted verdicts, checked against the documentation. Commercially
the costly mistakes are asymmetric, so they are counted separately:
- "missed saving": a built-in feature judged as custom work (the client overpays);
- "false promise": custom or unclear work judged as already built (we under-quote).
"""

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any

from evaluation.common import kb_key, load_jsonl, load_root_env, p50_p95, save
from scope.agents.feasibility import FeasibilityAgent
from scope.config import settings
from scope.knowledge import HttpKnowledgeClient
from scope.llm import ClaudeLLM
from scope.models import Requirement

HERE = Path(__file__).parent
EXISTING = {"existing_configuration", "existing_with_limitation"}


def grade(case: dict[str, Any], verdict: str) -> dict[str, Any]:
    accept = set(case["accept"])
    return {
        "correct": verdict in accept,
        "missed_saving": bool(accept & EXISTING)
        and not (accept - EXISTING)
        and verdict == "custom_development",
        "false_promise": not (accept & EXISTING) and verdict in EXISTING,
    }


async def run(models: list[str], cases: list[dict[str, Any]]) -> dict[str, Any]:
    kb = HttpKnowledgeClient(settings.kb_url, kb_key(settings.kb_api_key))
    summaries, rows = [], []
    for model in models:
        llm = ClaudeLLM()
        agent = FeasibilityAgent(llm, kb, model, concurrency=1)
        mine = []
        for c in cases:
            req = Requirement(
                id=c["id"],
                description=c["requirement"],
                status="confirmed",
                priority="must",
                origin="client_stated",
                evidence=[],
            )
            t = time.perf_counter()
            r = await agent.check(req)
            row = {
                "model": model,
                "id": c["id"],
                "verdict": r.verdict,
                "kb_status": r.kb_status,
                "how_to": r.how_to,
                "latency_ms": (time.perf_counter() - t) * 1000,
                **grade(c, r.verdict),
            }
            mine.append(row)
            print(f"  {model} {c['id']}: {r.verdict} {'ok' if row['correct'] else 'WRONG'}")
        p50, p95 = p50_p95([r["latency_ms"] for r in mine])
        summaries.append(
            {
                "model": model,
                "accuracy": sum(r["correct"] for r in mine) / len(mine),
                "missed_savings": sum(r["missed_saving"] for r in mine),
                "false_promises": sum(r["false_promise"] for r in mine),
                "kb_unavailable": sum(r["kb_status"] == "unavailable" for r in mine),
                "llm_cost_per_requirement_usd": llm.usage.cost_usd / len(mine),
                "p50_ms": p50,
                "p95_ms": p95,
            }
        )
        rows += mine
    await kb.aclose()
    return {"summary": summaries, "cases": rows, "prompt_version": "feasibility_v1"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="claude-haiku-4-5,claude-sonnet-4-6")
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()
    models = [m for m in args.models.split(",") if m]
    cases = load_jsonl(HERE / "cases" / "feasibility.jsonl")
    print(
        f"{len(cases)} requirements x {len(models)} models = {len(cases) * len(models)} judgements "
        f"(+ {len(cases) * len(models)} Knowledge Brain questions)."
    )
    if not args.run:
        print(
            "Dry run. Add --run to execute (needs Claude on Amazon Bedrock and the Knowledge Brain)."
        )
        return
    load_root_env()
    result = asyncio.run(run(models, cases))
    print(
        "\n| model | accuracy | missed savings | false promises | KB down | $/requirement (excl. KB) | p50 s | p95 s |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for s in result["summary"]:
        print(
            f"| {s['model']} | {s['accuracy']:.0%} | {s['missed_savings']} | {s['false_promises']} | "
            f"{s['kb_unavailable']} | {s['llm_cost_per_requirement_usd']:.5f} | {s['p50_ms'] / 1000:.1f} | {s['p95_ms'] / 1000:.1f} |"
        )
    print(f"saved {save('feasibility', result)}")


if __name__ == "__main__":
    main()
