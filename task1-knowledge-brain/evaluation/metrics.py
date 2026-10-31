"""Retrieval and gate metrics, graded in code (no judge model).

Ranks are counted by distinct article, not by passage: two passages of one article are
one result. A case's `gold` lists the articles that answer it; any one of them is enough,
except for multi-part cases, whose `parts` each need their own gold article.

- recall@k: share of answerable cases with a gold article in the top k articles (for a
  multi-part case: every part has one). With one gold article per need this is the usual
  recall; it is not "share of all gold articles found".
- MRR@10: mean of 1/rank of the first gold article (0 if not in the top 10).
- nDCG@10: binary relevance, every gold article counts; rewards ranking all of them high.
- Gate: the answer step refuses when the best passage's similarity is below a threshold.
  wrongly_answered = refuse cases that pass the gate; wrongly_refused = answerable cases
  that do not. Both rates are measured on the passages the answer model would see.
"""

import math
import random
from collections.abc import Callable, Sequence
from typing import Any

Case = dict[str, Any]


def distinct(article_ids: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(article_ids))


def first_rank(ranked: Sequence[str], gold: Sequence[str]) -> int | None:
    for rank, aid in enumerate(distinct(ranked), start=1):
        if aid in gold:
            return rank
    return None


def parts_of(case: Case) -> list[list[str]]:
    return case.get("parts") or [case["gold"]]


def hit_at(case: Case, ranked: Sequence[str], k: int) -> bool:
    top = distinct(ranked)[:k]
    return all(any(g in top for g in part) for part in parts_of(case))


def ndcg_at(ranked: Sequence[str], gold: Sequence[str], k: int = 10) -> float:
    top = distinct(ranked)[:k]
    dcg = sum(1 / math.log2(i + 2) for i, aid in enumerate(top) if aid in gold)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(set(gold)), k)))
    return dcg / ideal if ideal else 0.0


def answerable(case: Case) -> bool:
    return bool(case["gold"]) and not case.get("should_refuse")


def case_scores(case: Case, ranked: Sequence[str], top_score: float, threshold: float) -> Case:
    """Everything graded for one case. `ranked` is the article order of a deep search
    (k=20); `top_score` the similarity of the first passage the answer model would see."""
    passes_gate = top_score >= threshold
    if not answerable(case):
        return {
            "answerable": False,
            "top_score": top_score,
            "passes_gate": passes_gate,
            "wrongly_answered": passes_gate,
            "failure": "wrongly answered" if passes_gate else None,
        }
    rank = first_rank(ranked, case["gold"])
    out = {
        "answerable": True,
        "rank": rank,
        "hit@1": rank == 1,
        "hit@5": hit_at(case, ranked, 5),
        "hit@6": hit_at(case, ranked, 6),
        "hit@10": hit_at(case, ranked, 10),
        "rr@10": 1 / rank if rank is not None and rank <= 10 else 0.0,
        "ndcg@10": ndcg_at(ranked, case["gold"]),
        "top_score": top_score,
        "passes_gate": passes_gate,
        "wrongly_refused": not passes_gate,
    }
    if not out["hit@6"]:
        out["failure"] = "multi-part miss" if case.get("parts") and rank else "retrieval miss"
    elif not passes_gate:
        out["failure"] = "wrong refusal"
    else:
        out["failure"] = None
    return out


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


METRICS: dict[str, tuple[bool, str]] = {  # name -> (over answerable cases?, per-case key)
    "hit@1": (True, "hit@1"),
    "recall@5": (True, "hit@5"),
    "recall@10": (True, "hit@10"),
    "in_context@6": (True, "hit@6"),
    "mrr@10": (True, "rr@10"),
    "ndcg@10": (True, "ndcg@10"),
    "wrongly_refused": (True, "wrongly_refused"),
    "wrongly_answered": (False, "wrongly_answered"),
}


def summarise(rows: Sequence[Case]) -> dict[str, float]:
    ans = [r for r in rows if r["answerable"]]
    ref = [r for r in rows if not r["answerable"]]
    out = {
        name: _mean([float(r[key]) for r in (ans if over_ans else ref)])
        for name, (over_ans, key) in METRICS.items()
    }
    out["answerable_cases"] = len(ans)
    out["refuse_cases"] = len(ref)
    return out


def bootstrap_ci(
    rows: Sequence[Case],
    metric: Callable[[Sequence[Case]], float],
    n: int = 1000,
    seed: int = 7,
) -> tuple[float, float]:
    """95% interval from resampling the cases: how much the number would move on another
    set of questions like these. This is the noise that decides whether a gap is real."""
    if not rows:
        return 0.0, 0.0
    rng = random.Random(seed)
    stats = sorted(metric([rng.choice(rows) for _ in rows]) for _ in range(n))
    return stats[int(0.025 * n)], stats[int(0.975 * n) - 1]


def gate_sweep(rows: Sequence[Case], thresholds: Sequence[float]) -> list[dict[str, float]]:
    """Error rates for each candidate threshold, to recalibrate the gate per model.
    A wrong answer costs more than a wrong refusal (which becomes a human task), so the
    suggested threshold minimises wrongly_answered first, then wrongly_refused."""
    ans = [r["top_score"] for r in rows if r["answerable"] and r.get("hit@6")]
    ref = [r["top_score"] for r in rows if not r["answerable"]]
    out = []
    for t in thresholds:
        out.append(
            {
                "threshold": round(t, 3),
                "wrongly_answered": _mean([float(s >= t) for s in ref]),
                "wrongly_refused": _mean([float(s < t) for s in ans]),
            }
        )
    return out


def suggest_threshold(
    sweep: Sequence[dict[str, float]], max_wrongly_refused: float = 0.05
) -> float:
    """Highest-safety threshold that still answers at least 95% of the answerable cases
    whose gold passage was retrieved. Falls back to the lowest-total-error threshold."""
    ok = [s for s in sweep if s["wrongly_refused"] <= max_wrongly_refused]
    if ok:
        best = min(ok, key=lambda s: (s["wrongly_answered"], -s["threshold"]))
        return best["threshold"]
    return min(sweep, key=lambda s: s["wrongly_answered"] + s["wrongly_refused"])["threshold"]
