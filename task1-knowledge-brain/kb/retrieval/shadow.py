"""Shadow indexes: build a candidate index beside the live one, evaluate it, then switch.

    uv run python -m kb.retrieval.shadow build e5 --model intfloat/e5-large-v2
    uv run python -m kb.retrieval.shadow build overlap12 --overlap 0.12
    uv run python -m evaluation.run_retrieval_eval --db data/shadow/e5.db --embedding intfloat/e5-large-v2

`build` copies the live database (a consistent copy, kb/snapshot.py) to data/shadow/<name>.db
and re-indexes every article there with the new model or chunking. The live index is not
touched, so answers keep coming from it while the shadow is built and measured. Both
databases record the shadow in `index_meta` (model, dimension, input preparation,
chunking, date). Expert-answer chunks are re-embedded too: every vector in an index
must come from the same model.

Switching the live index to a shadow is a deliberate step once its eval numbers are
recorded: re-index the live database with the shadow's settings (`KB_EMBEDDING_MODEL`
and the chunking), which publishes page by page and reuses unchanged vectors.
"""

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

import aiosqlite
import numpy as np

from kb.config import settings
from kb.db import connect, init_db
from kb.retrieval.chunker import ChunkingConfig
from kb.retrieval.embedder import Embedder, make_embedder
from kb.retrieval.indexer import IndexReport, sync_index
from kb.snapshot import make_snapshot

log = logging.getLogger(__name__)


async def record(
    db: aiosqlite.Connection,
    name: str,
    embedder: Embedder,
    config: ChunkingConfig,
    status: str,
    path: Path | None,
) -> None:
    [n] = await db.execute_fetchall("SELECT count(*) AS n FROM chunks WHERE is_active=1")
    await db.execute(
        """INSERT INTO index_meta(name, embedding_model, dim, input_type, chunking, status, path, chunks)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET embedding_model=excluded.embedding_model,
             dim=excluded.dim, input_type=excluded.input_type, chunking=excluded.chunking,
             status=excluded.status, path=excluded.path, chunks=excluded.chunks,
             created_at=strftime('%Y-%m-%dT%H:%M:%SZ', 'now')""",
        (
            name,
            embedder.model_name,
            embedder.dim,
            embedder.input_type,
            json.dumps(config.describe()),
            status,
            str(path) if path else None,
            n["n"],
        ),
    )
    await db.commit()


async def _reembed_other_chunks(db: aiosqlite.Connection, embedder: Embedder) -> int:
    rows = list(
        await db.execute_fetchall(
            "SELECT chunk_id, text FROM chunks WHERE source != 'article' AND is_active=1"
        )
    )
    if rows:
        vecs = await asyncio.to_thread(embedder.embed_documents, [r["text"] for r in rows])
        for r, v in zip(rows, vecs, strict=True):
            await db.execute(
                "UPDATE chunks SET embedding=?, embedding_model=?, embedding_dim=? WHERE chunk_id=?",
                (v.astype(np.float32).tobytes(), embedder.model_name, embedder.dim, r["chunk_id"]),
            )
        await db.commit()
    return len(rows)


async def build_shadow(
    live: Path, name: str, embedder: Embedder, config: ChunkingConfig, out_dir: Path
) -> tuple[Path, IndexReport]:
    out = out_dir / f"{name}.db"
    make_snapshot(live, out)
    await init_db(out)
    async with connect(out) as db:
        await record(db, name, embedder, config, "building", out)
        report = await sync_index(db, embedder, config, force=True)
        await _reembed_other_chunks(db, embedder)
        await record(db, name, embedder, config, "shadow", out)
    async with connect(live) as db:
        await record_in_live(db, out, name)
    return out, report


async def record_in_live(live_db: aiosqlite.Connection, shadow: Path, name: str) -> None:
    """Copy the shadow's index_meta row into the live database, so the live service
    knows which candidates exist and where."""
    async with connect(shadow) as db:
        [row] = await db.execute_fetchall("SELECT * FROM index_meta WHERE name=?", (name,))
    cols = list(row.keys())
    await live_db.execute(
        f"INSERT OR REPLACE INTO index_meta({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [row[c] for c in cols],
    )
    await live_db.commit()


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("name")
    b.add_argument("--model", default=settings.embedding_model)
    b.add_argument("--overlap", type=float, default=ChunkingConfig.overlap)
    b.add_argument("--heading-path", action="store_true")
    b.add_argument("--max-chars", type=int, default=ChunkingConfig.max_chars)
    b.add_argument("--min-chars", type=int, default=ChunkingConfig.min_chars)
    b.add_argument("--out-dir", type=Path, default=Path("data/shadow"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = ChunkingConfig(args.max_chars, args.min_chars, args.overlap, args.heading_path)
    embedder = make_embedder(args.model, settings.model_cache_dir)
    t = time.perf_counter()
    out, report = asyncio.run(
        build_shadow(settings.db_path, args.name, embedder, config, args.out_dir)
    )
    print(f"{out}: {report}, {time.perf_counter() - t:.0f} s, {out.stat().st_size / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
