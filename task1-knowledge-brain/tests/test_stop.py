"""Stopping a crawl: it ends at a page boundary. Pages in progress are finished and stored
with their passages, no new page starts, the rest stay pending, and the next run resumes."""

import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from kb import api
from kb.config import Settings
from kb.ingestion.pipeline import run_ingestion
from kb.refresh import Refresher
from kb.retrieval.search import Searcher
from tests.fakes import FakeEmbedder
from tests.test_pipeline import FakeSite, sitemap, statuses

H = {"X-API-Key": "secret"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "kb.db",
        sitemap_url="https://help.test/sitemap.xml",
        min_interval_s=0,
        max_attempts=2,
        max_concurrency=1,
    )


def stop_after(site: FakeSite, pages: int, stop: list[bool]) -> httpx.MockTransport:
    """Serve the site, and flip `stop` once `pages` article pages have been requested."""

    def handler(request: httpx.Request) -> httpx.Response:
        resp = site.handler(request)
        if sum("articles" in c for c in site.calls) >= pages:
            stop[0] = True
        return resp

    return httpx.MockTransport(handler)


async def test_stop_finishes_pages_in_progress_and_leaves_the_rest_pending(
    settings: Settings,
) -> None:
    site = FakeSite()
    site.sitemap = sitemap(*((str(i), "d1") for i in range(8)))
    stop = [False]
    report = await run_ingestion(
        settings, transport=stop_after(site, 2, stop), should_stop=lambda: stop[0]
    )

    assert report.stopped
    st = statuses(settings)
    done = {a for a, s in st.items() if s == "done"}
    # Every page that was requested was finished and stored; nothing is half-written.
    requested = {c.rsplit("/", 1)[-1] for c in site.calls if "articles" in c}
    assert done == requested and 2 <= len(done) < 8
    assert report.fetched == len(done) and report.skipped == 8 - len(done)
    assert {s for a, s in st.items() if a not in done} == {"pending"}
    with sqlite3.connect(settings.db_path) as c:
        stored = {r[0] for r in c.execute("SELECT article_id FROM articles WHERE body != ''")}
        run = c.execute("SELECT status, failed FROM ingestion_runs").fetchone()
    assert stored == done
    assert run == ("stopped", 0)

    # The next run picks up exactly the pages that weren't reached.
    site.calls.clear()
    report = await run_ingestion(settings, transport=httpx.MockTransport(site.handler))
    assert not report.stopped and report.fetched == 8 - len(done)
    assert set(statuses(settings).values()) == {"done"}


async def test_a_stopped_refresh_still_indexes_the_pages_it_finished(settings: Settings) -> None:
    site = FakeSite()
    site.sitemap = sitemap(*((str(i), "d1") for i in range(6)))
    refresher = Refresher(settings, FakeEmbedder(), None)

    def handler(request: httpx.Request) -> httpx.Response:
        resp = site.handler(request)
        if "articles" in request.url.path:
            refresher.stop()  # pressed while the first page is being fetched
        return resp

    refresher.transport = httpx.MockTransport(handler)
    refresher.queued = {"https://shop.test/sitemap.xml"}  # confirmed meanwhile: dropped
    result = await refresher.run()

    assert result.status == "stopped" and result.ingestion["stopped"]
    with sqlite3.connect(settings.db_path) as c:
        done = {r[0] for r in c.execute("SELECT article_id FROM pages WHERE status='done'")}
        indexed = {
            r[0] for r in c.execute("SELECT DISTINCT article_id FROM chunks WHERE source='article'")
        }
    assert done and len(done) < 6
    assert indexed == done  # finished pages are searchable straight away
    assert not refresher.running and not refresher.queued


def test_stop_needs_a_running_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api.settings, "db_path", tmp_path / "kb.db")
    monkeypatch.setattr(api.settings, "api_key", "secret")
    monkeypatch.setattr(api.settings, "refresh_enabled", False)
    monkeypatch.setattr(api.settings, "bootstrap_crawl", False)
    monkeypatch.setattr(api.settings, "speech_preload", False)
    api.app.state.searcher = Searcher(FakeEmbedder())
    api.app.state.answerer = None
    api.app.state.refresher = Refresher(api.settings, FakeEmbedder(), None)
    try:
        with TestClient(api.app) as client:
            assert client.post("/ingestion/stop").status_code == 401
            assert client.post("/ingestion/stop", headers=H).status_code == 409
            assert client.get("/ingestion/status", headers=H).json()["refresh_stopping"] is False
    finally:
        del api.app.state.searcher, api.app.state.answerer, api.app.state.refresher
