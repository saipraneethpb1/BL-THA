"""Bookkeeping around the crawl: when each source is due, how long an ingest will take,
and every page's change history.

Schedule: each source (the help center and every custom sitemap) has its own re-check
interval: daily, weekly, every 15 or every 30 days. The daily job checks only the sources
that are due; "Run refresh now" checks them all.
"""

from datetime import UTC, datetime, timedelta

import aiosqlite

from kb.config import Settings

RECHECK_CHOICES = {1: "Daily", 7: "Weekly", 15: "Every 15 days", 30: "Every 30 days"}
HELP_CENTER = "help-center"
# The daily job runs at the same hour each day; a little slack keeps "weekly" from slipping
# to day 8 because last week's run finished a few minutes later.
_SLACK = timedelta(hours=3)


def parse_time(t: str | None) -> datetime | None:
    if not t:
        return None
    t = t.replace("Z", "").replace("T", " ")
    try:
        return datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def is_due(last_checked: str | None, days: int, now: datetime | None = None) -> bool:
    last = parse_time(last_checked)
    return last is None or (now or datetime.now(UTC)) - last >= timedelta(days=days) - _SLACK


def next_check(last_checked: str | None, days: int) -> str | None:
    last = parse_time(last_checked)
    return (last + timedelta(days=days)).isoformat(timespec="minutes") if last else None


async def _setting(db: aiosqlite.Connection, key: str) -> str | None:
    rows = list(await db.execute_fetchall("SELECT value FROM kb_settings WHERE key=?", (key,)))
    return str(rows[0][0]) if rows else None


async def _set(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO kb_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


async def helpcenter_schedule(
    db: aiosqlite.Connection, settings: Settings
) -> tuple[int, str | None]:
    """(re-check interval in days, when it was last checked) for the help center."""
    days = await _setting(db, "helpcenter_recheck_days")
    return int(days) if days else settings.helpcenter_recheck_days, await _setting(
        db, "helpcenter_last_checked_at"
    )


async def set_helpcenter_days(db: aiosqlite.Connection, days: int) -> None:
    await _set(db, "helpcenter_recheck_days", str(days))


async def mark_helpcenter_checked(db: aiosqlite.Connection) -> None:
    await _set(db, "helpcenter_last_checked_at", datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"))


def estimate_seconds(pages: int, settings: Settings) -> int:
    """Rough time to fetch and index `pages` new pages: requests are spaced
    `min_interval_s` apart (politeness), then each page is embedded on this server."""
    per_page = settings.min_interval_s + 0.3 + settings.embed_s_per_page
    return round(pages * per_page)


async def record(
    db: aiosqlite.Connection,
    article_id: str,
    event: str,
    detail: str | None = None,
    url: str | None = None,
) -> None:
    """One line in a page's change history."""
    await db.execute(
        "INSERT INTO page_history(article_id, event, detail, url) VALUES (?, ?, ?, ?)",
        (article_id, event, detail, url),
    )
