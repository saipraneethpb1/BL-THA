"""URL health: which pages have stopped working, and what happens to them.

Problems tracked:
- "gone": the URL answers 404/410 AND is no longer listed in its sitemap. Both signals
  agree the page was taken down, so it moves to the Archived KB at once;
- "broken": the URL answers 404/410 but its sitemap still lists it. That can be a passing
  fault, so it's counted, like the next one;
- "moved": a permanent redirect (301/308) whose new address isn't given. (When it is, the
  page is simply re-pointed to the new URL and re-indexed there: kb/ingestion/pipeline.py.)

Each check that sees the same problem adds one to the URL's count; a check that sees it
working again resets it. At the threshold (presets: 2, 15 or 30 checks in a row) the URL
becomes "pending" (rule: archive after confirming) or "flagged" (rule: flag only). Nothing
is ever deleted: the Archived KB keeps the page and its passages, searchable on their own,
but out of normal answers. ("quarantined" is the archived state's name in the database.)
"""

from dataclasses import dataclass

import aiosqlite

from kb.ingestion import catalog

ISSUES = {
    "gone": "Not found (404) and missing from its sitemap",
    "broken": "Not found (404) but still listed in its sitemap",
    "moved": "301 permanent redirect, no new address given",
}
THRESHOLD_PRESETS = (2, 15, 30)
ACTIONS = ("quarantine", "flag")
OPEN_STATES = ("counting", "pending", "flagged")


@dataclass(frozen=True)
class HealthSettings:
    threshold: int = 3
    action_gone: str = "quarantine"
    action_moved: str = "quarantine"

    def action_for(self, issue: str) -> str:
        return self.action_moved if issue == "moved" else self.action_gone


async def get_settings(db: aiosqlite.Connection) -> HealthSettings:
    rows = await db.execute_fetchall(
        "SELECT key, value FROM kb_settings WHERE key IN ('health_threshold', 'health_action_gone', 'health_action_moved')"
    )
    values = {r["key"]: r["value"] for r in rows}
    d = HealthSettings()
    return HealthSettings(
        threshold=int(values.get("health_threshold", d.threshold)),
        action_gone=values.get("health_action_gone", d.action_gone),
        action_moved=values.get("health_action_moved", d.action_moved),
    )


def _state(count: int, issue: str, s: HealthSettings) -> str:
    if count < s.threshold:
        return "counting"
    return "pending" if s.action_for(issue) == "quarantine" else "flagged"


async def save_settings(db: aiosqlite.Connection, s: HealthSettings) -> None:
    """Store new settings and re-sort every open URL under them (a lower threshold can make
    URLs pending at once; a higher one moves them back to counting). Nothing is quarantined."""
    for key, value in (
        ("health_threshold", str(s.threshold)),
        ("health_action_gone", s.action_gone),
        ("health_action_moved", s.action_moved),
    ):
        await db.execute(
            "INSERT INTO kb_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    rows = await db.execute_fetchall(
        f"SELECT article_id, issue, count FROM url_health WHERE state IN {OPEN_STATES}"
    )
    for r in rows:
        await db.execute(
            "UPDATE url_health SET state=? WHERE article_id=?",
            (_state(r["count"], r["issue"], s), r["article_id"]),
        )


async def observe(
    db: aiosqlite.Connection,
    s: HealthSettings,
    article_id: str,
    url: str,
    issue: str | None,
    *,
    status: int | None = None,
    redirect_to: str | None = None,
) -> str | None:
    """Record what this refresh saw for a URL. `issue=None` means it's healthy: the count
    resets. Returns the URL's state afterwards (None if it isn't being tracked)."""
    rows = list(
        await db.execute_fetchall(
            "SELECT issue, count, state FROM url_health WHERE article_id=?", (article_id,)
        )
    )
    row = rows[0] if rows else None
    if issue is None:
        if row is not None and row["state"] in OPEN_STATES:
            await db.execute("DELETE FROM url_health WHERE article_id=?", (article_id,))
        return None if row is None or row["state"] in OPEN_STATES else str(row["state"])
    if row is not None and row["state"] == "quarantined":
        await db.execute(
            "UPDATE url_health SET last_status=?, redirect_to=?, last_seen_at=datetime('now') WHERE article_id=?",
            (status, redirect_to, article_id),
        )
        return "quarantined"
    count = row["count"] + 1 if row is not None and row["issue"] == issue else 1
    auto_quarantine = issue == "gone"  # 404/410 and missing from its sitemap
    state = "quarantined" if auto_quarantine else _state(count, issue, s)
    await db.execute(
        """INSERT INTO url_health(article_id, url, issue, count, state, last_status, redirect_to,
                                  first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
           ON CONFLICT(article_id) DO UPDATE SET url=excluded.url, issue=excluded.issue,
             count=excluded.count, state=excluded.state, last_status=excluded.last_status,
             redirect_to=excluded.redirect_to, last_seen_at=excluded.last_seen_at,
             first_seen_at=CASE WHEN url_health.issue = excluded.issue
                                THEN url_health.first_seen_at ELSE excluded.first_seen_at END""",
        (article_id, url, issue, count, state, status, redirect_to),
    )
    if auto_quarantine:
        await db.execute(
            """UPDATE pages SET status='quarantined', last_error='HTTP 404 and missing from its sitemap (archived automatically)',
               quarantined_at=coalesce(quarantined_at, datetime('now')), updated_at=datetime('now')
               WHERE article_id=?""",
            (article_id,),
        )
        await db.execute(
            "UPDATE url_health SET decided_at=datetime('now') WHERE article_id=?",
            (article_id,),
        )
        await catalog.record(
            db, article_id, "archived", f"HTTP {status} and missing from its sitemap", url
        )
    elif issue == "broken" and count == 1:
        await catalog.record(
            db, article_id, "broken", f"HTTP {status} while still listed in its sitemap", url
        )
    return state


async def open_ids(db: aiosqlite.Connection) -> set[str]:
    rows = await db.execute_fetchall(
        f"SELECT article_id FROM url_health WHERE state IN {OPEN_STATES}"
    )
    return {r["article_id"] for r in rows}


async def quarantine(db: aiosqlite.Connection, article_ids: list[str]) -> int:
    """A person confirmed: take these URLs out of search (they're kept, not deleted)."""
    done = 0
    for aid in article_ids:
        rows = list(
            await db.execute_fetchall(
                "SELECT issue, count FROM url_health WHERE article_id=? AND state IN ('pending', 'flagged', 'counting')",
                (aid,),
            )
        )
        if not rows:
            continue
        reason = f"{ISSUES[rows[0]['issue']]} for {rows[0]['count']} refreshes in a row (confirmed)"
        await db.execute(
            """UPDATE pages SET status='quarantined', last_error=?,
               quarantined_at=coalesce(quarantined_at, datetime('now')), updated_at=datetime('now')
               WHERE article_id=?""",
            (reason, aid),
        )
        await db.execute(
            "UPDATE url_health SET state='quarantined', decided_at=datetime('now') WHERE article_id=?",
            (aid,),
        )
        await catalog.record(db, aid, "archived", reason)
        done += 1
    return done


async def keep(db: aiosqlite.Connection, article_ids: list[str]) -> int:
    """A person decided the URL is fine: reset its count (it starts again if the problem recurs)."""
    marks = ",".join("?" * len(article_ids))
    cur = await db.execute(
        f"DELETE FROM url_health WHERE article_id IN ({marks}) AND state IN {OPEN_STATES}",
        article_ids,
    )
    return cur.rowcount


async def restore(db: aiosqlite.Connection, article_ids: list[str]) -> int:
    """Bring quarantined URLs back: they're fetched again at the next refresh."""
    marks = ",".join("?" * len(article_ids))
    cur = await db.execute(
        f"""UPDATE pages SET status='pending', attempts=0, last_error=NULL, quarantined_at=NULL,
            updated_at=datetime('now') WHERE article_id IN ({marks}) AND status='quarantined'""",
        article_ids,
    )
    await db.execute(f"DELETE FROM url_health WHERE article_id IN ({marks})", article_ids)
    for aid in article_ids:
        await catalog.record(
            db, aid, "restored", "back from the Archived KB: fetched again at the next check"
        )
    return cur.rowcount
