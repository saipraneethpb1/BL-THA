"""URL health: count problems across consecutive refreshes, and let a person decide.

Two problems are tracked, each with its own rule (quarantine, or only flag):
- "gone":  the URL answers 404/410 and is no longer listed in its sitemap;
- "moved": the URL answers a permanent redirect (301/308).

Each refresh that sees the same problem adds one to the URL's count; a refresh that sees it
working again resets it. When the count reaches the threshold, the URL becomes "pending"
(rule: quarantine) or "flagged" (rule: flag). Nothing is quarantined until a person confirms
it, and nothing is ever deleted: quarantine keeps the page but takes it out of search.
"""

from dataclasses import dataclass

import aiosqlite

ISSUES = {
    "gone": "404 and no longer in its sitemap",
    "moved": "301 permanent redirect",
}
ACTIONS = ("quarantine", "flag")
OPEN_STATES = ("counting", "pending", "flagged")


@dataclass(frozen=True)
class HealthSettings:
    threshold: int = 3
    action_gone: str = "quarantine"
    action_moved: str = "quarantine"

    def action_for(self, issue: str) -> str:
        return self.action_gone if issue == "gone" else self.action_moved


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
    state = _state(count, issue, s)
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
    return cur.rowcount
