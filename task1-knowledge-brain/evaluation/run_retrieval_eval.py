"""Retrieval eval: does the right article show up, does the evidence gate decide right,
and how fast? Graded in code against each case's gold articles (see evaluation/metrics.py).

    uv run python -m evaluation.run_retrieval_eval                    # production config, dev set
    uv run python -m evaluation.run_retrieval_eval --split heldout    # only to compare finished versions
    uv run python -m evaluation.run_retrieval_eval --configs all      # the ablation table
    uv run python -m evaluation.run_retrieval_eval --repeats 3        # run-to-run noise
    uv run python -m evaluation.run_retrieval_eval --smoke --check evaluation/baselines/smoke.json

The cases (evaluation/cases/retrieval.jsonl) carry a split: `dev` (about 70%, tune on it),
`heldout` (about 30%, run only to compare finished versions) and a `smoke` flag on about
15% of the dev cases, for every pull request. `--check` fails (exit 1) when a metric falls
below the baseline by more than the tolerance; with the free configs, search is
deterministic, so the default tolerance is zero.

Every config here is free and local except `production+rewrite`, which calls Haiku and runs
only with --allow-paid. Results go to evaluation/results/retrieval_<timestamp>.json with
the code, case-file and index versions they were measured on.
"""

import argparse
import asyncio
import hashlib
import json
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import aiosqlite

from evaluation import metrics
from evaluation.common import load_root_env, p50_p95, save
from kb.answer.service import with_context
from kb.config import settings
from kb.db import connect, init_db
from kb.retrieval.embedder import Embedder, make_embedder
from kb.retrieval.search import Mode, Searcher

HERE = Path(__file__).parent
CASES = HERE / "cases" / "retrieval.jsonl"
DEPTH = 20  # articles ranked per question; recall@10 and nDCG@10 need at least 10
CONTEXT_K = 6  # passages the answer model actually sees per question


@dataclass(frozen=True)
class Config:
    mode: Mode = "hybrid"
    keyword_weight: float = 0.3
    changelog_weight: float = 0.5
    max_per_article: int | None = 2
    rewrite: bool = False  # Haiku: paid, so never part of the free stack
    rerank: str | None = None  # cross-encoder model name (kb/retrieval/rerank.py)
    gate: Literal["cosine", "rerank"] = "cosine"  # which score the evidence gate reads
    rerank_depth: int = 20
    rerank_chars: int | None = None
    rewriter: str | None = None  # "ollama:<model>": a local, free rewriter
    rewrite_skip_above: float | None = None


MINILM6, MINILM12 = "Xenova/ms-marco-MiniLM-L-6-v2", "Xenova/ms-marco-MiniLM-L-12-v2"
JINA_TURBO, BGE_RERANK = "jinaai/jina-reranker-v1-turbo-en", "BAAI/bge-reranker-base"
CONFIGS: dict[str, Config] = {
    "keyword": Config("keyword", 1.0, 1.0, None),
    "vector": Config("vector", 1.0, 1.0, None),
    "hybrid": Config("hybrid", 0.3, 1.0, None),  # keyword x0.3
    "production": Config(),  # + changelogs x0.5, max 2 passages per article
    "production+rewrite": Config(rewrite=True),
    "rerank-minilm6": Config(rerank=MINILM6),
    "rerank-minilm6+gate": Config(rerank=MINILM6, gate="rerank"),
    "rerank-minilm6-1000ch": Config(rerank=MINILM6, rerank_chars=1000),
    "rerank-minilm6-12x1000ch": Config(rerank=MINILM6, rerank_depth=12, rerank_chars=1000),
    "rerank-jina-turbo-1000ch": Config(rerank=JINA_TURBO, rerank_chars=1000),
    "rerank-minilm12-1000ch": Config(rerank=MINILM12, rerank_chars=1000),
    "rerank-minilm12+gate": Config(rerank=MINILM12, gate="rerank"),
    "rerank-jina-turbo+gate": Config(rerank=JINA_TURBO, gate="rerank"),
    "rerank-bge-base+gate": Config(rerank=BGE_RERANK, gate="rerank"),
    "rewrite-llama3.2": Config(rewriter="ollama:llama3.2"),
    "rewrite-llama3.2-skip0.80": Config(rewriter="ollama:llama3.2", rewrite_skip_above=0.80),
    "rewrite-mistral": Config(rewriter="ollama:mistral"),
    "rewrite-mistral-skip0.80": Config(rewriter="ollama:mistral", rewrite_skip_above=0.80),
}
PRODUCTION = "production"
ABLATION = ["keyword", "vector", "hybrid", PRODUCTION]  # --configs all
ALIASES = {"+ changelogs x0.5, max 2/article": PRODUCTION}  # its name in older result files


def load_cases(split: str = "dev", smoke: bool = False, path: Path = CASES) -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if smoke:
        return [c for c in cases if c.get("smoke")]
    return [c for c in cases if split == "all" or c["split"] == split]


def search_text(case: dict[str, Any]) -> str:
    """What production searches for: a follow-up carries its earlier turns (kb/api.py).
    The eval has only the earlier questions, not the answers given to them."""
    turns = [{"user": q, "assistant": ""} for q in case.get("history", [])]
    return with_context(case["question"], turns)


async def index_info(db: aiosqlite.Connection, embedding_model: str) -> dict[str, Any]:
    # Row IDs are allocation history, not index content: force-reindex changes them.
    rows = list(
        await db.execute_fetchall(
            "SELECT article_id, source, position, section, parent_section, text, image_ids, "
            "embedding_model, embedding_dim FROM chunks WHERE is_active=1"
        )
    )
    digest = hashlib.sha256()
    records = sorted(json.dumps(list(r), ensure_ascii=False, separators=(",", ":")) for r in rows)
    for record in records:
        digest.update(record.encode("utf-8") + b"\n")
    info: dict[str, Any] = {
        "chunks": len(rows),
        "chunk_text_sha256": digest.hexdigest()[:16],
        "embedding_model": embedding_model,
    }
    with_meta = await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='index_meta'"
    )
    if with_meta:
        info["index_meta"] = [
            dict(r) for r in await db.execute_fetchall("SELECT * FROM index_meta")
        ]
    return info


def code_version() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "task1-knowledge-brain/kb", "kb"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return sha + ("+dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


async def run_config(
    db: aiosqlite.Connection,
    searcher: Searcher,
    config: Config,
    cases: list[dict[str, Any]],
    threshold: float,
) -> tuple[list[dict[str, Any]], list[float]]:
    """`threshold` is on the scale of the score the config's gate reads."""
    searcher.keyword_weight = config.keyword_weight
    searcher.changelog_weight = config.changelog_weight
    searcher.max_per_article = config.max_per_article
    rows, latencies = [], []
    for case in cases:
        t = time.perf_counter()
        hits = await searcher.search(db, search_text(case), k=DEPTH, mode=config.mode)
        latencies.append((time.perf_counter() - t) * 1000)
        # The answer step sees the first CONTEXT_K passages; its gate reads the first one.
        context = hits[:CONTEXT_K]
        if not context or config.mode == "keyword":
            top = 0.0
        elif config.gate == "rerank":
            top = context[0].rerank_score if context[0].rerank_score is not None else 0.0
        else:
            top = context[0].score
        ranked = [h.article_id for h in hits]
        row = {
            "id": case["id"],
            "category": case["category"],
            **metrics.case_scores(case, ranked, top, threshold),
        }
        if row.get("failure"):
            row["top_titles"] = list(dict.fromkeys(h.title for h in hits))[:5]
        rows.append(row)
    return rows, latencies


def describe(rows: list[dict[str, Any]], latencies: list[float]) -> dict[str, Any]:
    summary: dict[str, Any] = metrics.summarise(rows)
    ans = [r for r in rows if r["answerable"]]
    for name in ("recall@5", "mrr@10", "ndcg@10"):
        key = metrics.METRICS[name][1]
        summary[f"{name}_ci95"] = metrics.bootstrap_ci(
            ans, lambda sample, key=key: statistics.fmean(float(r[key]) for r in sample)
        )
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for r in ans:
        by_cat.setdefault(r["category"], []).append(r)
    summary["recall@5_by_category"] = {
        cat: {"n": len(rs), "recall@5": statistics.fmean(float(r["hit@5"]) for r in rs)}
        for cat, rs in sorted(by_cat.items())
    }
    summary["p50_ms"], summary["p95_ms"] = p50_p95(latencies)
    summary["failures"] = {r["id"]: r["failure"] for r in rows if r.get("failure")}
    # Every observed score is a candidate threshold, so this works on any score scale.
    sweep = metrics.gate_sweep(rows, sorted({round(r["top_score"], 3) for r in rows}))
    summary["gate_sweep"] = sweep
    summary["suggested_threshold"] = metrics.suggest_threshold(sweep)
    return summary


HEADLINE = [
    "recall@5",
    "recall@10",
    "mrr@10",
    "ndcg@10",
    "hit@1",
    "in_context@6",
    "wrongly_refused",
    "wrongly_answered",
]


def print_tables(results: dict[str, dict[str, Any]], repeats: int) -> None:
    print(
        "| config | recall@5 (95% CI) | recall@10 | MRR@10 | nDCG@10 | hit@1 | in context @6 "
        "| wrongly refused | wrongly answered | p50 / p95 ms |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|")
    for name, r in results.items():
        s = r["runs"][0]
        lo, hi = s["recall@5_ci95"]
        # Keyword-only search has no similarity score, so the gate cannot be measured.
        gate = (
            "n/a | n/a"
            if r["config"]["mode"] == "keyword"
            else f"{s['wrongly_refused']:.0%} | {s['wrongly_answered']:.0%}"
        )
        print(
            f"| {name} | {s['recall@5']:.0%} ({lo:.0%}–{hi:.0%}) | {s['recall@10']:.0%} | "
            f"{s['mrr@10']:.2f} | {s['ndcg@10']:.2f} | {s['hit@1']:.0%} | "
            f"{s['in_context@6']:.0%} | {gate} | {s['p50_ms']:.0f} / {s['p95_ms']:.0f} |"
        )
    first = next(iter(results.values()))["runs"][0]
    print(
        f"\n{first['answerable_cases']} answerable + {first['refuse_cases']} refuse cases. "
        "Gate columns use the threshold in the results file."
    )
    if repeats > 1:
        print("\nRun-to-run noise (same cases, same config):")
        print("| config | metric | mean | sd | min | max |")
        print("|---|---|---|---|---|---|")
        for name, r in results.items():
            for m in ("recall@5", "mrr@10", "ndcg@10", "wrongly_answered"):
                vals = [run[m] for run in r["runs"]]
                print(
                    f"| {name} | {m} | {statistics.fmean(vals):.3f} | "
                    f"{statistics.pstdev(vals):.3f} | {min(vals):.3f} | {max(vals):.3f} |"
                )
    for name, r in results.items():
        s = r["runs"][0]
        print(
            f"\n{name}: recall@5 by category: "
            + ", ".join(
                f"{c} {v['recall@5']:.0%} (n={v['n']})"
                for c, v in s["recall@5_by_category"].items()
            )
        )
        print(
            f"{name}: failures: "
            + (", ".join(f"{k} ({v})" for k, v in s["failures"].items()) or "-")
        )
        t = s["suggested_threshold"]
        at = next(row for row in s["gate_sweep"] if row["threshold"] == t)
        print(
            f"{name}: suggested gate threshold {t} (on these cases: wrongly answered "
            f"{at['wrongly_answered']:.0%}, wrongly refused {at['wrongly_refused']:.0%} "
            "of found answers; choose it on dev, confirm it on heldout)"
        )


# ---------- baseline check (CI) ----------

HIGHER_IS_BETTER = ["recall@5", "recall@10", "mrr@10", "ndcg@10", "in_context@6"]
LOWER_IS_BETTER = ["wrongly_answered", "wrongly_refused"]


def baseline_payload(
    name: str,
    split: str,
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
    index: dict[str, Any],
) -> dict[str, Any]:
    return {
        "config": name,
        "split": split,
        "index": {k: index[k] for k in ("chunks", "chunk_text_sha256", "embedding_model")},
        "summary": {m: summary[m] for m in HIGHER_IS_BETTER + LOWER_IS_BETTER},
        "cases": {r["id"]: {k: r[k] for k in ("hit@5", "passes_gate") if k in r} for r in rows},
    }


def check(
    baseline: dict[str, Any],
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
    tolerance: float,
    index: dict[str, Any],
) -> list[str]:
    problems = []
    for k, v in baseline.get("index", {}).items():
        if index.get(k) != v:
            problems.append(
                f"index {k} is {index.get(k)!r}, the baseline was measured on {v!r}: "
                "if the index change is intended, re-run with --write-baseline"
            )
    for m in HIGHER_IS_BETTER:
        if summary[m] < baseline["summary"][m] - tolerance - 1e-9:
            problems.append(f"{m} fell from {baseline['summary'][m]:.3f} to {summary[m]:.3f}")
    for m in LOWER_IS_BETTER:
        if summary[m] > baseline["summary"][m] + tolerance + 1e-9:
            problems.append(f"{m} rose from {baseline['summary'][m]:.3f} to {summary[m]:.3f}")
    for r in rows:
        before = baseline["cases"].get(r["id"])
        if before is None:
            continue
        if before.get("hit@5") and not r.get("hit@5"):
            problems.append(f"{r['id']}: gold article no longer in the top 5")
        if not r["answerable"] and not before.get("passes_gate") and r["passes_gate"]:
            problems.append(f"{r['id']}: should be refused but now passes the evidence gate")
    return problems


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--split", default="dev", choices=["dev", "heldout", "all"])
    ap.add_argument("--smoke", action="store_true", help="only the smoke cases (for CI)")
    ap.add_argument(
        "--configs", default="production", help="comma-separated names, or 'all' (the ablation)"
    )
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--threshold", type=float, default=settings.min_evidence_score)
    ap.add_argument(
        "--rerank-threshold",
        type=float,
        default=0.0,
        help="gate threshold for rerank-gated configs",
    )
    ap.add_argument("--db", type=Path, default=settings.db_path)
    ap.add_argument("--embedding", default=settings.embedding_model)
    ap.add_argument(
        "--allow-paid", action="store_true", help="allow configs that call a paid model"
    )
    ap.add_argument("--check", type=Path, help="baseline JSON: exit 1 on a drop")
    ap.add_argument("--tolerance", type=float, default=0.0, help="allowed drop for --check")
    ap.add_argument("--write-baseline", type=Path)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    names = (
        ABLATION
        if args.configs == "all"
        else [ALIASES.get(n.strip(), n.strip()) for n in args.configs.split(",")]
    )
    unknown = [n for n in names if n not in CONFIGS]
    if unknown:
        ap.error(f"unknown config(s) {unknown}; choose from {list(CONFIGS)}")
    if any(CONFIGS[n].rewrite for n in names) and not args.allow_paid:
        ap.error("query rewriting calls Haiku (paid): add --allow-paid to run it")
    cases = load_cases(args.split, args.smoke)
    split = "smoke" if args.smoke else args.split

    embedder: Embedder = make_embedder(args.embedding, settings.model_cache_dir)
    searcher = Searcher(embedder)
    rewriter = None
    if any(CONFIGS[n].rewrite for n in names):
        from kb.retrieval.rewrite import ClaudeQueryRewriter

        load_root_env()
        rewriter = ClaudeQueryRewriter(settings.rewrite_model)
    rerankers: dict[str, Any] = {}

    results: dict[str, dict[str, Any]] = {}
    await init_db(args.db)  # same schema migrations the API applies at start-up
    async with connect(args.db) as db:
        await searcher.vectors.refresh(db)  # load once, outside the timings
        embedder.embed_query("warm up")
        info = await index_info(db, args.embedding)
        for name in names:
            config = CONFIGS[name]
            searcher.rewriter = rewriter if config.rewrite else None
            if config.rewriter:
                from kb.answer.local import OllamaQueryRewriter

                searcher.rewriter = OllamaQueryRewriter(config.rewriter.removeprefix("ollama:"))
            searcher.rewrite_skip_above = config.rewrite_skip_above
            searcher.rewrites = {"used": 0, "skipped": 0}
            if config.rerank:
                from kb.retrieval.rerank import CrossEncoderReranker

                rerankers.setdefault(
                    config.rerank, CrossEncoderReranker(config.rerank, settings.model_cache_dir)
                )
            searcher.reranker = rerankers.get(config.rerank) if config.rerank else None
            searcher.rerank_depth, searcher.rerank_chars = config.rerank_depth, config.rerank_chars
            runs, all_rows = [], []
            for _ in range(args.repeats):
                if searcher.rewriter is not None:  # a repeat must ask the model again
                    searcher.rewriter._cache.clear()  # type: ignore[attr-defined]
                threshold = args.rerank_threshold if config.gate == "rerank" else args.threshold
                rows, latencies = await run_config(db, searcher, config, cases, threshold)
                runs.append(describe(rows, latencies))
                all_rows.append(rows)
            results[name] = {
                "config": config.__dict__,
                "runs": runs,
                "per_case": all_rows[0],
                "rewrites": dict(searcher.rewrites),
            }

    print_tables(results, args.repeats)
    payload = {
        "cases": len(cases),
        "split": split,
        "threshold": args.threshold,
        "rerank_threshold": args.rerank_threshold,
        "code_version": code_version(),
        "cases_sha256": hashlib.sha256(CASES.read_bytes()).hexdigest()[:16],
        "index": info,
        "results": results,
    }
    if not args.no_save:
        print(f"\nsaved {save('retrieval', payload)}")

    first_name = names[0]
    first = results[first_name]
    if args.write_baseline:
        args.write_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.write_baseline.write_text(
            json.dumps(
                baseline_payload(first_name, split, first["runs"][0], first["per_case"], info),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"baseline written to {args.write_baseline}")
    if args.check:
        baseline = json.loads(args.check.read_text(encoding="utf-8"))
        problems = check(baseline, first["runs"][0], first["per_case"], args.tolerance, info)
        if problems:
            print("\nREGRESSION against " + str(args.check) + ":\n  " + "\n  ".join(problems))
            return 1
        print(f"\nNo regression against {args.check}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
