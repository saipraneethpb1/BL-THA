"""404s quarantine immediately; other problems retain review rules. Content is kept."""

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


async def test_404_quarantines_immediately_despite_preferences(settings: Settings) -> None:
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
    assert reason == "HTTP 404 (automatically quarantined)"

    async with connect(settings.db_path) as db:  # restore: fetched again at the next refresh
        assert await health.restore(db, ["2"]) == 1
        await db.commit()
    assert status(settings, "2") == "pending" and tracked(settings) == {}


async def test_a_recovered_url_resets_its_count(settings: Settings) -> None:
    await set_rules(settings, threshold=3)
    site = Site()
    site.gone_status = 410  # non-404 problems still use consecutive-refresh review
    t = httpx.MockTransport(site.handler)
    site.sitemap = sitemap(("1", "d1"), ("2", "d1"))
    await run_ingestion(settings, transport=t)
    site.sitemap, site.gone = sitemap(("1", "d1")), {"2"}
    await run_ingestion(settings, transport=t)
    await run_ingestion(settings, transport=t)
    assert tracked(settings) == {"2": ("gone", 2, "counting")}
    site.gone = set()  # it answers again (still unlisted): no longer a 404, so the count resets
    await run_ingestion(settings, transport=t)
    assert tracked(settings) == {}


async def test_permanent_redirects_are_counted_and_the_flag_rule_never_quarantines(
    settings: Settings,
) -> None:
    await set_rules(settings, threshold=2, action_moved="flag")
    site = Site()
    t = httpx.MockTransport(site.handler)
    site.sitemap = sitemap(("1", "d1"), ("9", "d1"))
    site.moved = {"1": "9"}
    await run_ingestion(settings, transport=t)  # 1 is fetched through its redirect: count 1
    assert tracked(settings) == {"1": ("moved", 1, "counting")}
    assert status(settings, "1") == "done"
    await run_ingestion(settings, transport=t)  # unchanged, but re-checked because it's counted
    assert tracked(settings) == {"1": ("moved", 2, "flagged")}
    with sqlite3.connect(settings.db_path) as db:
        to = db.execute("SELECT redirect_to FROM url_health").fetchone()[0]
    assert to == f"{BASE}/9"

    # The rule changes to quarantine: it becomes pending, still waiting for a person.
    async with connect(settings.db_path) as db:
        await health.save_settings(
            db, health.HealthSettings(threshold=2, action_moved="quarantine")
        )
        await db.commit()
    assert tracked(settings)["1"][2] == "pending"
    # A higher threshold moves it back to counting.
    async with connect(settings.db_path) as db:
        await health.save_settings(db, health.HealthSettings(threshold=5))
        await db.commit()
    assert tracked(settings)["1"][2] == "counting"


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
