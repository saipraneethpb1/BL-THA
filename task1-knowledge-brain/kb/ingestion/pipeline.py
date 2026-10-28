"""The ingestion job: read the sitemap, decide what to fetch, fetch it politely,
and store clean articles. Safe to stop and restart at any point.

Restart safety comes from the `pages` table: every article is `pending` until it is
stored, so a crashed run simply picks up the remaining `pending`/`failed` rows.
"""

import asyncio
import json
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urljoin

import aiosqlite
import httpx

from kb.config import Settings
from kb.db import connect, init_db
from kb.ingestion import catalog, health
from kb.ingestion.fetcher import FetchError, PoliteFetcher
from kb.ingestion.parser import ParsedArticle, ParseError, parse_article, parse_web_page
from kb.ingestion.sitemap import ManifestRow, SitemapEntry, diff_sitemap, parse_sitemap
from kb.ingestion.sources import (
    HostGuard,
    Resolver,
    SitemapError,
    SitemapTree,
    guarded_client,
    normalize_url,
    system_resolver,
    walk_sitemap,
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
    # Pages left for the next run because someone pressed Stop (0 when the run finished).
    skipped: int = 0
    stopped: bool = False

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


async def _known_urls(db: aiosqlite.Connection) -> dict[str, str]:
    """Normalised URL -> page id, for every stored page and every earlier URL of a page
    that moved. A sitemap entry matching one of these is that page, not a new one."""
    known = {
        normalize_url(r["url"]): r["article_id"]
        for r in await db.execute_fetchall("SELECT article_id, url FROM pages")
    }
    for r in await db.execute_fetchall("SELECT url, article_id FROM url_aliases"):
        known.setdefault(r["url"], r["article_id"])
    return known


def _summary(tree: SitemapTree) -> dict[str, object]:
    """What the Sources list shows for a sitemap: totals, and each nested sitemap with its
    page count and first and last URL. Refreshed at every check."""
    return {
        "total": len(tree.locs),
        "first": tree.locs[0][0] if tree.locs else None,
        "last": tree.locs[-1][0] if tree.locs else None,
        "listed_twice": tree.listed_twice,
        "truncated": tree.truncated,
        "children": [vars(c) for c in tree.children],
    }


async def _custom_entries(
    db: aiosqlite.Connection,
    client: httpx.AsyncClient,
    settings: Settings,
    active: set[str],
) -> tuple[list[SitemapEntry], frozenset[str]]:
    """Pages from users' own sitemaps that are due this run (`active`), every nested sitemap
    included. A sitemap that fails to load keeps its existing pages (returned as `keep`): a
    temporary outage shouldn't wipe a site. So do sitemaps that aren't due yet."""
    entries: list[SitemapEntry] = []
    keep: set[str] = set()
    known = await _known_urls(db)
    help_center_ids = {
        r["article_id"]
        for r in await db.execute_fetchall(
            "SELECT article_id FROM pages WHERE source='help-center'"
        )
    }
    claimed: set[str] = set()
    for row in await db.execute_fetchall("SELECT id, url FROM custom_sitemaps ORDER BY id"):
        if row["url"] not in active:
            kept = await db.execute_fetchall(
                "SELECT article_id FROM pages WHERE source=? AND status NOT IN ('removed', 'quarantined')",
                (row["url"],),
            )
            keep.update(r["article_id"] for r in kept)
            continue
        try:
            tree = await walk_sitemap(
                client,
                row["url"],
                max_pages=settings.custom_sitemap_max_pages,
                max_children=settings.custom_sitemap_max_children,
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
        await db.execute("DELETE FROM page_listings WHERE source=?", (row["url"],))
        for loc, lastmod in tree.locs:
            article_id = known.get(normalize_url(loc)) or web_article_id(normalize_url(loc))
            await db.execute(
                "INSERT OR IGNORE INTO page_listings(article_id, source) VALUES (?, ?)",
                (article_id, row["url"]),
            )
            # Listed by an earlier sitemap this run, or already a help-center article:
            # stored once (the duplicate listing is counted, not fetched again).
            if article_id in claimed or article_id in help_center_ids:
                continue
            claimed.add(article_id)
            entries.append(SitemapEntry(article_id, loc, lastmod, source=row["url"], kind="web"))
        await db.execute(
            """UPDATE custom_sitemaps SET url_count=?, last_checked_at=datetime('now'),
               last_error=?, summary=? WHERE id=?""",
            (len(tree.locs), tree.truncated, json.dumps(_summary(tree)), row["id"]),
        )
    return entries, frozenset(keep)


async def _apply_diff(
    db: aiosqlite.Connection,
    entries: list[SitemapEntry],
    keep: frozenset[str] = frozenset(),
    checked: list[str] | None = None,
) -> tuple[IngestionReport, set[str]]:
    """Returns the report and the ids missing from their sitemap, for URL health checks."""
    rows = await db.execute_fetchall("SELECT article_id, lastmod, status FROM pages")
    manifest = [ManifestRow(r["article_id"], r["lastmod"], r["status"]) for r in rows]
    diff = diff_sitemap(entries, manifest, keep)

    cur = await db.execute(
        "INSERT INTO ingestion_runs(sources) VALUES (?)",
        (json.dumps(checked) if checked else None,),
    )
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
    await catalog.record(db, article_id, "archived", reason)


async def _repoint(
    db: aiosqlite.Connection, article_id: str, old_url: str, new_url: str
) -> str | None:
    """A page moved (301/308): it now lives at `new_url`. The old URL is kept as an alias,
    so a sitemap still listing it maps to this page. If another page is already stored at
    the new URL, this one is its duplicate. Returns that page's id in that case."""
    new_key = normalize_url(new_url)
    other = list(
        await db.execute_fetchall(
            "SELECT article_id, url FROM pages WHERE article_id != ? AND status != 'duplicate'",
            (article_id,),
        )
    )
    twin: str | None = next(
        (str(r["article_id"]) for r in other if normalize_url(r["url"]) == new_key), None
    )
    await db.execute(
        "INSERT OR REPLACE INTO url_aliases(url, article_id) VALUES (?, ?)",
        (normalize_url(old_url), twin or article_id),
    )
    if twin:
        await db.execute(
            """UPDATE pages SET status='duplicate', duplicate_of=?, updated_at=datetime('now')
               WHERE article_id=?""",
            (twin, article_id),
        )
        await catalog.record(
            db, article_id, "duplicate", f"moved to {new_url}, which is already stored", new_url
        )
        return twin
    await db.execute(
        "UPDATE pages SET url=?, updated_at=datetime('now') WHERE article_id=?",
        (new_url, article_id),
    )
    await db.execute("UPDATE articles SET url=? WHERE article_id=?", (new_url, article_id))
    await catalog.record(db, article_id, "moved", f"{old_url} → {new_url}", new_url)
    return None


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
    if not rows:
        await catalog.record(db, article.article_id, "added", article.title, article.url)
    elif changed:
        old_len = list(
            await db.execute_fetchall(
                "SELECT length(body) AS n FROM articles WHERE article_id=?", (article.article_id,)
            )
        )[0]["n"]
        delta = len(article.body) - (old_len or 0)
        await catalog.record(
            db,
            article.article_id,
            "changed",
            f"content changed ({'+' if delta >= 0 else '−'}{abs(delta):,} characters)",
            article.url,
        )
    if changed:
        await db.execute(
            """INSERT INTO articles(article_id, url, title, category, folder, body, content_hash, lastmod)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(article_id) DO UPDATE SET url=excluded.url, title=excluded.title,
               category=excluded.category, folder=excluded.folder, body=excluded.body,
               content_hash=excluded.content_hash, lastmod=excluded.lastmod,
               indexed_hash=NULL, ingested_at=datetime('now')""",
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
           duplicate_of=NULL, fetched_at=datetime('now'), updated_at=datetime('now')
           WHERE article_id=?""",
        (new_hash, article.article_id),
    )
    # The same text at another URL (a mirror, a print view): kept, but searched once. Help-
    # center articles have their own ids from the help center, so only custom pages are checked.
    twin = (
        []
        if not article.article_id.startswith("web-")
        else list(
            await db.execute_fetchall(
                """SELECT article_id, url FROM pages WHERE content_hash=? AND article_id != ?
               AND status = 'done' ORDER BY fetched_at LIMIT 1""",
                (new_hash, article.article_id),
            )
        )
    )
    if twin:
        await db.execute(
            "UPDATE pages SET status='duplicate', duplicate_of=? WHERE article_id=?",
            (twin[0]["article_id"], article.article_id),
        )
        await catalog.record(
            db, article.article_id, "duplicate", f"same content as {twin[0]['url']}", article.url
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
        f"SELECT article_id, url, kind, source, status FROM pages WHERE article_id IN ({marks}) AND status != 'quarantined'",
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
        if status in (301, 308) and location:
            # Moved for good: fetch it at its new address and re-index it there.
            await _process_one(
                db,
                db_lock,
                fetchers[r["kind"]],
                aid,
                urljoin(url, location),
                None,
                report,
                r["kind"],
                r["source"],
                hs,
                None,
                moved_from=url,
            )
            return
        if status in (404, 410) and missing:
            issue, outcome = "gone", "missing"
        elif status in (404, 410):
            # Still listed in its sitemap: counted as broken, not archived on sight.
            issue, outcome = "broken", "broken"
        elif status in (301, 308):
            issue, outcome = "moved", "moved"
        elif status == 200 and r["status"] == "failed":
            # It works again after failing: fetch and store it now (this also resets its count).
            await _process_one(
                db,
                db_lock,
                fetchers[r["kind"]],
                aid,
                url,
                None,
                report,
                r["kind"],
                r["source"],
                hs,
                None,
            )
            return
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
            if state == "quarantined":
                outcome = "quarantined"
            row = list(
                await db.execute_fetchall("SELECT count FROM url_health WHERE article_id=?", (aid,))
            )
            detail: str | None = None
            if issue is not None and row:
                detail = (
                    "HTTP 404 and missing from its sitemap: archived automatically"
                    if state == "quarantined" and issue == "gone"
                    else f"HTTP {status}, {row[0]['count']} of {hs.threshold} checks in a row ({state})"
                )
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
    moved_from: str | None = None,
) -> None:
    if fetched_ids is not None:  # checked this run, whatever the outcome: no second count
        fetched_ids.add(article_id)
    try:
        fetched = await fetcher.fetch(url)
        html = fetched.text
        parse = parse_web_page if kind == "web" else parse_article
        article = parse(html, article_id, fetched.moved_to or url)
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
            if exc.status_code in (404, 410):
                # The page is still listed in its sitemap (that's why it was fetched), so
                # this counts as broken; only a 404 that's also missing is archived at once.
                await _mark_failed(db, article_id, str(exc))
                state = await health.observe(
                    db,
                    hs or health.HealthSettings(),
                    article_id,
                    url,
                    "broken",
                    status=exc.status_code,
                )
                outcome, failure_detail = (
                    "broken",
                    f"HTTP {exc.status_code} but still listed in its sitemap ({state})",
                )
            else:
                await _mark_failed(db, article_id, str(exc))
                outcome, failure_detail = "failed", str(exc)
            await _log_fetch(db, report, article_id, url, source, outcome, failure_detail)
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
        detail = None
        old_url = moved_from or url
        new_url = fetched.moved_to or (url if moved_from else None)
        if new_url and normalize_url(new_url) != normalize_url(old_url):
            # Moved for good (301/308): the page now lives at the new URL, re-indexed there.
            twin = await _repoint(db, article_id, old_url, new_url)
            if twin:
                await _log_fetch(
                    db,
                    report,
                    article_id,
                    new_url,
                    source,
                    "duplicate",
                    f"moved to a page that's already stored ({twin})",
                )
                await db.commit()
                return
            detail = f"moved from {old_url}: re-pointed and re-indexed at the new URL"
        changed = await _store_article(db, article, lastmod)
        report.fetched += 1
        report.content_changed += int(changed)
        if hs is not None:  # a clean fetch resets any count
            await health.observe(db, hs, article_id, new_url or url, None, status=200)
        dup = list(
            await db.execute_fetchall("SELECT status FROM pages WHERE article_id=?", (article_id,))
        )
        outcome = (
            "duplicate"
            if dup and dup[0]["status"] == "duplicate"
            else "moved"
            if detail
            else "updated"
            if changed
            else "unchanged"
        )
        await _log_fetch(db, report, article_id, new_url or url, source, outcome, detail)
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
    sources: set[str] | None = None,
    scheduled: bool = False,
    should_stop: Callable[[], bool] | None = None,
) -> IngestionReport:
    """`sources`: check only these (a sitemap just confirmed). `scheduled`: check only the
    sources that are due (the daily job). Neither: check everything ("Run refresh now").

    `should_stop`: asked before each page is started. Once it says yes, no new page is
    fetched, but every page already in progress is finished and stored with its passages,
    so nothing is left half-written. The pages not reached stay pending for the next run."""
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
        hc_days, hc_last = await catalog.helpcenter_schedule(db, settings)
        configured = {catalog.HELP_CENTER} | {
            r["url"] for r in await db.execute_fetchall("SELECT url FROM custom_sitemaps")
        }
        if sources is not None:
            active = set(sources) & configured
        elif scheduled:
            due = [
                r["url"]
                for r in await db.execute_fetchall(
                    "SELECT url, recheck_days, last_checked_at FROM custom_sitemaps"
                )
                if catalog.is_due(r["last_checked_at"], r["recheck_days"])
            ]
            active = set(due) | (
                {catalog.HELP_CENTER} if catalog.is_due(hc_last, hc_days) else set()
            )
        else:
            active = set(configured)
        entries: list[SitemapEntry] = []
        hc_keep: set[str] = set()
        if catalog.HELP_CENTER in active:
            entries = parse_sitemap(await fetcher.get_text(settings.sitemap_url))
            await catalog.mark_helpcenter_checked(db)
        else:  # not due: its pages stay as they are
            hc_keep = {
                r["article_id"]
                for r in await db.execute_fetchall(
                    "SELECT article_id FROM pages WHERE source='help-center'"
                )
            }
        custom, keep = await _custom_entries(db, web_client, settings, active)
        report, absent = await _apply_diff(db, entries + custom, keep | hc_keep, sorted(active))
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
        todo = list(await db.execute_fetchall(sql, params))
        await db.execute(
            "UPDATE ingestion_runs SET planned=? WHERE id=?", (len(todo), report.run_id)
        )
        await db.commit()

        db_lock = asyncio.Lock()
        queue = deque(todo)
        stop = should_stop or (lambda: False)

        async def worker() -> None:
            # Each page is taken only when a worker is free, so a stop takes effect at the
            # next page boundary; the fetchers still keep each site's pace polite.
            while queue and not stop():
                r = queue.popleft()
                await _process_one(
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

        try:
            # Two fetchers (help center, custom sites), each with its own concurrency cap.
            workers = min(len(todo), 2 * settings.max_concurrency)
            await asyncio.gather(*(worker() for _ in range(workers)))
            report.skipped = len(queue)
            report.stopped = stop()
            if report.stopped:
                log.info("stopped: %d pages left pending for the next run", report.skipped)
            if limit is None and not report.stopped:  # partial runs skip health checks
                await _health_checks(
                    db,
                    db_lock,
                    {"helpcenter": fetcher, "web": web_fetcher},
                    hs,
                    report,
                    absent,
                    fetched_ids,
                )
            status, error = ("stopped" if report.stopped else "finished"), None
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
