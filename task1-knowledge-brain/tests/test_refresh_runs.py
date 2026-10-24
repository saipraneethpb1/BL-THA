"""Refresh runs: every fetched URL is recorded with its outcome, and the API reports
first and last fetch time, totals and the full list."""

import sqlite3
from collections.abc import Iterator
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
from tests.test_pipeline import FakeSite, sitemap

H = {"X-API-Key": "secret"}


async def test_each_fetch_is_logged_with_its_outcome(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "kb.db",
        sitemap_url="https://help.test/sitemap.xml",
        min_interval_s=0,
        max_attempts=1,
    )
    site = FakeSite()
    site.sitemap = sitemap(("1", "d1"), ("2", "d1"), ("3", "d1"))
    site.gone, site.broken = {"2"}, {"3"}
    t = httpx.MockTransport(site.handler)
    first = await run_ingestion(settings, transport=t)
    site.sitemap = sitemap(("1", "d2"), ("2", "d1"), ("3", "d2"))  # 1 changes on paper only
    site.gone, site.broken = set(), set()
    second = await run_ingestion(settings, transport=t)

    with sqlite3.connect(settings.db_path) as db:
        log = db.execute(
            "SELECT run_id, url, outcome, detail, source FROM fetch_log ORDER BY id"
        ).fetchall()
    run1 = {u.rsplit("/", 1)[-1]: (o, d) for r, u, o, d, _ in log if r == first.run_id}
    assert run1 == {
        "1": ("updated", None),
        "2": ("broken", "HTTP 404 but still listed in its sitemap (counting)"),
        "3": ("failed", "gave up after 1 attempts: HTTP 503"),
    }
    run2 = {u.rsplit("/", 1)[-1]: o for r, u, o, _, _ in log if r == second.run_id}
    # A broken page still listed in the sitemap is retried and recovers on HTTP 200.
    assert run2 == {"1": "unchanged", "2": "updated", "3": "updated"}
    assert {s for *_, s in log} == {"help-center"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(api.settings, "db_path", tmp_path / "kb.db")
    monkeypatch.setattr(api.settings, "api_key", "secret")
    monkeypatch.setattr(api.settings, "refresh_enabled", False)
    monkeypatch.setattr(api.settings, "bootstrap_crawl", False)
    monkeypatch.setattr(api.settings, "speech_preload", False)
    api.app.state.searcher = Searcher(FakeEmbedder())
    api.app.state.answerer = None
    api.app.state.refresher = Refresher(api.settings, FakeEmbedder(), None)
    with TestClient(api.app) as c:
        yield c
    del api.app.state.searcher, api.app.state.answerer, api.app.state.refresher


def test_runs_report_first_and_last_fetch_and_list_every_url(client: TestClient) -> None:
    with sqlite3.connect(api.settings.db_path) as db:
        db.execute(
            "INSERT INTO ingestion_runs(id, started_at, finished_at, status) VALUES (1, '2026-10-20 02:00:00', '2026-10-20 02:00:03', 'finished')"
        )
        db.execute(
            "INSERT INTO ingestion_runs(id, started_at, finished_at, status) VALUES (2, '2026-10-21 02:00:00', '2026-10-21 02:05:00', 'finished')"
        )
        db.executemany(
            "INSERT INTO fetch_log(run_id, url, source, outcome, detail, fetched_at) VALUES (2, ?, ?, ?, ?, ?)",
            [
                ("https://help.test/a", "help-center", "updated", None, "2026-10-21T02:00:05Z"),
                (
                    "https://shop.test/b",
                    "https://shop.test/sitemap.xml",
                    "failed",
                    "HTTP 503",
                    "2026-10-21T02:01:00Z",
                ),
                ("https://help.test/c", "help-center", "unchanged", None, "2026-10-21T02:04:59Z"),
            ],
        )
    assert client.get("/ingestion/runs").status_code == 401
    runs = client.get("/ingestion/runs", headers=H).json()
    assert [r["id"] for r in runs] == [2, 1]
    latest, older = runs
    assert (latest["first_fetch_at"], latest["last_fetch_at"], latest["fetched_urls"]) == (
        "2026-10-21T02:00:05Z",
        "2026-10-21T02:04:59Z",
        3,
    )
    assert latest["outcomes"] == {"updated": 1, "failed": 1, "unchanged": 1}
    assert latest["started_at"] == "2026-10-21T02:00:00Z"
    assert (older["fetched_urls"], older["first_fetch_at"]) == (
        0,
        None,
    )  # nothing fetched or logged

    urls = client.get("/ingestion/runs/2/urls", headers=H).json()["urls"]
    assert [u["url"] for u in urls] == [
        "https://help.test/a",
        "https://shop.test/b",
        "https://help.test/c",
    ]
    assert urls[1]["detail"] == "HTTP 503" and urls[1]["source"] == "https://shop.test/sitemap.xml"
    assert client.get("/ingestion/runs/99/urls", headers=H).status_code == 404
