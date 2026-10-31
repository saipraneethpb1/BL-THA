"""Answer eval: the same 32 questions answered by each candidate model.

    uv run python -m evaluation.run_answer_eval                 # dry run: shows the plan
    uv run python -m evaluation.run_answer_eval --run           # spends API credits
    uv run python -m evaluation.run_answer_eval --run --models claude-haiku-4-5,claude-sonnet-4-6
    uv run python -m evaluation.run_answer_eval --run --models ollama:llama3.2 --rewriter none

Models named `ollama:<model>` run locally through Ollama (free, kb/answer/local.py).
`--rewriter` picks the query rewriter: `haiku` (paid, the old production setting), `none`,
or `ollama:<model>`.

Runs against a throwaway copy of the knowledge base, so eval questions never create
real escalations. A case passes only if every check passes:
- expected behaviour: "answer" cases must come back `answered`; "abstain" cases must
  come back `insufficient_evidence` (a confident answer to an unanswerable question fails);
- citation: an answer cites at least one gold article;
- facts: each group of required phrases has at least one phrase in the answer;
- forbidden: none of the forbidden phrases appear (hallucinated steps, leaked prompt).
"""

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from evaluation.common import load_root_env, p50_p95, save, table
from kb.answer.llm import Answerer, ClaudeAnswerer
from kb.answer.local import OllamaAnswerer, OllamaQueryRewriter
from kb.answer.service import QueryResult, answer_question
from kb.config import settings
from kb.db import connect, init_db
from kb.retrieval.embedder import FastEmbedder
from kb.retrieval.rewrite import ClaudeQueryRewriter
from kb.retrieval.search import Searcher
from kb.snapshot import make_snapshot

HERE = Path(__file__).parent
DEFAULT_MODELS = "claude-haiku-4-5,claude-sonnet-4-6,claude-opus-4-6"


def load_cases(tags: set[str] | None = None) -> list[dict[str, Any]]:
    lines = (HERE / "cases" / "answer.jsonl").read_text(encoding="utf-8").splitlines()
    cases = [json.loads(line) for line in lines if line.strip()]
    return [c for c in cases if not tags or tags & set(c["tags"])]


def grade(case: dict[str, Any], r: QueryResult) -> dict[str, Any]:
    text = r.answer.lower()
    cited = " ".join(s.url or "" for s in r.sources)
    if case["expect"] == "answer":
        behaviour = r.status == "answered"
        citation = any(g in cited for g in case["gold"]) if case["gold"] else True
        facts = all(any(p.lower() in text for p in group) for group in case["facts"])
    else:
        behaviour = r.status == "insufficient_evidence"
        citation = facts = True
    forbidden = [p for p in case["forbidden"] if p.lower() in text]
    return {
        "behaviour_ok": behaviour,
        "citation_ok": citation,
        "facts_ok": facts,
        "forbidden_found": forbidden,
        "passed": behaviour and citation and facts and not forbidden,
    }


def summarise(model: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ans = [r for r in rows if r["expect"] == "answer"]
    abst = [r for r in rows if r["expect"] == "abstain"]
    p50, p95 = p50_p95([r["latency_ms"] for r in rows])
    return {
        "model": model,
        "cases": len(rows),
        "pass_rate": sum(r["passed"] for r in rows) / len(rows),
        "answered_when_answerable": sum(r["behaviour_ok"] for r in ans) / max(len(ans), 1),
        "citation_hit": sum(r["citation_ok"] for r in ans if r["behaviour_ok"])
        / max(sum(r["behaviour_ok"] for r in ans), 1),
        "facts_ok": sum(r["facts_ok"] for r in ans if r["behaviour_ok"])
        / max(sum(r["behaviour_ok"] for r in ans), 1),
        "abstained_when_unanswerable": sum(r["behaviour_ok"] for r in abst) / max(len(abst), 1),
        "forbidden_hits": sum(bool(r["forbidden_found"]) for r in rows),
        "cost_per_question_usd": sum(r["cost_usd"] for r in rows) / len(rows),
        "total_cost_usd": sum(r["cost_usd"] for r in rows),
        "p50_ms": p50,
        "p95_ms": p95,
    }


def make_answerer(model: str) -> Answerer:
    if model.startswith("ollama:"):
        return OllamaAnswerer(model.removeprefix("ollama:"), settings.answer_prompt_version)
    return ClaudeAnswerer(model, settings.answer_prompt_version, settings.answer_effort)


def make_rewriter(name: str) -> ClaudeQueryRewriter | OllamaQueryRewriter | None:
    if name == "none":
        return None
    if name.startswith("ollama:"):
        return OllamaQueryRewriter(name.removeprefix("ollama:"))
    return ClaudeQueryRewriter(settings.rewrite_model)


async def run(
    models: list[str], cases: list[dict[str, Any]], rewriter: str = "haiku"
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        db_copy = Path(tmp) / "eval.db"
        make_snapshot(settings.db_path, db_copy)
        await init_db(db_copy)  # schema migrations, as at API start-up
        searcher = Searcher(  # same retrieval as production
            FastEmbedder(settings.embedding_model, settings.model_cache_dir),
            rewriter=make_rewriter(rewriter),
        )
        summaries, per_case = [], []
        async with connect(db_copy) as db:
            await searcher.vectors.refresh(db)
            for model in models:
                answerer = make_answerer(model)
                rows = []
                for case in cases:
                    t = time.perf_counter()
                    r = await answer_question(
                        db,
                        searcher,
                        answerer,
                        case["question"],
                        min_score=settings.min_evidence_score,
                        caller="eval",
                    )
                    row = {
                        "model": model,
                        "id": case["id"],
                        "expect": case["expect"],
                        "tags": case["tags"],
                        "status": r.status,
                        "answer": r.answer,
                        "sources": [s.url for s in r.sources],
                        "cost_usd": r.cost_usd,
                        "latency_ms": (time.perf_counter() - t) * 1000,
                        **grade(case, r),
                    }
                    rows.append(row)
                    print(
                        f"  {model} {case['id']}: {'PASS' if row['passed'] else 'fail'} ({r.status})"
                    )
                summaries.append(summarise(model, rows))
                per_case += rows
    return {
        "summary": summaries,
        "cases": per_case,
        "prompt_version": settings.answer_prompt_version,
        "rewriter": rewriter,
        "min_evidence_score": settings.min_evidence_score,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=DEFAULT_MODELS)
    ap.add_argument("--tags", default="", help="only cases with any of these tags, comma-separated")
    ap.add_argument("--run", action="store_true", help="actually run (Claude models cost money)")
    ap.add_argument("--rewriter", default="haiku", help="haiku | none | ollama:<model>")
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    cases = load_cases(set(filter(None, args.tags.split(","))) or None)
    print(
        f"{len(cases)} cases x {len(models)} models = up to {len(cases) * len(models)} answer calls "
        f"(fewer: weak-evidence questions are refused before any model call)."
    )
    if not args.run:
        print("Dry run. Add --run to execute.")
        return
    load_root_env()
    result = asyncio.run(run(models, cases, args.rewriter))
    path = save("answers", result)
    print(
        table(
            [
                "model",
                "pass",
                "answers when it should",
                "cites gold",
                "facts",
                "abstains when it should",
                "forbidden",
                "$/question",
                "p50 ms",
                "p95 ms",
            ],
            [
                [
                    s["model"],
                    s["pass_rate"],
                    s["answered_when_answerable"],
                    s["citation_hit"],
                    s["facts_ok"],
                    s["abstained_when_unanswerable"],
                    s["forbidden_hits"],
                    f"{s['cost_per_question_usd']:.5f}",
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
