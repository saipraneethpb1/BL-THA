"""Robustness eval: does every wording of a question find the same right article?

Usage: uv run python -m evaluation.run_robustness_eval [--version N]
Runs the robustness check (kb/tuning/robustness.py) on a rewrite prompt version from the
database (the active one by default): 42 questions, each as asked, with synonyms,
misspelled, and with other names for the feature. Writes evaluation/results/robustness_*.json.
"""

import argparse
import asyncio

from evaluation.common import load_root_env, save, table
from kb.config import settings
from kb.db import connect, init_db
from kb.retrieval.embedder import FastEmbedder
from kb.retrieval.rewrite import ClaudeQueryRewriter
from kb.retrieval.search import Searcher
from kb.tuning import registry, robustness, writer


async def main(version: int | None) -> None:
    load_root_env()
    await init_db(settings.db_path)
    searcher = Searcher(FastEmbedder(settings.embedding_model, settings.model_cache_dir))
    async with connect(settings.db_path) as db:
        await registry.ensure_seeded(db, writer.NAME, "rewrite_v1")
        v = await (
            registry.get(db, writer.NAME, version) if version else registry.active(db, writer.NAME)
        )
        await searcher.vectors.refresh(db)
        rewriter = ClaudeQueryRewriter(settings.rewrite_model, system=v["text"])
        groups = await robustness.run(db, searcher, rewriter, robustness.load_cases())
    s = robustness.summary(groups)
    rows = [
        [
            split,
            f"{s[split]['passed']}/{s[split]['groups']}",
            f"{s[split]['hit_rate']:.0%}",
            *(f"{s[split]['by_kind'][k]:.0%}" for k in robustness.KINDS),
        ]
        for split in ("all", "train", "holdout")
    ]
    print(f"rewrite prompt v{v['version']}")
    print(
        table(
            ["split", "every wording same + right", "wordings right (top 5)", *robustness.KINDS],
            rows,
        )
    )
    path = save(
        "robustness", {"version": v["version"], "summary": s, "groups": robustness.to_json(groups)}
    )
    print(f"saved {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", type=int, default=None)
    asyncio.run(main(parser.parse_args().version))
