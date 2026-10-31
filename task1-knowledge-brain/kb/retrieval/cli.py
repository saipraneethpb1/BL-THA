"""Command line: `python -m kb.retrieval.cli index [--force]` or `... search "question"`.

`index --force` re-chunks and re-embeds every article with the current chunking and model
(after a shadow index passed the eval, kb/retrieval/shadow.py). Pages switch over one by
one, whole, and chunks whose text did not change keep their vectors."""

import argparse
import asyncio
import logging
import time

from kb.config import settings
from kb.db import connect, init_db
from kb.retrieval.embedder import make_embedder
from kb.retrieval.indexer import sync_index
from kb.retrieval.search import Searcher


async def _index(force: bool) -> None:
    await init_db(settings.db_path)
    embedder = make_embedder(settings.embedding_model, settings.model_cache_dir)
    async with connect(settings.db_path) as db:
        t = time.perf_counter()
        report = await sync_index(db, embedder, force=force)
    print(report, f"{time.perf_counter() - t:.1f}s")


async def _search(question: str, mode: str, k: int) -> None:
    searcher = Searcher(make_embedder(settings.embedding_model, settings.model_cache_dir))
    async with connect(settings.db_path) as db:
        hits = await searcher.search(db, question, k=k, mode=mode)  # type: ignore[arg-type]
    for h in hits:
        print(f"{h.score:.3f}  {h.title} > {h.section}  {h.url}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index").add_argument("--force", action="store_true")
    s = sub.add_parser("search")
    s.add_argument("question")
    s.add_argument("--mode", default="hybrid", choices=["hybrid", "vector", "keyword"])
    s.add_argument("-k", type=int, default=5)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd == "index":
        asyncio.run(_index(args.force))
    else:
        asyncio.run(_search(args.question, args.mode, args.k))


if __name__ == "__main__":
    main()
