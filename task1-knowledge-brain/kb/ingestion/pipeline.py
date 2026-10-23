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
from kb.ingestion import health
from kb.ingestion.fetcher import FetchError, PoliteFetcher
from kb.ingestion.parser import ParsedArticle, ParseError, parse_article, parse_web_page
from kb.ingestion.sitemap import ManifestRow, SitemapEntry, diff_sitemap, parse_sitemap
from kb.ingestion.sources import (
    HostGuard,
    Resolver,
    SitemapError,
    fetch_sitemap,
    guarded_client,
    parse_any_sitemap,
    system_resolver,
    web_article_id,
)

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


async def _custom_entries(
    db: aiosqlite.Connection, client: httpx.AsyncClient, settings: Settings
) -> tuple[list[SitemapEntry], frozenset[str]]:
    """Pages from users' own sitemaps. A sitemap that fails to load this time keeps its
    existing pages (returned as `keep`): a temporary outage shouldn't wipe a site."""
    entries: list[SitemapEntry] = []
    keep: set[str] = set()
    for row in await db.execute_fetchall("SELECT id, url FROM custom_sitemaps ORDER BY id"):
        found: list[tuple[str, str | None]] = []
        try:
            parsed = parse_any_sitemap(await fetch_sitemap(client, row["url"]))
            if parsed.kind == "urlset":
                found = parsed.locs
            else:  # a sitemap index: read the first few child sitemaps
                for child, _ in parsed.locs[: settings.custom_sitemap_max_children]:
                    try:
                        sub = parse_any_sitemap(await fetch_sitemap(client, child))
                    except SitemapError as exc:
                        log.warning("child sitemap %s skipped: %s", child, exc)
                        continue
                    if sub.kind == "urlset":
                        found.extend(sub.locs)
                    if len(found) >= settings.custom_sitemap_max_pages:
                        break
            found = found[: settings.custom_sitemap_max_pages]
            await db.execute(
                """UPDATE custom_sitemaps SET url_count=?, last_checked_at=datetime('now'),
                   last_error=NULL WHERE id=?""",
                (len(found), row["id"]),
            )
        except SitemapError as exc:
            log.warning("custom sitemap %s failed: %s", row["url"], exc)
            await db.execute(
                "UPDATE custom_sitemaps SET last_checked_at=datetime('now'), last_error=? WHERE id=?",
                (str(exc), row["id"]),
            )
            kept = await db.execute_fetchall(
                "SELECT article_id FROM pages WHERE source=? AND status NOT IN ('removed', 'quarantined')",
                (row["url"],),
            )
            keep.update(r["article_id"] for r in kept)
            continue
        entries.extend(
            SitemapEntry(web_article_id(loc), loc, lastmod, source=row["url"], kind="web")
            for loc, lastmod in found
        )
    return entries, frozenset(keep)


async def _apply_diff(
    db: aiosqlite.Connection, entries: list[SitemapEntry], keep: frozenset[str] = frozenset()
) -> tuple[IngestionReport, set[str]]:
    """Returns the report and the ids missing from their sitemap, for URL health checks."""
    rows = await db.execute_fetchall("SELECT article_id, lastmod, status FROM pages")
    manifest = [ManifestRow(r["article_id"], r["lastmod"], r["status"]) for r in rows]
    diff = diff_sitemap(entries, manifest, keep)

    cur = await db.execute("INSERT INTO ingestion_runs DEFAULT VALUES")
    report = IngestionReport(run_id=cur.lastrowid or 0)
    report.new, report.changed, report.removed = len(diff.new), len(diff.changed), len(diff.removed)

    for e in diff.new:
        # A previously removed article can come back; reset it to pending.
        await db.execute(
            """INSERT INTO pages(article_id, url, lastmod, status, attempts, source, kind)
               VALUES (?, ?, ?, 'pending', 0, ?, ?)
               ON CONFLICT(article_id) DO UPDATE SET url=excluded.url, lastmod=excluded.lastmod,
               status='pending', attempts=0, source=excluded.source, kind=excluded.kind,
               updated_at=datetime('now')""",
            (e.article_id, e.url, e.lastmod, e.source, e.kind),
        )
    for e in diff.changed:
        await db.execute(
            """UPDATE pages SET url=?, lastmod=?, status='pending', attempts=0,
               updated_at=datetime('now') WHERE article_id=?""",
            (e.url, e.lastmod, e.article_id),
        )
    # Missing from its sitemap: re-checked by URL health, never quarantined on sight. The
    # exception is a page whose custom sitemap was removed: that was a person's decision.
    active = {"help-center"} | {
        r["url"] for r in await db.execute_fetchall("SELECT url FROM custom_sitemaps")
    }
    absent: set[str] = set()
    for article_id in diff.removed:
        rows = list(
            await db.execute_fetchall("SELECT source FROM pages WHERE article_id=?", (article_id,))
        )
        if rows and rows[0]["source"] not in active:
            await _quarantine(db, article_id, reason="its custom sitemap was removed")
        else:
            absent.add(article_id)
    await db.commit()
    return report, absent


async def _quarantine(db: aiosqlite.Connection, article_id: str, reason: str) -> None:
    """Nothing is ever deleted. A quarantined URL keeps its text and index entries, but
    search skips it (kb/retrieval/search.py). If it's listed and fetched successfully again,
    it's stored as normal and is back in search."""
    await db.execute(
        """UPDATE pages SET status='quarantined', last_error=?,
           quarantined_at=coalesce(quarantined_at, datetime('now')), updated_at=datetime('now')
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
        """UPDATE pages SET status='done', content_hash=?, last_error=NULL, quarantined_at=NULL,
           fetched_at=datetime('now'), updated_at=datetime('now') WHERE article_id=?""",
        (new_hash, article.article_id),
    )
    return changed


async def _log_fetch(
    db: aiosqlite.Connection,
    report: IngestionReport,
    article_id: str,
    url: str,
    source: str,
    outcome: str,
    detail: str | None = None,
) -> None:
    await db.execute(
        """INSERT INTO fetch_log(run_id, article_id, url, source, outcome, detail)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (report.run_id, article_id, url, source, outcome, detail),
    )


async def _health_checks(
    db: aiosqlite.Connection,
    db_lock: asyncio.Lock,
    fetchers: dict[str, PoliteFetcher],
    hs: health.HealthSettings,
    report: IngestionReport,
    absent: set[str],
    skip: set[str],
) -> None:
    """Re-check the URLs that may need a decision: those missing from their sitemap and
    those already being counted. Only these are re-checked, not every page every night."""
    targets = (absent | await health.open_ids(db)) - skip
    if not targets:
        return
    marks = ",".join("?" * len(targets))
    rows = await db.execute_fetchall(
        f"SELECT article_id, url, kind, source FROM pages WHERE article_id IN ({marks}) AND status != 'quarantined'",
        list(targets),
    )

    async def check(r: aiosqlite.Row) -> None:
        aid, url = r["article_id"], r["url"]
        missing = aid in absent
        try:
            status, location = await fetchers[r["kind"]].probe(url)
        except SitemapError as exc:  # a custom page now pointing at a private address
            async with db_lock:
                await _quarantine(db, aid, reason=str(exc))
                await _log_fetch(db, report, aid, url, r["source"], "refused", str(exc))
                await db.commit()
            return
        issue: str | None
        if status in (404, 410) and missing:
            issue, outcome = "gone", "missing"
        elif status in (301, 308):
            issue, outcome = "moved", "moved"
        elif status == 200:
            issue, outcome = None, "checked"
        else:  # unreachable, a server error or a temporary redirect: no evidence either way
            async with db_lock:
                why = f"HTTP {status}" if status else "couldn't be reached"
                await _log_fetch(db, report, aid, url, r["source"], "unreachable", why)
                await db.commit()
            return
        async with db_lock:
            state = await health.observe(
                db, hs, aid, url, issue, status=status, redirect_to=location
            )
            row = list(
                await db.execute_fetchall("SELECT count FROM url_health WHERE article_id=?", (aid,))
            )
            detail: str | None = None
            if issue is not None and row:
                detail = f"HTTP {status}, {row[0]['count']} of {hs.threshold} refreshes in a row ({state})"
                if location:
                    detail += f", redirects to {location}"
            elif missing:
                detail = "not in its sitemap, but still answers 200"
            await _log_fetch(db, report, aid, url, r["source"], outcome, detail)
            await db.commit()

    await asyncio.gather(*(check(r) for r in rows))


async def _process_one(
    db: aiosqlite.Connection,
    db_lock: asyncio.Lock,
    fetcher: PoliteFetcher,
    article_id: str,
    url: str,
    lastmod: str | None,
    report: IngestionReport,
    kind: str = "helpcenter",
    source: str = "help-center",
    hs: health.HealthSettings | None = None,
    fetched_ids: set[str] | None = None,
) -> None:
    try:
        fetched = await fetcher.fetch(url)
        html = fetched.text
        parse = parse_web_page if kind == "web" else parse_article
        article = parse(html, article_id, url)
    except SitemapError as exc:  # a custom page pointing at a private address
        async with db_lock:
            report.failed += 1
            await _quarantine(db, article_id, reason=str(exc))
            await _log_fetch(db, report, article_id, url, source, "refused", str(exc))
            await db.commit()
        log.warning("refused %s: %s", url, exc)
        return
    except FetchError as exc:
        async with db_lock:
            report.failed += 1
            # A page still listed in its sitemap that answers 404 isn't one of the URL health
            # rules (those need it to be missing too), so it's recorded as failed, not quarantined.
            await _mark_failed(db, article_id, str(exc))
            await _log_fetch(db, report, article_id, url, source, "failed", str(exc))
            await db.commit()
        log.warning("fetch failed %s: %s", url, exc)
        return
    except ParseError as exc:
        async with db_lock:
            report.failed += 1
            await _mark_failed(db, article_id, str(exc))
            await _log_fetch(db, report, article_id, url, source, "failed", str(exc))
            await db.commit()
        log.warning("parse failed %s: %s", url, exc)
        return

    async with db_lock:
        changed = await _store_article(db, article, lastmod)
        report.fetched += 1
        report.content_changed += int(changed)
        detail = None
        if hs is not None:  # a followed 301/308 counts towards URL health; a clean fetch resets it
            issue = "moved" if fetched.moved_to else None
            state = await health.observe(
                db,
                hs,
                article_id,
                url,
                issue,
                status=301 if issue else 200,
                redirect_to=fetched.moved_to,
            )
            if issue:
                detail = f"permanent redirect to {fetched.moved_to} ({state})"
        if fetched_ids is not None:
            fetched_ids.add(article_id)
        outcome = "updated" if changed else "unchanged"
        await _log_fetch(db, report, article_id, url, source, outcome, detail)
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
    resolver: Resolver | None = None,
) -> IngestionReport:
    await init_db(settings.db_path)
    headers = {"User-Agent": settings.user_agent}
    guard = HostGuard(resolver or system_resolver)
    async with (
        httpx.AsyncClient(
            headers=headers,
            timeout=settings.request_timeout_s,
            follow_redirects=True,
            transport=transport,
        ) as client,
        # Custom sitemaps and their pages come from users: every request is address-checked.
        guarded_client(
            guard,
            headers=headers,
            timeout=settings.request_timeout_s,
            follow_redirects=True,
            transport=transport,
        ) as web_client,
        connect(settings.db_path) as db,
    ):
        fetcher = PoliteFetcher(
            client, settings.max_concurrency, settings.min_interval_s, settings.max_attempts
        )
        web_fetcher = PoliteFetcher(
            web_client, settings.max_concurrency, settings.min_interval_s, settings.max_attempts
        )
        sitemap_xml = await fetcher.get_text(settings.sitemap_url)
        entries = parse_sitemap(sitemap_xml)
        custom, keep = await _custom_entries(db, web_client, settings)
        report, absent = await _apply_diff(db, entries + custom, keep)
        hs = await health.get_settings(db)
        fetched_ids: set[str] = set()
        log.info(
            "sitemaps: %d help-center articles, %d custom pages, diff=%s",
            len(entries),
            len(custom),
            report.as_dict(),
        )

        # Work queue = everything not yet stored, including leftovers from a crashed run.
        sql = """SELECT article_id, url, lastmod, kind, source FROM pages
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
                        db,
                        db_lock,
                        web_fetcher if r["kind"] == "web" else fetcher,
                        r["article_id"],
                        r["url"],
                        r["lastmod"],
                        report,
                        r["kind"],
                        r["source"],
                        hs,
                        fetched_ids,
                    )
                    for r in todo
                )
            )
            if limit is None:  # partial runs skip health checks
                await _health_checks(
                    db,
                    db_lock,
                    {"helpcenter": fetcher, "web": web_fetcher},
                    hs,
                    report,
                    absent,
                    fetched_ids,
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
