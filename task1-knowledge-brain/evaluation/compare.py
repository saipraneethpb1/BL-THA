"""Compare two retrieval runs on the same cases: is the difference real or noise?

    uv run python -m evaluation.compare evaluation/results/A.json evaluation/results/B.json
    uv run python -m evaluation.compare A.json B.json --config-a production --config-b "+ rerank"

Prints each metric for A and B, the difference, and a 95% interval for the difference from
a paired bootstrap (the same resampled cases for both, which is far tighter than comparing
two separate intervals). A difference whose interval contains 0 is not shown to be real.
Also lists every case that flipped, so a gain that breaks other questions is visible.
"""

import argparse
import json
import random
from pathlib import Path
from typing import Any

PAIRED = [  # (metric, per-case key, over answerable cases?)
    ("recall@5", "hit@5", True),
    ("recall@10", "hit@10", True),
    ("mrr@10", "rr@10", True),
    ("ndcg@10", "ndcg@10", True),
    ("in_context@6", "hit@6", True),
    ("wrongly_answered", "wrongly_answered", False),
]


def per_case(path: Path, config: str | None) -> tuple[str, dict[str, dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    name = config or next(iter(data["results"]))
    return name, {r["id"]: r for r in data["results"][name]["per_case"]}


def paired(
    a: list[float], b: list[float], n: int = 2000, seed: int = 7
) -> tuple[float, float, float]:
    diff = sum(b) / len(b) - sum(a) / len(a)
    rng = random.Random(seed)
    idx = range(len(a))
    stats = []
    for _ in range(n):
        sample = [rng.choice(idx) for _ in idx]
        stats.append(sum(b[i] - a[i] for i in sample) / len(sample))
    stats.sort()
    return diff, stats[int(0.025 * n)], stats[int(0.975 * n) - 1]


def compare(a: dict[str, dict[str, Any]], b: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    ids = sorted(set(a) & set(b))
    out = []
    for metric, key, over_ans in PAIRED:
        use = [i for i in ids if a[i]["answerable"] == over_ans]
        if not use:
            continue
        va = [float(a[i][key]) for i in use]
        vb = [float(b[i][key]) for i in use]
        diff, lo, hi = paired(va, vb)
        out.append(
            {
                "metric": metric,
                "a": sum(va) / len(va),
                "b": sum(vb) / len(vb),
                "diff": diff,
                "ci95": (lo, hi),
                "real": lo > 0 or hi < 0,
                "n": len(use),
            }
        )
    return out


def flips(
    a: dict[str, dict[str, Any]], b: dict[str, dict[str, Any]]
) -> tuple[list[str], list[str]]:
    fixed, broken = [], []
    for i in sorted(set(a) & set(b)):
        key = "hit@5" if a[i]["answerable"] else "passes_gate"
        good_a = a[i][key] if a[i]["answerable"] else not a[i][key]
        good_b = b[i][key] if b[i]["answerable"] else not b[i][key]
        if good_b and not good_a:
            fixed.append(i)
        elif good_a and not good_b:
            broken.append(i)
    return fixed, broken


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("a", type=Path)
    ap.add_argument("b", type=Path)
    ap.add_argument("--config-a")
    ap.add_argument("--config-b")
    args = ap.parse_args()
    name_a, a = per_case(args.a, args.config_a)
    name_b, b = per_case(args.b, args.config_b)
    print(f"A = {name_a} ({args.a.name})\nB = {name_b} ({args.b.name})\n")
    print("| metric | A | B | B − A | 95% CI (paired) | real? |")
    print("|---|---|---|---|---|---|")
    for r in compare(a, b):
        lo, hi = r["ci95"]
        print(
            f"| {r['metric']} (n={r['n']}) | {r['a']:.3f} | {r['b']:.3f} | {r['diff']:+.3f} | "
            f"{lo:+.3f} to {hi:+.3f} | {'yes' if r['real'] else 'no'} |"
        )
    fixed, broken = flips(a, b)
    print(f"\nFixed by B: {', '.join(fixed) or '-'}")
    print(f"Broken by B: {', '.join(broken) or '-'}")


if __name__ == "__main__":
    main()
