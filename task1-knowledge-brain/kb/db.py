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
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed | quarantined
                                                   -- ('removed' only in databases from before
                                                   -- quarantine: URLs are never deleted now)
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

-- Retrievable pieces of articles (and, later, of approved expert answers).
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'article',  -- article | expert
    position   INTEGER NOT NULL,
    section    TEXT,
    text       TEXT NOT NULL,
    image_ids  TEXT NOT NULL DEFAULT '[]',       -- JSON list
    embedding  BLOB                              -- float32 vector, L2-normalised
);
CREATE INDEX IF NOT EXISTS idx_chunks_article ON chunks(article_id);

-- Keyword index (BM25) kept in sync with chunks by triggers.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='chunk_id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.chunk_id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.chunk_id, old.text);
END;

-- Questions the brain could not answer, waiting for a human expert.
CREATE TABLE IF NOT EXISTS escalations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question    TEXT NOT NULL,
    normalized  TEXT NOT NULL,                 -- for de-duplicating repeat questions
    status      TEXT NOT NULL DEFAULT 'open',  -- open | resolved | dismissed
    reason      TEXT NOT NULL,
    evidence    TEXT NOT NULL DEFAULT '[]',    -- JSON: passages we had, for the expert
    caller      TEXT,                          -- chat | inbox-agent | scope-builder | ...
    times_asked INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_escalations_open ON escalations(status, normalized);

-- Expert answers are versioned knowledge records. Retracting one deactivates it and
-- removes its chunk, so a bad expert answer can be undone without touching anything else.
CREATE TABLE IF NOT EXISTS expert_answers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    escalation_id INTEGER REFERENCES escalations(id),
    question      TEXT NOT NULL,
    answer        TEXT NOT NULL,
    author        TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Every answer we give, for audits and for measuring quality over time.
CREATE TABLE IF NOT EXISTS query_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    question      TEXT NOT NULL,
    status        TEXT NOT NULL,
    model         TEXT,
    prompt_version TEXT,
    top_score     REAL,
    cited         TEXT NOT NULL DEFAULT '[]',
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    latency_ms    REAL,
    caller        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
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

-- Every URL a refresh run fetched, and what happened to it (the Refresh runs panel).
CREATE TABLE IF NOT EXISTS fetch_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES ingestion_runs(id),
    article_id TEXT,
    url        TEXT NOT NULL,
    source     TEXT,      -- 'help-center' or a custom sitemap's URL
    outcome    TEXT NOT NULL,  -- updated | unchanged | failed | quarantined | refused
    detail     TEXT,      -- the error, if any
    fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_run ON fetch_log(run_id, fetched_at);

-- Sitemaps users added on top of the help center's own (see kb/ingestion/sources.py).
CREATE TABLE IF NOT EXISTS custom_sitemaps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL,              -- urlset | sitemapindex
    url_count       INTEGER NOT NULL DEFAULT 0, -- entries found when last checked
    added_by        TEXT,
    added_at        TEXT NOT NULL DEFAULT (datetime('now')),
    last_checked_at TEXT,
    last_error      TEXT
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


# Columns added after the first release. init_db adds any that an older database lacks.
MIGRATIONS = {
    "pages": {
        # Which sitemap listed the page: 'help-center' or a custom sitemap's URL.
        "source": "TEXT NOT NULL DEFAULT 'help-center'",
        # 'helpcenter' (Freshdesk article parser) or 'web' (general page parser).
        "kind": "TEXT NOT NULL DEFAULT 'helpcenter'",
        # When and why a URL was quarantined (kept, but not searched).
        "quarantined_at": "TEXT",
    },
}


async def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            have = {r[1] for r in await db.execute_fetchall(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in have:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        await db.commit()


@asynccontextmanager
async def connect(path: Path) -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA journal_mode = WAL")
        yield db
