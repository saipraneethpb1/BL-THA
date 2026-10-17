"""SQLite storage. One file holds the crawl manifest, articles and images.

Keeping everything in one file makes the service easy to back up, ship as a seed
snapshot, and explain. Chunks and vectors are added by the retrieval module.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA = """
-- One row per article URL in the sitemap. This is the resumable crawl manifest.
CREATE TABLE IF NOT EXISTS pages (
    article_id   TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    lastmod      TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed | removed
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    content_hash TEXT,
    fetched_at   TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(status);

-- The cleaned article, independent of the original HTML.
CREATE TABLE IF NOT EXISTS articles (
    article_id   TEXT PRIMARY KEY REFERENCES pages(article_id),
    url          TEXT NOT NULL,
    title        TEXT NOT NULL,
    category     TEXT,
    folder       TEXT,
    body         TEXT NOT NULL,   -- markdown-like text with [IMAGE:<id>] markers
    content_hash TEXT NOT NULL,
    lastmod      TEXT,
    indexed_hash TEXT,            -- content_hash at last chunk/embed; differs => re-index
    ingested_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per ingestion run, for the status endpoint and the daily job's audit trail.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',  -- running | finished | crashed
    new         INTEGER NOT NULL DEFAULT 0,
    changed     INTEGER NOT NULL DEFAULT 0,
    removed     INTEGER NOT NULL DEFAULT 0,
    fetched     INTEGER NOT NULL DEFAULT 0,
    content_changed INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

-- Screenshots, kept with the text around them so they make sense on their own.
CREATE TABLE IF NOT EXISTS images (
    image_id       TEXT NOT NULL,
    article_id     TEXT NOT NULL REFERENCES articles(article_id) ON DELETE CASCADE,
    position       INTEGER NOT NULL,
    src            TEXT NOT NULL,
    section        TEXT,
    context_before TEXT,
    context_after  TEXT,
    description    TEXT,          -- filled by the vision step
    described_with TEXT,          -- model id used, for audit
    PRIMARY KEY (article_id, image_id)
);
"""


async def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


@asynccontextmanager
async def connect(path: Path) -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA journal_mode = WAL")
        yield db
