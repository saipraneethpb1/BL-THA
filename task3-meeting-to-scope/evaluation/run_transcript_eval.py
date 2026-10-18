"""Transcript eval: the whole Scope Builder on your recorded meetings.

    uv run python -m evaluation.run_transcript_eval                 # dry run: lists gold files
    uv run python -m evaluation.run_transcript_eval --run           # needs KB + API key
    uv run python -m evaluation.run_transcript_eval --run --analyst-models claude-haiku-5-5,claude-sonnet-5-5

Gold files live in evaluation/gold/<meeting>.json (see TEMPLATE.json). Every gold
requirement is one case; every expected follow-up change is one case. For each analyst
model the full pipeline runs in a throwaway database: build v1 from the first meeting,
approve it, then (for follow-ups) add the next meeting and build v2 against v1.

Measured: requirement recall, share of extracted requirements that match no gold item
(noise), share of quotes found in the transcript, change detection accuracy,
cost and time per build.
"""

import argparse
import asyncio
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from evaluation.common import kb_key, load_root_env, save
from scope.config import Settings, settings
from scope.knowledge import HttpKnowledgeClient, KBAnswer, KnowledgeClient
from scope.llm import ClaudeLLM
from scope.models import MeetingAnalysis, Requirement, RequirementChange
from scope.pipeline import ScopeBuilder
from scope.store import Store

ROOT = Path(__file__).parent.parent
GOLD = Path(__file__).parent / "gold"


def matches(text: str, keywords: list[list[str]]) -> bool:
    t = " ".join(re.findall(r"[a-z0-9]+", text.lower()))
    return all(any(k.lower() in t for k in group) for group in keywords)


def score_requirements(
    gold: list[dict[str, Any]], reqs: list[Requirement], notes: list[str] | None = None
) -> dict[str, Any]:
    """Recall counts a gold item found in a requirement OR a recorded decision/constraint:
    teams often state a requirement as a decision ("the remote will use regular batteries").
    Noise is counted on requirements only."""
    texts = [r.description for r in reqs] + (notes or [])
    found = [g["id"] for g in gold if any(matches(t, g["keywords"]) for t in texts)]
    noise = [r.id for r in reqs if not any(matches(r.description, g["keywords"]) for g in gold)]
    quotes = [e.verified for r in reqs for e in r.evidence]
    return {
        "gold": len(gold),
        "found": len(found),
        "missed": [g["id"] for g in gold if g["id"] not in found],
        "extracted": len(reqs),
        "unmatched_extracted": len(noise),
        "quotes_verified": sum(quotes) / len(quotes) if quotes else 0.0,
    }


def score_changes(
    expected: list[dict[str, Any]], changes: list[RequirementChange]
) -> dict[str, Any]:
    hits = []
    for exp in expected:
        ok = any(
            c.change in ([exp["change"]] if isinstance(exp["change"], str) else exp["change"])
            and matches(" ".join(filter(None, [c.before, c.after, c.summary])), exp["keywords"])
            for c in changes
        )
        hits.append({"change": exp["change"], "keywords": exp["keywords"], "detected": ok})
    flagged = [c.requirement_id for c in changes if c.out_of_scope]
    return {
        "expected": len(expected),
        "detected": sum(h["detected"] for h in hits),
        "cases": hits,
        "flagged_out_of_scope": flagged,
    }


class NoKnowledge:
    """Stand-in Knowledge Brain for meetings that aren't about the product (the AMI
    corpus is about a TV remote). It answers "not covered" so no money is spent and no
    junk lands in the real expert queue. Feasibility has its own eval."""

    async def query(self, question: str) -> KBAnswer:
        return KBAnswer(
            status="insufficient_evidence", answer="Not covered.", sources=[], needs_human=True
        )


def notes_of(a: MeetingAnalysis) -> list[str]:
    return [n.statement for n in a.decisions + a.constraints]


def load_gold() -> dict[str, dict[str, Any]]:
    return {
        p.stem: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(GOLD.glob("*.json"))
        if p.stem != "TEMPLATE"
    }


async def build(builder: ScopeBuilder, store: Store, pid: int) -> tuple[Any, float]:
    t = time.perf_counter()
    n = await builder.start(pid)
    await builder.build(pid, n)
    v = await store.version(pid, n)
    if v is None or v.data is None:
        raise RuntimeError(f"build failed: {v.error if v else 'missing'}")
    return v, time.perf_counter() - t


async def run(models: list[str], gold: dict[str, dict[str, Any]], live_kb: bool) -> dict[str, Any]:
    results = []
    kb: KnowledgeClient = (
        HttpKnowledgeClient(settings.kb_url, kb_key(settings.kb_api_key))
        if live_kb
        else NoKnowledge()
    )
    first_meetings = {k: g for k, g in gold.items() if not g.get("followup")}
    followups = {k: g for k, g in gold.items() if g.get("followup")}
    for model in models:
        s = Settings(**{**settings.model_dump(), "analyst_model": model})
        for name, g in first_meetings.items():
            with tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp) / "eval.db")
                await store.init()
                builder = ScopeBuilder(store, ClaudeLLM(), kb, s)
                pid = await store.create_project(f"eval {name}", "eval")
                await store.add_meeting(
                    pid, g["title"], g["date"], (ROOT / g["transcript"]).read_text(encoding="utf-8")
                )
                v1, secs = await build(builder, store, pid)
                row: dict[str, Any] = {
                    "model": model,
                    "meeting": name,
                    "build_seconds": secs,
                    "cost_usd": v1.usage.get("cost_usd", 0.0),
                    "requirements": score_requirements(
                        g["requirements"], v1.data.analysis.requirements, notes_of(v1.data.analysis)
                    ),
                }
                for fname, f in followups.items():
                    if f["followup"]["baseline"] != name:
                        continue
                    await store.approve(
                        pid, v1.number, "eval", "auto-approved to test the follow-up"
                    )
                    await store.add_meeting(
                        pid,
                        f["title"],
                        f["date"],
                        (ROOT / f["transcript"]).read_text(encoding="utf-8"),
                    )
                    v2, secs2 = await build(builder, store, pid)
                    row["followup"] = {
                        "meeting": fname,
                        "build_seconds": secs2,
                        "cost_usd": v2.usage.get("cost_usd", 0.0),
                        "requirements": score_requirements(
                            f["requirements"],
                            v2.data.analysis.requirements,
                            notes_of(v2.data.analysis),
                        ),
                        "changes": score_changes(
                            f["followup"]["changes"], v2.data.diff.changes if v2.data.diff else []
                        ),
                        # Full detail, so a weak score can be diagnosed without re-running.
                        "baseline_requirements": [
                            r.description for r in v1.data.analysis.requirements
                        ],
                        "baseline_decisions": notes_of(v1.data.analysis),
                        "diff": [
                            c.model_dump(exclude={"evidence"})
                            for c in (v2.data.diff.changes if v2.data.diff else [])
                        ],
                    }
                results.append(row)
                print(
                    f"  {model} {name}: {row['requirements']['found']}/{row['requirements']['gold']} requirements"
                    + (
                        f", changes {row['followup']['changes']['detected']}/{row['followup']['changes']['expected']}"
                        if "followup" in row
                        else ""
                    )
                )
    if isinstance(kb, HttpKnowledgeClient):
        await kb.aclose()
    return {"results": results}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--analyst-models", default="claude-haiku-5-5,claude-sonnet-5-5")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--meetings", default="", help="only these first meetings (and their follow-ups)")
    ap.add_argument(
        "--live-kb",
        action="store_true",
        help="ask the real Knowledge Brain (only useful for meetings about the product)",
    )
    args = ap.parse_args()
    gold = load_gold()
    if args.meetings:
        keep = set(args.meetings.split(","))
        gold = {k: g for k, g in gold.items() if k in keep or g.get("followup", {}).get("baseline") in keep}
    models = [m for m in args.analyst_models.split(",") if m]
    if not gold:
        print(
            "No gold files yet. Record the meetings, then copy evaluation/gold/TEMPLATE.json per meeting."
        )
        return
    cases = sum(
        len(g["requirements"]) + len(g.get("followup", {}).get("changes", []))
        for g in gold.values()
    )
    print(f"{len(gold)} meetings, {cases} gold cases, x {len(models)} analyst models.")
    if not args.run:
        print("Dry run. Add --run to execute (needs ANTHROPIC_API_KEY and the Knowledge Brain).")
        return
    load_root_env()
    result = asyncio.run(run(models, gold, args.live_kb))
    print(
        "\n| analyst model | meeting | requirements found | unmatched extracted | quotes verified | changes detected | $ build | s build |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for r in result["results"]:
        q = r["requirements"]
        print(
            f"| {r['model']} | {r['meeting']} | {q['found']}/{q['gold']} | {q['unmatched_extracted']} | "
            f"{q['quotes_verified']:.0%} | - | {r['cost_usd']:.3f} | {r['build_seconds']:.0f} |"
        )
        if f := r.get("followup"):
            q2, ch = f["requirements"], f["changes"]
            print(
                f"| {r['model']} | {f['meeting']} | {q2['found']}/{q2['gold']} | {q2['unmatched_extracted']} | "
                f"{q2['quotes_verified']:.0%} | {ch['detected']}/{ch['expected']} | {f['cost_usd']:.3f} | {f['build_seconds']:.0f} |"
            )
    print(f"saved {save('transcripts', result)}")


if __name__ == "__main__":
    main()
