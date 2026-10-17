"""Retrieval eval: does the right article show up, and how fast?

Usage: uv run python -m evaluation.run_retrieval_eval
Writes evaluation/results/retrieval_<timestamp>.json and prints a markdown table.

Metrics per search mode:
- hit@1 / hit@5: a gold article is the first result / in the top 5 articles
- MRR@10: mean of 1/rank of the first gold article (0 if not in the top 10)
- p50 / p95 latency of the search call
"""

import asyncio
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from kb.config import settings
from kb.db import connect
from kb.retrieval.embedder import FastEmbedder
from kb.retrieval.search import Mode, Searcher

HERE = Path(__file__).parent
# (label, mode, keyword weight used in hybrid)
CONFIGS: list[tuple[str, Mode, float]] = [
    ("keyword", "keyword", 1.0),
    ("vector", "vector", 1.0),
    ("hybrid w=1.0", "hybrid", 1.0),
    ("hybrid w=0.5", "hybrid", 0.5),
    ("hybrid w=0.3", "hybrid", 0.3),
]


def first_gold_rank(article_ids: list[str], gold: set[str]) -> int | None:
    seen: list[str] = []
    for aid in article_ids:  # rank by distinct article, not by chunk
        if aid not in seen:
            seen.append(aid)
    for rank, aid in enumerate(seen, start=1):
        if aid in gold:
            return rank
    return None


async def main() -> None:
    cases = [json.loads(line) for line in (HERE / "retrieval_cases.jsonl").read_text().splitlines()]
    searcher = Searcher(FastEmbedder(settings.embedding_model, settings.model_cache_dir))
    results: dict[str, dict[str, object]] = {}
    per_case: list[dict[str, object]] = []
    async with connect(settings.db_path) as db:
        await searcher.vectors.refresh(db)  # load once, outside the timings
        for label, mode, weight in CONFIGS:
            searcher.keyword_weight = weight
            ranks: list[int | None] = []
            latencies: list[float] = []
            for case in cases:
                t = time.perf_counter()
                hits = await searcher.search(db, case["question"], k=20, mode=mode)
                latencies.append((time.perf_counter() - t) * 1000)
                rank = first_gold_rank([h.article_id for h in hits], set(case["gold"]))
                ranks.append(rank)
                per_case.append(
                    {
                        "mode": label,
                        "id": case["id"],
                        "rank": rank,
                        "top": hits[0].title if hits else None,
                    }
                )
            n = len(cases)
            latencies.sort()
            results[label] = {
                "hit@1": sum(r == 1 for r in ranks) / n,
                "hit@5": sum(r is not None and r <= 5 for r in ranks) / n,
                "mrr@10": sum(1 / r for r in ranks if r is not None and r <= 10) / n,
                "p50_ms": statistics.median(latencies),
                "p95_ms": latencies[int(0.95 * (n - 1))],
                "misses@5": [
                    c["id"] for c, r in zip(cases, ranks, strict=True) if r is None or r > 5
                ],
            }

    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (out_dir / f"retrieval_{stamp}.json").write_text(
        json.dumps({"cases": len(cases), "summary": results, "per_case": per_case}, indent=2)
    )
    print(
        "| mode | hit@1 | hit@5 | MRR@10 | p50 ms | p95 ms | misses@5 |\n|---|---|---|---|---|---|---|"
    )
    for mode, r in results.items():
        print(
            f"| {mode} | {r['hit@1']:.0%} | {r['hit@5']:.0%} | {r['mrr@10']:.2f} | "
            f"{r['p50_ms']:.0f} | {r['p95_ms']:.0f} | {', '.join(r['misses@5']) or '-'} |"
        )  # type: ignore[arg-type]


if __name__ == "__main__":
    asyncio.run(main())
