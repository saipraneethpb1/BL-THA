"""Vision eval: which model should describe the 5,040 help-center screenshots?

    uv run python -m evaluation.run_vision_eval                  # dry run
    uv run python -m evaluation.run_vision_eval --run            # spends API credits

26 real screenshots, each with 1-3 labels read by eye from the image (66 checks). A model
scores a label when its stored description (the exact text that gets indexed) contains
it, ignoring case and punctuation. Two cases (V25, V26) are screenshots whose setting
names appear nowhere in the article text: the reason image understanding exists.
Read-only: nothing is written to the knowledge base.
"""

import argparse
import asyncio
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from evaluation.common import p50_p95, save, table
from kb.answer.llm import PRICES
from kb.config import settings
from kb.ingestion.vision import ClaudeImageDescriber, ImageJob

HERE = Path(__file__).parent
DEFAULT_MODELS = "claude-haiku-5-5,claude-sonnet-5-5"


def norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9_]+", text.lower()))


def label_hits(text: str, labels: list[list[str]]) -> list[bool]:
    t = norm(text)
    return [any(norm(alt) in t for alt in group) for group in labels]


def load_cases() -> list[dict[str, Any]]:
    lines = (HERE / "vision_cases.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def job_for(case: dict[str, Any]) -> ImageJob:
    """Same context the production describer gets: article title, section, nearby text."""
    with closing(sqlite3.connect(settings.db_path)) as c:
        row = c.execute(
            """SELECT i.section, i.context_before, i.context_after FROM images i
               WHERE i.src = ? LIMIT 1""",
            (case["src"],),
        ).fetchone()
    section, before, after = row if row else (None, "", "")
    return ImageJob(case["src"], case["article_title"], section, before or "", after or "")


async def run(models: list[str], cases: list[dict[str, Any]]) -> dict[str, Any]:
    summaries, rows = [], []
    for model in models:
        describer = ClaudeImageDescriber(model)
        mine = []
        for case in cases:
            t = time.perf_counter()
            try:
                r = await describer.describe(job_for(case))
                text = r.description.as_text()
                tokens = (r.input_tokens, r.output_tokens)
                error = None
            except Exception as exc:  # a failed image counts as zero labels found
                text, tokens, error = "", (0, 0), repr(exc)
            hits = label_hits(text, case["labels"])
            price_in, price_out = PRICES.get(model, (0, 0))
            row = {
                "model": model,
                "id": case["id"],
                "text": text,
                "hits": hits,
                "error": error,
                "cost_usd": (tokens[0] * price_in + tokens[1] * price_out) / 1_000_000,
                "latency_ms": (time.perf_counter() - t) * 1000,
            }
            mine.append(row)
            print(f"  {model} {case['id']}: {sum(hits)}/{len(hits)}{' ERROR' if error else ''}")
        all_hits = [h for r in mine for h in r["hits"]]
        p50, p95 = p50_p95([r["latency_ms"] for r in mine])
        only_in_image = [r for r in mine if r["id"] in ("V25", "V26")]
        summaries.append(
            {
                "model": model,
                "label_recall": sum(all_hits) / len(all_hits),
                "images_fully_read": sum(all(r["hits"]) for r in mine) / len(mine),
                "image_only_answers_found": sum(all(r["hits"]) for r in only_in_image),
                "errors": sum(r["error"] is not None for r in mine),
                "cost_per_image_usd": sum(r["cost_usd"] for r in mine) / len(mine),
                "projected_cost_5040_images_usd": 5040
                * sum(r["cost_usd"] for r in mine)
                / len(mine),
                "p50_ms": p50,
                "p95_ms": p95,
            }
        )
        rows += mine
    return {"summary": summaries, "cases": rows, "prompt_version": "vision_v1"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=DEFAULT_MODELS)
    ap.add_argument("--run", action="store_true", help="actually call the API (costs money)")
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    cases = load_cases()
    print(
        f"{len(cases)} images ({sum(len(c['labels']) for c in cases)} label checks) x {len(models)} models "
        f"= {len(cases) * len(models)} vision calls."
    )
    if not args.run:
        print("Dry run. Add --run to execute.")
        return
    result = asyncio.run(run(models, cases))
    path = save("vision", result)
    print(
        table(
            [
                "model",
                "labels found",
                "images fully read",
                "image-only answers (of 2)",
                "errors",
                "$/image",
                "$ for all 5,040",
                "p50 ms",
                "p95 ms",
            ],
            [
                [
                    s["model"],
                    s["label_recall"],
                    s["images_fully_read"],
                    s["image_only_answers_found"],
                    s["errors"],
                    f"{s['cost_per_image_usd']:.5f}",
                    f"{s['projected_cost_5040_images_usd']:.2f}",
                    round(s["p50_ms"]),
                    round(s["p95_ms"]),
                ]
                for s in result["summary"]
            ],
        )
    )
    print(f"saved {path}")


if __name__ == "__main__":
    main()
