"""Keep the chunk index in step with the articles table, touching only what changed.

An article is re-indexed when its "index signature" changes: the article text plus the
descriptions of its screenshots. So a new image description also refreshes its chunks.
"""

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass

import aiosqlite
import numpy as np

from kb.retrieval.chunker import DEFAULT_CONFIG, ChunkingConfig, chunk_article
from kb.retrieval.embedder import Embedder

log = logging.getLogger(__name__)


@dataclass
class IndexReport:
    reindexed: int = 0
    dropped: int = 0
    chunks_written: int = 0


def index_signature(content_hash: str, descriptions: dict[str, str]) -> str:
    payload = content_hash + json.dumps(descriptions, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


async def _descriptions(db: aiosqlite.Connection, article_id: str) -> dict[str, str]:
    rows = await db.execute_fetchall(
        "SELECT image_id, description FROM images WHERE article_id=? AND description IS NOT NULL",
        (article_id,),
    )
    return {r["image_id"]: r["description"] for r in rows}


async def sync_index(
    db: aiosqlite.Connection,
    embedder: Embedder,
    config: ChunkingConfig = DEFAULT_CONFIG,
    batch_articles: int = 50,
) -> IndexReport:
    report = IndexReport()

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
        if sig != a["indexed_hash"]:
            stale.append((a, descs, sig))
    log.info("index: %d of %d articles need (re)indexing", len(stale), len(articles))

    for start in range(0, len(stale), batch_articles):
        batch = stale[start : start + batch_articles]
        planned = [
            (a, sig, chunk_article(a["title"], a["body"], descs, config)) for a, descs, sig in batch
        ]
        texts = [c.text for _, _, chunks in planned for c in chunks]
        # Embedding is CPU-bound; run it in a thread so the API stays responsive.
        vectors = (
            await asyncio.to_thread(embedder.embed_documents, texts)
            if texts
            else np.zeros((0, embedder.dim))
        )
        i = 0
        for a, sig, chunks in planned:
            await db.execute(
                "DELETE FROM chunks WHERE source='article' AND article_id=?", (a["article_id"],)
            )
            for c in chunks:
                await db.execute(
                    """INSERT INTO chunks(article_id, source, position, section, text, image_ids,
                       embedding) VALUES (?, 'article', ?, ?, ?, ?, ?)""",
                    (
                        a["article_id"],
                        c.position,
                        c.section,
                        c.text,
                        json.dumps(c.image_ids),
                        vectors[i].astype(np.float32).tobytes(),
                    ),
                )
                i += 1
            await db.execute(
                "UPDATE articles SET indexed_hash=? WHERE article_id=?", (sig, a["article_id"])
            )
            report.reindexed += 1
            report.chunks_written += len(chunks)
        await db.commit()  # commit per batch so an interrupted index run keeps its progress
        log.info("index: %d/%d articles done", min(start + batch_articles, len(stale)), len(stale))
    return report
