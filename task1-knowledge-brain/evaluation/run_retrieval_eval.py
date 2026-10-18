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

from evaluation.common import load_root_env
from kb.config import settings
from kb.db import connect
from kb.retrieval.embedder import FastEmbedder
from kb.retrieval.rewrite import ClaudeQueryRewriter
from kb.retrieval.search import Mode, Searcher

HERE = Path(__file__).parent
# (label, mode, keyword weight, changelog weight, max passages per article)
CONFIGS: list[tuple[str, Mode, float, float, int | None, bool]] = [
    ("keyword", "keyword", 1.0, 1.0, None, False),
    ("vector", "vector", 1.0, 1.0, None, False),
    ("hybrid w=0.3 (before tuning)", "hybrid", 0.3, 1.0, None, False),
    ("+ changelogs x0.5", "hybrid", 0.3, 0.5, None, False),
    ("+ changelogs x0.25", "hybrid", 0.3, 0.25, None, False),
    ("+ changelogs x0.5, max 2/article", "hybrid", 0.3, 0.5, 2, False),
    ("+ changelogs x0.25, max 2/article", "hybrid", 0.3, 0.25, 2, False),
    ("+ query rewriting (Haiku)", "hybrid", 0.3, 0.5, 2, True),
]
CONTEXT_K = 6  # passages the answer model actually sees per question


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
    load_root_env()
    rewriter = ClaudeQueryRewriter(settings.rewrite_model)
    searcher = Searcher(FastEmbedder(settings.embedding_model, settings.model_cache_dir))
    results: dict[str, dict[str, object]] = {}
    per_case: list[dict[str, object]] = []
    async with connect(settings.db_path) as db:
        await searcher.vectors.refresh(db)  # load once, outside the timings
        for label, mode, weight, changelog, cap, rewrite in CONFIGS:
            searcher.rewriter = rewriter if rewrite else None
            searcher.keyword_weight = weight
            searcher.changelog_weight = changelog
            searcher.max_per_article = cap
            ranks: list[int | None] = []
            in_context: list[bool] = []
            latencies: list[float] = []
            for case in cases:
                t = time.perf_counter()
                hits = await searcher.search(db, case["question"], k=20, mode=mode)
                latencies.append((time.perf_counter() - t) * 1000)
                rank = first_gold_rank([h.article_id for h in hits], set(case["gold"]))
                ranks.append(rank)
                ctx = await searcher.search(db, case["question"], k=CONTEXT_K, mode=mode)
                in_context.append(any(h.article_id in case["gold"] for h in ctx))
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
                "in_context@6": sum(in_context) / n,
                "in_context@6_fresh": sum(
                    ok
                    for c, ok in zip(cases, in_context, strict=True)
                    if c.get("added") != "after_diagnosis"
                )
                / max(sum(c.get("added") != "after_diagnosis" for c in cases), 1),
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
        "| config | hit@1 | hit@5 | MRR@10 | in context @6 | same, excl. diagnosed cases | p50 ms | p95 ms | misses@5 |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for mode, r in results.items():
        print(
            f"| {mode} | {r['hit@1']:.0%} | {r['hit@5']:.0%} | {r['mrr@10']:.2f} | "
            f"{r['in_context@6']:.0%} | {r['in_context@6_fresh']:.0%} | "
            f"{r['p50_ms']:.0f} | {r['p95_ms']:.0f} | {', '.join(r['misses@5']) or '-'} |"
        )  # type: ignore[arg-type]
    cost = (rewriter.input_tokens * 0.10 + rewriter.output_tokens * 0.50) / 1e6
    print()
    print(
        f"Query rewriting: {len(cases)} Haiku calls, ${cost:.4f} total ({cost / len(cases):.6f}/question)."
    )


if __name__ == "__main__":
    asyncio.run(main())
