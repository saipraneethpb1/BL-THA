"""A 404 that's also missing from its sitemap is archived at once; a 404 that's still
listed is counted; a 301 re-points the page to its new URL. Content is always kept."""

import sqlite3
from pathlib import Path

import aiosqlite
import httpx
import pytest

from kb.config import Settings
from kb.db import connect, init_db
from kb.ingestion import health
from kb.ingestion.pipeline import run_ingestion
from tests.test_pipeline import ARTICLE_HTML, BASE, sitemap


class Site:
    """A fake help center whose articles can disappear (404) or move (301)."""

    def __init__(self) -> None:
        self.sitemap = ""
        self.gone: set[str] = set()
        self.gone_status = 404
        self.moved: dict[str, str] = {}  # article id -> article id it redirects to

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("sitemap.xml"):
            return httpx.Response(200, text=self.sitemap)
        aid = path.rsplit("/", 1)[-1]
        if aid in self.gone:
            return httpx.Response(self.gone_status)
        if aid in self.moved:
            return httpx.Response(301, headers={"location": f"{BASE}/{self.moved[aid]}"})
        return httpx.Response(200, text=ARTICLE_HTML.replace("5000558401", aid))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "kb.db",
        sitemap_url="https://help.test/sitemap.xml",
        min_interval_s=0,
        max_attempts=1,
    )


async def set_rules(settings: Settings, **kw: object) -> None:
    await init_db(settings.db_path)
    async with connect(settings.db_path) as db:
        await health.save_settings(db, health.HealthSettings(**kw))  # type: ignore[arg-type]
        await db.commit()


def tracked(settings: Settings) -> dict[str, tuple[str, int, str]]:
    with sqlite3.connect(settings.db_path) as db:
        return {
            a: (i, c, s)
            for a, i, c, s in db.execute("SELECT article_id, issue, count, state FROM url_health")
        }


def status(settings: Settings, aid: str) -> str:
    with sqlite3.connect(settings.db_path) as db:
        return db.execute("SELECT status FROM pages WHERE article_id=?", (aid,)).fetchone()[0]


async def test_404_and_missing_from_its_sitemap_is_archived_at_once(settings: Settings) -> None:
    await set_rules(settings, threshold=30, action_gone="flag")
    site = Site()
    t = httpx.MockTransport(site.handler)
    site.sitemap = sitemap(("1", "d1"), ("2", "d1"))
    await run_ingestion(settings, transport=t)

    site.sitemap, site.gone = sitemap(("1", "d1")), {"2"}  # 2 disappears and answers 404
    await run_ingestion(settings, transport=t)
    assert tracked(settings) == {"2": ("gone", 1, "quarantined")}
    assert status(settings, "2") == "quarantined"
    assert tracked(settings)["2"][2] == "quarantined"
    with sqlite3.connect(settings.db_path) as db:
        assert db.execute("SELECT count(*) FROM articles WHERE article_id='2'").fetchone()[0] == 1
        reason = db.execute("SELECT last_error FROM pages WHERE article_id='2'").fetchone()[0]
    assert reason == "HTTP 404 and missing from its sitemap (archived automatically)"

    async with connect(settings.db_path) as db:  # restore: fetched again at the next refresh
        assert await health.restore(db, ["2"]) == 1
        await db.commit()
    assert status(settings, "2") == "pending" and tracked(settings) == {}
    assert history(settings, "2") == ["added", "archived", "restored"]


async def test_a_broken_page_is_counted_once_per_check_and_resets_when_it_recovers(
    settings: Settings,
) -> None:
    await set_rules(settings, threshold=2)
    site = Site()
    t = httpx.MockTransport(site.handler)
    site.sitemap = sitemap(("1", "d1"), ("2", "d1"))
    await run_ingestion(settings, transport=t)
    site.gone = {"2"}  # still listed, but 404
    site.sitemap = sitemap(("1", "d1"), ("2", "d2"))
    await run_ingestion(settings, transport=t)
    assert tracked(settings) == {"2": ("broken", 1, "counting")}
    await run_ingestion(settings, transport=t)
    assert tracked(settings) == {"2": ("broken", 2, "pending")}  # waits for a person
    assert status(settings, "2") == "failed"  # not archived on its own
    site.gone = set()  # it answers again: the count resets
    await run_ingestion(settings, transport=t)
    assert tracked(settings) == {} and status(settings, "2") == "done"


async def test_a_permanent_redirect_repoints_the_page_and_reindexes_it(
    settings: Settings,
) -> None:
    site = Site()
    t = httpx.MockTransport(site.handler)
    site.sitemap = sitemap(("1", "d1"))
    site.moved = {"1": "1-new"}  # fetched through its 301
    await run_ingestion(settings, transport=t)
    with sqlite3.connect(settings.db_path) as db:
        page_url, article_url = db.execute(
            "SELECT p.url, a.url FROM pages p JOIN articles a USING (article_id) WHERE article_id='1'"
        ).fetchone()
        alias = db.execute("SELECT article_id FROM url_aliases").fetchall()
        outcome = db.execute("SELECT outcome FROM fetch_log").fetchone()[0]
    assert page_url == article_url == f"{BASE}/1-new"
    assert alias == [("1",)] and outcome == "moved"
    assert tracked(settings) == {} and status(settings, "1") == "done"
    assert history(settings, "1") == ["moved", "added"]


async def test_a_redirect_onto_a_stored_page_makes_a_duplicate(settings: Settings) -> None:
    site = Site()
    t = httpx.MockTransport(site.handler)
    site.sitemap = sitemap(("9", "d1"))
    await run_ingestion(settings, transport=t)
    site.sitemap = sitemap(("1", "d1"), ("9", "d1"))
    site.moved = {"1": "9"}  # 1 now redirects to a page that's already stored
    await run_ingestion(settings, transport=t)
    assert status(settings, "1") == "duplicate" and status(settings, "9") == "done"


async def test_keep_resets_and_settings_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "kb.db"
    await init_db(path)
    async with aiosqlite.connect(path) as raw:
        raw.row_factory = aiosqlite.Row
        assert await health.get_settings(raw) == health.HealthSettings()
        s = health.HealthSettings(threshold=4, action_gone="flag", action_moved="quarantine")
        await health.save_settings(raw, s)
        assert await health.get_settings(raw) == s
        await health.observe(raw, s, "7", "https://help.test/7", "moved", status=301)
        assert await health.keep(raw, ["7"]) == 1
        assert await health.open_ids(raw) == set()


def history(settings: Settings, aid: str) -> list[str]:
    with sqlite3.connect(settings.db_path) as db:
        return [
            e
            for (e,) in db.execute(
                "SELECT event FROM page_history WHERE article_id=? ORDER BY id", (aid,)
            )
        ]


async def test_a_page_back_from_the_archive_is_counted_again(settings: Settings) -> None:
    """An old 'archived' health record must not hide a page that's back and failing."""
    await set_rules(settings, threshold=2)
    site = Site()
    t = httpx.MockTransport(site.handler)
    site.sitemap = sitemap(("1", "d1"), ("2", "d1"))
    await run_ingestion(settings, transport=t)
    site.sitemap, site.gone = sitemap(("1", "d1")), {"2"}
    await run_ingestion(settings, transport=t)  # 404 + missing: archived
    assert status(settings, "2") == "quarantined"
    site.sitemap = sitemap(("1", "d1"), ("2", "d2"))  # listed again, still 404
    await run_ingestion(settings, transport=t)
    assert status(settings, "2") == "failed"
    assert tracked(settings) == {"2": ("broken", 1, "counting")}
    await run_ingestion(settings, transport=t)
    assert tracked(settings) == {"2": ("broken", 2, "pending")}
