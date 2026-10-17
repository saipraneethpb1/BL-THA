"""The ingestion job: read the sitemap, decide what to fetch, fetch it politely,
and store clean articles. Safe to stop and restart at any point.

Restart safety comes from the `pages` table: every article is `pending` until it is
stored, so a crashed run simply picks up the remaining `pending`/`failed` rows.
"""

import asyncio
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import aiosqlite
import httpx

from kb.config import Settings
from kb.db import connect, init_db
from kb.ingestion.fetcher import FetchError, PoliteFetcher
from kb.ingestion.parser import ParsedArticle, ParseError, parse_article
from kb.ingestion.sitemap import ManifestRow, SitemapEntry, diff_sitemap, parse_sitemap

log = logging.getLogger(__name__)


@dataclass
class IngestionReport:
    run_id: int
    new: int = 0
    changed: int = 0
    removed: int = 0
    fetched: int = 0
    content_changed: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


async def _apply_diff(db: aiosqlite.Connection, entries: list[SitemapEntry]) -> IngestionReport:
    rows = await db.execute_fetchall("SELECT article_id, lastmod, status FROM pages")
    manifest = [ManifestRow(r["article_id"], r["lastmod"], r["status"]) for r in rows]
    diff = diff_sitemap(entries, manifest)

    cur = await db.execute("INSERT INTO ingestion_runs DEFAULT VALUES")
    report = IngestionReport(run_id=cur.lastrowid or 0)
    report.new, report.changed, report.removed = len(diff.new), len(diff.changed), len(diff.removed)

    for e in diff.new:
        # A previously removed article can come back; reset it to pending.
        await db.execute(
            """INSERT INTO pages(article_id, url, lastmod, status, attempts) VALUES (?, ?, ?, 'pending', 0)
               ON CONFLICT(article_id) DO UPDATE SET url=excluded.url, lastmod=excluded.lastmod,
               status='pending', attempts=0, updated_at=datetime('now')""",
            (e.article_id, e.url, e.lastmod),
        )
    for e in diff.changed:
        await db.execute(
            """UPDATE pages SET url=?, lastmod=?, status='pending', attempts=0,
               updated_at=datetime('now') WHERE article_id=?""",
            (e.url, e.lastmod, e.article_id),
        )
    for article_id in diff.removed:
        await _remove_article(db, article_id, reason="not in sitemap")
    await db.commit()
    return report


async def _remove_article(db: aiosqlite.Connection, article_id: str, reason: str) -> None:
    # Deleting the article cascades to its images; chunks are dropped by the indexer
    # (it deletes chunks whose article no longer exists), so removed pages stop being cited.
    await db.execute("DELETE FROM articles WHERE article_id=?", (article_id,))
    await db.execute(
        """UPDATE pages SET status='removed', last_error=?, updated_at=datetime('now')
           WHERE article_id=?""",
        (reason, article_id),
    )


async def _store_article(
    db: aiosqlite.Connection, article: ParsedArticle, lastmod: str | None
) -> bool:
    """Store the article. Returns True if its content actually changed."""
    rows = list(
        await db.execute_fetchall(
            "SELECT content_hash FROM articles WHERE article_id=?", (article.article_id,)
        )
    )
    new_hash = article.content_hash
    changed = not rows or rows[0]["content_hash"] != new_hash
    if changed:
        await db.execute(
            """INSERT INTO articles(article_id, url, title, category, folder, body, content_hash, lastmod)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(article_id) DO UPDATE SET url=excluded.url, title=excluded.title,
               category=excluded.category, folder=excluded.folder, body=excluded.body,
               content_hash=excluded.content_hash, lastmod=excluded.lastmod,
               ingested_at=datetime('now')""",
            (
                article.article_id,
                article.url,
                article.title,
                article.category,
                article.folder,
                article.body,
                new_hash,
                lastmod,
            ),
        )
        # Keep descriptions of images that survived the edit, so we do not pay for them twice.
        old = await db.execute_fetchall(
            "SELECT src, description, described_with FROM images WHERE article_id=?",
            (article.article_id,),
        )
        known = {r["src"]: (r["description"], r["described_with"]) for r in old}
        await db.execute("DELETE FROM images WHERE article_id=?", (article.article_id,))
        for img in article.images:
            desc, model = known.get(img.src, (None, None))
            await db.execute(
                """INSERT OR REPLACE INTO images(image_id, article_id, position, src, section,
                   context_before, context_after, description, described_with)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    img.image_id,
                    article.article_id,
                    img.position,
                    img.src,
                    img.section,
                    img.context_before,
                    img.context_after,
                    desc,
                    model,
                ),
            )
    else:
        await db.execute(
            "UPDATE articles SET lastmod=? WHERE article_id=?", (lastmod, article.article_id)
        )
    await db.execute(
        """UPDATE pages SET status='done', content_hash=?, last_error=NULL,
           fetched_at=datetime('now'), updated_at=datetime('now') WHERE article_id=?""",
        (new_hash, article.article_id),
    )
    return changed


async def _process_one(
    db: aiosqlite.Connection,
    db_lock: asyncio.Lock,
    fetcher: PoliteFetcher,
    article_id: str,
    url: str,
    lastmod: str | None,
    report: IngestionReport,
) -> None:
    try:
        html = await fetcher.get_text(url)
        article = parse_article(html, article_id, url)
    except FetchError as exc:
        async with db_lock:
            report.failed += 1
            if exc.permanent:
                await _remove_article(db, article_id, reason=str(exc))
            else:
                await _mark_failed(db, article_id, str(exc))
            await db.commit()
        log.warning("fetch failed %s: %s", url, exc)
        return
    except ParseError as exc:
        async with db_lock:
            report.failed += 1
            await _mark_failed(db, article_id, str(exc))
            await db.commit()
        log.warning("parse failed %s: %s", url, exc)
        return

    async with db_lock:
        changed = await _store_article(db, article, lastmod)
        report.fetched += 1
        report.content_changed += int(changed)
        await db.commit()
    log.info("stored %s (%s)%s", article_id, article.title, "" if changed else " [unchanged]")


async def _mark_failed(db: aiosqlite.Connection, article_id: str, error: str) -> None:
    await db.execute(
        """UPDATE pages SET status='failed', attempts=attempts+1, last_error=?,
           updated_at=datetime('now') WHERE article_id=?""",
        (error, article_id),
    )


async def run_ingestion(
    settings: Settings,
    *,
    limit: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> IngestionReport:
    await init_db(settings.db_path)
    headers = {"User-Agent": settings.user_agent}
    async with (
        httpx.AsyncClient(
            headers=headers,
            timeout=settings.request_timeout_s,
            follow_redirects=True,
            transport=transport,
        ) as client,
        connect(settings.db_path) as db,
    ):
        fetcher = PoliteFetcher(
            client, settings.max_concurrency, settings.min_interval_s, settings.max_attempts
        )
        sitemap_xml = await fetcher.get_text(settings.sitemap_url)
        entries = parse_sitemap(sitemap_xml)
        report = await _apply_diff(db, entries)
        log.info("sitemap: %d articles, diff=%s", len(entries), report.as_dict())

        # Work queue = everything not yet stored, including leftovers from a crashed run.
        sql = """SELECT article_id, url, lastmod FROM pages
                 WHERE status='pending' OR (status='failed' AND attempts < ?)
                 ORDER BY article_id"""
        params: list[object] = [settings.max_attempts]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        todo = await db.execute_fetchall(sql, params)

        db_lock = asyncio.Lock()
        try:
            await asyncio.gather(
                *(
                    _process_one(
                        db, db_lock, fetcher, r["article_id"], r["url"], r["lastmod"], report
                    )
                    for r in todo
                )
            )
            status, error = "finished", None
        except BaseException as exc:  # record crashes (incl. Ctrl+C) before re-raising
            status, error = "crashed", repr(exc)
            raise
        finally:
            await db.execute(
                """UPDATE ingestion_runs SET finished_at=datetime('now'), status=?, new=?, changed=?,
                   removed=?, fetched=?, content_changed=?, failed=?, error=? WHERE id=?""",
                (
                    status,
                    report.new,
                    report.changed,
                    report.removed,
                    report.fetched,
                    report.content_changed,
                    report.failed,
                    error,
                    report.run_id,
                ),
            )
            await db.commit()
        return report


def main() -> None:
    import argparse

    from kb.config import settings

    parser = argparse.ArgumentParser(description="Crawl the help center into the Knowledge Brain.")
    parser.add_argument("--limit", type=int, default=None, help="process at most N pages this run")
    parser.add_argument("--db", type=Path, default=None, help="override the database path")
    args = parser.parse_args()
    if args.db:
        settings.db_path = args.db
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    report = asyncio.run(run_ingestion(settings, limit=args.limit))
    print(report.as_dict())


if __name__ == "__main__":
    main()
