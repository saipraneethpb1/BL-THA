"""The harness that judges a rewrite prompt: does every wording of a question behave the same?

Each of the 42 eval questions comes in four forms (robustness_cases.jsonl): as asked, with
synonyms ("customized" for "custom", "colour" for "color"), misspelled, and with other names
for the same feature ("coupon" for "discount code"). A group passes when every form finds the
right article in the top 5 AND the same article first: then wording, spelling and naming
don't change what the customer gets.

Groups are split in two for fairness: the prompt writer sees failures from "train" only; a
new prompt is judged on "holdout", which it never saw.
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

from kb.progress import Progress, report
from kb.retrieval.rewrite import QueryRewriter
from kb.retrieval.search import Searcher

CASES_FILE = Path(__file__).resolve().parent / "robustness_cases.jsonl"
KINDS = ("original", "synonym", "misspelling", "naming")


def load_cases(path: Path = CASES_FILE) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def split_of(case_id: str) -> str:
    """Odd-numbered questions train, even-numbered ones are held out."""
    digits = "".join(c for c in case_id if c.isdigit())
    return "train" if int(digits or 0) % 2 else "holdout"


@dataclass
class VariantResult:
    kind: str
    question: str
    rewrites: list[str]
    top_article: str | None
    top_title: str | None
    rank: int | None  # of the first correct article among distinct articles, 1-based

    @property
    def hit(self) -> bool:
        return self.rank is not None and self.rank <= 5


@dataclass
class GroupResult:
    id: str
    split: str
    gold: list[str]
    gold_title: str | None
    variants: list[VariantResult] = field(default_factory=list)

    @property
    def all_hit(self) -> bool:
        return all(v.hit for v in self.variants)

    @property
    def same_top(self) -> bool:
        return len({v.top_article for v in self.variants}) == 1

    @property
    def passed(self) -> bool:
        return self.all_hit and self.same_top


def score(groups: list[GroupResult], split: str | None = None) -> dict[str, Any]:
    gs = [g for g in groups if split is None or g.split == split]
    vs = [v for g in gs for v in g.variants]
    if not gs:
        return {"groups": 0}
    return {
        "groups": len(gs),
        "variants": len(vs),
        "passed": sum(g.passed for g in gs),
        "consistency": round(sum(g.passed for g in gs) / len(gs), 3),
        "hit_rate": round(sum(v.hit for v in vs) / len(vs), 3),
        "by_kind": {
            k: round(
                sum(v.hit for v in vs if v.kind == k) / max(1, sum(v.kind == k for v in vs)), 3
            )
            for k in KINDS
        },
    }


def summary(groups: list[GroupResult]) -> dict[str, Any]:
    return {
        "all": score(groups),
        "train": score(groups, "train"),
        "holdout": score(groups, "holdout"),
    }


def to_json(groups: list[GroupResult]) -> list[dict[str, Any]]:
    return [
        {
            **{k: v for k, v in asdict(g).items() if k != "variants"},
            "passed": g.passed,
            "variants": [{**asdict(v), "hit": v.hit} for v in g.variants],
        }
        for g in groups
    ]


async def run(
    db: aiosqlite.Connection,
    live: Searcher,
    rewriter: QueryRewriter,
    cases: list[dict[str, Any]],
    progress: Progress | None = None,
    label: str = "",
) -> list[GroupResult]:
    """Search every wording of every question with this rewriter. A separate Searcher shares
    the live one's index and settings, so live answers are never touched by the test."""
    searcher = Searcher(
        live.embedder,
        keyword_weight=live.keyword_weight,
        changelog_weight=live.changelog_weight,
        max_per_article=live.max_per_article,
        rewriter=rewriter,
    )
    searcher.vectors = live.vectors
    titles = {
        r["article_id"]: r["title"]
        for r in await db.execute_fetchall("SELECT article_id, title FROM articles")
    }
    groups: list[GroupResult] = []
    for i, case in enumerate(cases, start=1):
        gold = set(case["gold"])
        g = GroupResult(case["id"], split_of(case["id"]), case["gold"], titles.get(case["gold"][0]))
        for kind in KINDS:
            question = case["variants"][kind]
            rewrites = await rewriter.rewrite(question)
            hits = await searcher.search(db, question, k=20, mode="hybrid")
            seen: list[str] = []
            for h in hits:
                if h.article_id not in seen:
                    seen.append(h.article_id)
            rank = next((n for n, aid in enumerate(seen, start=1) if aid in gold), None)
            top = seen[0] if seen else None
            g.variants.append(
                VariantResult(kind, question, rewrites, top, titles.get(top or ""), rank)
            )
        groups.append(g)
        if i % 7 == 0 or i == len(cases):
            done = sum(x.passed for x in groups)
            await report(
                progress,
                "search",
                f"{label}: {i} of {len(cases)} questions, {i * 4} wordings searched",
                f"{done} of {i} groups behave the same so far",
            )
    return groups
