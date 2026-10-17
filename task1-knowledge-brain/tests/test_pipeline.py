"""End-to-end ingestion against a fake help center (no network)."""

import sqlite3
from pathlib import Path

import httpx
import pytest

from kb.config import Settings
from kb.ingestion.pipeline import run_ingestion

ARTICLE_HTML = (Path(__file__).parent / "fixtures" / "article_5000558401.html").read_text(
    encoding="utf-8"
)
BASE = "https://help.test/support/solutions/articles"


def sitemap(*items: tuple[str, str]) -> str:
    urls = "".join(
        f"<url><loc>{BASE}/{aid}</loc><lastmod>{lm}</lastmod></url>" for aid, lm in items
    )
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'


class FakeSite:
    def __init__(self) -> None:
        self.sitemap = ""
        self.broken: set[str] = set()
        self.gone: set[str] = set()
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path.endswith("sitemap.xml"):
            return httpx.Response(200, text=self.sitemap)
        aid = path.rsplit("/", 1)[-1]
        if aid in self.gone:
            return httpx.Response(404)
        if aid in self.broken:
            return httpx.Response(503)
        return httpx.Response(200, text=ARTICLE_HTML.replace("5000558401", aid))


@pytest.fixture
def site() -> FakeSite:
    return FakeSite()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "kb.db",
        sitemap_url="https://help.test/sitemap.xml",
        min_interval_s=0,
        max_attempts=2,
    )


def statuses(settings: Settings) -> dict[str, str]:
    with sqlite3.connect(settings.db_path) as c:
        return dict(c.execute("SELECT article_id, status FROM pages").fetchall())


async def test_first_run_then_incremental_update(site: FakeSite, settings: Settings) -> None:
    site.sitemap = sitemap(("1", "d1"), ("2", "d1"), ("3", "d1"))
    t = httpx.MockTransport(site.handler)
    report = await run_ingestion(settings, transport=t)
    assert (report.new, report.fetched, report.failed) == (3, 3, 0)

    # Next day: article 2 updated, 3 removed, 4 added. Only 2 and 4 are fetched.
    site.sitemap = sitemap(("1", "d1"), ("2", "d2"), ("4", "d2"))
    site.calls.clear()
    report = await run_ingestion(settings, transport=t)
    assert (report.new, report.changed, report.removed) == (1, 1, 1)
    fetched = sorted(p.rsplit("/", 1)[-1] for p in site.calls if "articles" in p)
    assert fetched == ["2", "4"]
    assert statuses(settings) == {"1": "done", "2": "done", "3": "removed", "4": "done"}
    with sqlite3.connect(settings.db_path) as c:
        assert c.execute("SELECT count(*) FROM articles WHERE article_id='3'").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM images WHERE article_id='3'").fetchone()[0] == 0


async def test_failures_are_isolated_and_resumed(site: FakeSite, settings: Settings) -> None:
    site.sitemap = sitemap(("1", "d1"), ("2", "d1"))
    site.broken = {"2"}
    t = httpx.MockTransport(site.handler)
    report = await run_ingestion(settings, transport=t)
    assert (report.fetched, report.failed) == (1, 1)
    assert statuses(settings) == {"1": "done", "2": "failed"}

    site.broken.clear()
    report = await run_ingestion(settings, transport=t)  # resumes only the failed page
    assert report.fetched == 1
    assert statuses(settings) == {"1": "done", "2": "done"}


async def test_404_marks_article_removed(site: FakeSite, settings: Settings) -> None:
    site.sitemap = sitemap(("1", "d1"))
    site.gone = {"1"}
    await run_ingestion(settings, transport=httpx.MockTransport(site.handler))
    assert statuses(settings) == {"1": "removed"}


async def test_limit_allows_a_partial_run(site: FakeSite, settings: Settings) -> None:
    site.sitemap = sitemap(*((str(i), "d1") for i in range(5)))
    t = httpx.MockTransport(site.handler)
    report = await run_ingestion(settings, limit=2, transport=t)
    assert report.fetched == 2
    report = await run_ingestion(settings, transport=t)
    assert report.fetched == 3
