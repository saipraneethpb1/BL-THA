"""Keep the chunk index in step with the articles table, touching only what changed.

An article is re-indexed when its "index signature" changes: the article text plus the
descriptions of its screenshots. So a new image description also refreshes its chunks.

A page publishes when it is finished, never half-way: its new chunks are first written
inactive (`is_active = 0`), and one transaction then removes the old chunks and switches
the new ones on. Search reads only active chunks. An interrupted run leaves at most some
inactive chunks behind, which the next run deletes. Chunks keep a stable key (article id,
position, content hash), so a chunk whose text did not change reuses its vector.
"""

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass

import aiosqlite
import numpy as np

from kb.retrieval.chunker import DEFAULT_CONFIG, Chunk, ChunkingConfig, chunk_article
from kb.retrieval.embedder import Embedder

log = logging.getLogger(__name__)


@dataclass
class IndexReport:
    reindexed: int = 0
    dropped: int = 0
    chunks_written: int = 0
    vectors_reused: int = 0


def index_signature(content_hash: str, descriptions: dict[str, str]) -> str:
    payload = content_hash + json.dumps(descriptions, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


async def _descriptions(db: aiosqlite.Connection, article_id: str) -> dict[str, str]:
    rows = await db.execute_fetchall(
        "SELECT image_id, description FROM images WHERE article_id=? AND description IS NOT NULL",
        (article_id,),
    )
    return {r["image_id"]: r["description"] for r in rows}


async def _reusable_vectors(
    db: aiosqlite.Connection, keys: list[str], model: str
) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for start in range(0, len(keys), 500):  # stay under SQLite's variable limit
        part = keys[start : start + 500]
        marks = ",".join("?" * len(part))
        rows = await db.execute_fetchall(
            f"""SELECT chunk_key, embedding FROM chunks WHERE is_active=1
                AND embedding_model=? AND chunk_key IN ({marks})""",
            [model, *part],
        )
        out.update({r["chunk_key"]: r["embedding"] for r in rows})
    return out


async def sync_index(
    db: aiosqlite.Connection,
    embedder: Embedder,
    config: ChunkingConfig = DEFAULT_CONFIG,
    batch_articles: int = 50,
    run_id: int | None = None,
    force: bool = False,
) -> IndexReport:
    """`force` re-indexes every article (a new chunking config or embedding model)."""
    report = IndexReport()
    model = getattr(embedder, "model_name", type(embedder).__name__)

    # 0. Chunks staged by an interrupted run were never published: drop them.
    await db.execute("DELETE FROM chunks WHERE source='article' AND is_active=0")

    # 1. Drop chunks of articles that were removed from the help center.
    cur = await db.execute(
        """DELETE FROM chunks WHERE source='article'
           AND article_id NOT IN (SELECT article_id FROM articles)"""
    )
    report.dropped = cur.rowcount
    await db.commit()

    # 2. Re-chunk and re-embed articles whose signature changed.
    articles = list(
        await db.execute_fetchall(
            "SELECT article_id, title, body, content_hash, indexed_hash FROM articles"
        )
    )
    stale = []
    for a in articles:
        descs = await _descriptions(db, a["article_id"])
        sig = index_signature(a["content_hash"], descs)
        if force or sig != a["indexed_hash"]:
            stale.append((a, descs, sig))
    log.info("index: %d of %d articles need (re)indexing", len(stale), len(articles))

    for start in range(0, len(stale), batch_articles):
        batch = stale[start : start + batch_articles]
        planned: list[tuple[aiosqlite.Row, str, list[tuple[Chunk, str]]]] = []
        for a, descs, sig in batch:
            pieces = chunk_article(a["title"], a["body"], descs, config)
            planned.append((a, sig, [(c, c.key(a["article_id"])) for c in pieces]))
        keys = [key for _, _, chunks in planned for _, key in chunks]
        reused = await _reusable_vectors(db, keys, model)
        todo = [c.text for _, _, chunks in planned for c, key in chunks if key not in reused]
        # Embedding is CPU-bound; run it in a thread so the API stays responsive.
        vectors = (
            await asyncio.to_thread(embedder.embed_documents, todo)
            if todo
            else np.zeros((0, embedder.dim))
        )
        fresh = iter(vectors)

        # Stage: every new chunk is written inactive; searches cannot see it yet.
        for a, _, chunks in planned:
            for c, key in chunks:
                blob = reused.get(key)
                if blob is None:
                    blob = next(fresh).astype(np.float32).tobytes()
                await db.execute(
                    """INSERT INTO chunks(article_id, source, position, section, text, image_ids,
                       embedding, chunk_key, parent_section, is_active, run_id,
                       embedding_model, embedding_dim)
                       VALUES (?, 'article', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                    (
                        a["article_id"],
                        c.position,
                        c.section,
                        c.text,
                        json.dumps(c.image_ids),
                        blob,
                        key,
                        c.parent,
                        run_id,
                        model,
                        embedder.dim,
                    ),
                )
        await db.commit()

        # Publish: old chunks out and new chunks in, for the whole batch in one transaction,
        # so every page is switched over whole.
        for a, sig, chunks in planned:
            await db.execute(
                "DELETE FROM chunks WHERE source='article' AND article_id=? AND is_active=1",
                (a["article_id"],),
            )
            await db.execute(
                "UPDATE chunks SET is_active=1 WHERE source='article' AND article_id=? AND is_active=0",
                (a["article_id"],),
            )
            await db.execute(
                "UPDATE articles SET indexed_hash=? WHERE article_id=?", (sig, a["article_id"])
            )
            report.reindexed += 1
            report.chunks_written += len(chunks)
        await db.commit()
        report.vectors_reused += len(reused)
        log.info("index: %d/%d articles done", min(start + batch_articles, len(stale)), len(stale))
    return report
