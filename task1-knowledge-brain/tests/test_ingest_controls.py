"""Before ingesting: the whole sitemap tree is read and summarised. After: each source has
its own re-check schedule, duplicates are stored once, and every change is recorded."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from kb import api
from kb.config import Settings
from kb.db import connect, init_db
from kb.ingestion import catalog
from kb.ingestion.pipeline import run_ingestion
from kb.ingestion.sources import HostGuard, guarded_client, normalize_url, walk_sitemap
from tests.test_api import H, client  # noqa: F401  (shared fixture)
from tests.test_sources import PUBLIC, WEB_PAGE, fake_resolver, urlset

PUBLIC["blog.test"] = "93.184.216.36"  # a second public site for these tests

EMPTY = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'


def index(*locs: str) -> str:
    return (
        "<sitemapindex>"
        + "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in locs)
        + "</sitemapindex>"
    )


class Shop:
    """A site with an index of indexes, a broken child, and pages listed twice."""

    def __init__(self) -> None:
        self.mirror_same_text = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        docs = {
            "https://help.test/sitemap.xml": EMPTY,
            "https://shop.test/sitemap_index.xml": index(
                "https://shop.test/posts.xml",
                "https://shop.test/more_index.xml",
                "https://shop.test/broken.xml",
            ),
            "https://shop.test/more_index.xml": index("https://shop.test/pages.xml"),
            "https://shop.test/posts.xml": urlset(
                "https://shop.test/a", "https://shop.test/b/", "https://shop.test/c"
            ),
            # /b listed again with a tracking parameter and a trailing slash: the same page
            "https://shop.test/pages.xml": urlset(
                "https://shop.test/b?utm_source=x", "https://shop.test/d"
            ),
            "https://blog.test/sitemap.xml": urlset(
                "https://shop.test/a", "https://blog.test/post"
            ),
        }
        if url in docs:
            return httpx.Response(200, text=docs[url])
        if url == "https://shop.test/broken.xml":
            return httpx.Response(500)
        name = url.rstrip("/").rsplit("/", 1)[-1]
        if self.mirror_same_text and name == "post":
            name = "d"  # the blog post is a copy of the shop's page d
        if url.startswith(("https://shop.test/", "https://blog.test/")):
            return httpx.Response(
                200, text=WEB_PAGE.format(title=f"Page {name}", text=f"All about {name}. " * 5)
            )
        return httpx.Response(404)


def test_urls_are_normalised_so_one_page_has_one_spelling() -> None:
    same = {
        normalize_url(u)
        for u in (
            "https://Shop.test/b",
            "http://shop.test/b/",
            "https://shop.test/b?utm_source=news&utm_medium=x",
            "https://shop.test:443/b#top",
        )
    }
    assert same == {"https://shop.test/b"}
    assert normalize_url("https://shop.test/b?page=2") == "https://shop.test/b?page=2"


async def test_the_whole_tree_is_read_with_totals_per_nested_sitemap() -> None:
    shop = Shop()
    async with guarded_client(
        HostGuard(fake_resolver), transport=httpx.MockTransport(shop.handler)
    ) as c:
        tree = await walk_sitemap(
            c, "https://shop.test/sitemap_index.xml", max_pages=100, max_children=50
        )
    assert tree.kind == "sitemapindex"
    assert [normalize_url(u) for u, _ in tree.locs] == [
        "https://shop.test/a",
        "https://shop.test/b",
        "https://shop.test/c",
        "https://shop.test/d",
    ]
    assert tree.listed_twice == 1 and tree.truncated is None
    by_url = {c.url: c for c in tree.children}
    posts = by_url["https://shop.test/posts.xml"]
    assert (posts.count, posts.first, posts.last) == (
        3,
        "https://shop.test/a",
        "https://shop.test/c",
    )
    assert by_url["https://shop.test/pages.xml"].depth == 2  # inside the nested index
    assert "HTTP 500" in (by_url["https://shop.test/broken.xml"].error or "")

    async with guarded_client(
        HostGuard(fake_resolver), transport=httpx.MockTransport(shop.handler)
    ) as c:
        capped = await walk_sitemap(
            c, "https://shop.test/sitemap_index.xml", max_pages=2, max_children=50
        )
    assert len(capped.locs) == 2 and "2 pages" in (capped.truncated or "")


def test_a_source_is_due_by_its_own_interval() -> None:
    now = datetime(2026, 10, 24, 2, 0, tzinfo=UTC)
    six_days = (now - timedelta(days=6)).strftime("%Y-%m-%d %H:%M:%S")
    assert catalog.is_due(None, 30, now)
    assert catalog.is_due(six_days, 1, now) and not catalog.is_due(six_days, 7, now)
    late = (now - timedelta(days=7) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    assert catalog.is_due(late, 7, now)  # last week's run finished a little later: still due


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "kb.db",
        sitemap_url="https://help.test/sitemap.xml",
        min_interval_s=0,
        max_attempts=1,
    )


async def test_only_due_sources_are_checked_and_the_rest_keep_their_pages(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    await init_db(settings.db_path)
    with sqlite3.connect(settings.db_path) as db:
        db.execute(
            "INSERT INTO custom_sitemaps(url, kind, recheck_days) VALUES ('https://shop.test/sitemap_index.xml', 'sitemapindex', 30)"
        )
    t = httpx.MockTransport(Shop().handler)
    await run_ingestion(settings, transport=t, resolver=fake_resolver)  # first run: everything
    with sqlite3.connect(settings.db_path) as db:
        assert db.execute("SELECT count(*) FROM pages WHERE status='done'").fetchone()[0] == 4

    calls: list[str] = []

    def spy(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return Shop().handler(request)

    report = await run_ingestion(
        settings, transport=httpx.MockTransport(spy), resolver=fake_resolver, scheduled=True
    )
    # Checked yesterday on a 30-day schedule, and the help center was just checked: nothing due.
    assert calls == [] and report.removed == 0
    with sqlite3.connect(settings.db_path) as db:
        assert db.execute("SELECT count(*) FROM pages WHERE status='done'").fetchone()[0] == 4
        assert (
            db.execute("SELECT sources FROM ingestion_runs ORDER BY id DESC").fetchone()[0] is None
        )


async def test_duplicates_are_stored_once_and_searched_once(tmp_path: Path) -> None:
    from kb.retrieval.indexer import sync_index
    from kb.retrieval.search import Searcher
    from tests.fakes import FakeEmbedder

    settings = settings_for(tmp_path)
    await init_db(settings.db_path)
    with sqlite3.connect(settings.db_path) as db:
        db.execute(
            "INSERT INTO custom_sitemaps(url, kind) VALUES ('https://shop.test/sitemap_index.xml', 'sitemapindex')"
        )
        db.execute(
            "INSERT INTO custom_sitemaps(url, kind) VALUES ('https://blog.test/sitemap.xml', 'urlset')"
        )
    shop = Shop()
    shop.mirror_same_text = True
    await run_ingestion(
        settings, transport=httpx.MockTransport(shop.handler), resolver=fake_resolver
    )
    with sqlite3.connect(settings.db_path) as db:
        pages = dict(db.execute("SELECT url, status FROM pages").fetchall())
        listed = db.execute(
            "SELECT count(*) FROM page_listings WHERE article_id = (SELECT article_id FROM pages WHERE url='https://shop.test/a')"
        ).fetchone()[0]
        events = [
            e
            for (e,) in db.execute(
                "SELECT event FROM page_history WHERE article_id=(SELECT article_id FROM pages WHERE url='https://blog.test/post')"
            )
        ]
    assert len(pages) == 5  # a, b, c, d, post: /a listed by both sitemaps is one page
    assert listed == 2
    assert pages["https://blog.test/post"] == "duplicate"  # same text as /d
    assert events == ["added", "duplicate"]
    emb = FakeEmbedder()
    async with connect(settings.db_path) as db:
        await sync_index(db, emb)
        found = [h.url for h in await Searcher(emb).search(db, "All about d", mode="keyword")]
    assert "https://blog.test/post" not in found and "https://shop.test/d" in found


def test_preview_summarises_without_storing_then_confirm_adds_and_crawls(
    client: TestClient,  # noqa: F811
) -> None:
    shop = Shop()
    api.app.state.sitemap_client = lambda: guarded_client(
        HostGuard(fake_resolver), transport=httpx.MockTransport(shop.handler)
    )

    async def validate(url: str, progress: Any = None) -> Any:
        from kb.ingestion.sources import SitemapCheck

        return SitemapCheck(url, "sitemapindex", 3)

    api.app.state.sitemap_validator = validate
    crawled: list[set[str]] = []

    async def fake_ingest(sources: set[str]) -> None:
        crawled.append(sources)

    api.app.state.refresher.ingest_sources = fake_ingest  # type: ignore[method-assign]
    api.sitemap_limiter.calls.clear()
    try:
        r = client.post(
            "/sitemaps/preview/stream",
            json={"urls": ["https://shop.test/sitemap_index.xml"]},
            headers=H,
        )
        events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
        p = events[-1]["done"]["previews"][0]
        assert (p["total"], p["new"], p["already_stored"], p["listed_twice"]) == (4, 4, 0, 1)
        assert p["first"] == "https://shop.test/a" and p["last"] == "https://shop.test/d"
        assert len(p["children"]) == 4 and p["failed_children"] == 1 and p["estimate_s"] > 0
        assert any("4 pages in 4 nested sitemaps" in e.get("text", "") for e in events)
        assert client.get("/sitemaps", headers=H).json()["custom"] == []  # nothing stored

        added = client.post(
            "/sitemaps",
            json={
                "urls": ["https://shop.test/sitemap_index.xml"],
                "recheck_days": 15,
                "ingest": True,
            },
            headers=H,
        ).json()
        assert added["ingesting"] is True
        listed = client.get("/sitemaps", headers=H).json()
        row = listed["custom"][0]
        assert row["recheck_days"] == 15 and row["summary"]["total"] == 4 and row["url_count"] == 4
        assert listed["recheck_choices"] == {
            "1": "Daily",
            "7": "Weekly",
            "15": "Every 15 days",
            "30": "Every 30 days",
        }

        assert (
            client.put(
                f"/sitemaps/{row['id']}/schedule", json={"recheck_days": 30}, headers=H
            ).status_code
            == 200
        )
        assert (
            client.put(
                f"/sitemaps/{row['id']}/schedule", json={"recheck_days": 3}, headers=H
            ).status_code
            == 422
        )
        assert (
            client.put(
                "/sources/help-center/schedule", json={"recheck_days": 7}, headers=H
            ).status_code
            == 200
        )
        listed = client.get("/sitemaps", headers=H).json()
        assert listed["custom"][0]["recheck_days"] == 30 and listed["builtin"]["recheck_days"] == 7
    finally:
        del api.app.state.sitemap_validator
        del api.app.state.sitemap_client
    import asyncio

    asyncio.run(asyncio.sleep(0))
    assert crawled == [{"https://shop.test/sitemap_index.xml"}]


def test_archive_history_duplicates_and_dashboard_counts(client: TestClient) -> None:  # noqa: F811
    with sqlite3.connect(api.settings.db_path) as db:
        for aid, url, status in (
            ("1", "https://h/1", "done"),
            ("2", "https://h/2", "quarantined"),
            ("3", "https://h/3", "duplicate"),
            ("4", "https://h/4", "pending"),
        ):
            db.execute(
                "INSERT INTO pages(article_id, url, status, quarantined_at, last_error, duplicate_of) VALUES (?, ?, ?, '2026-10-20 02:00:00', ?, ?)",
                (
                    aid,
                    url,
                    status,
                    "HTTP 404 and missing from its sitemap" if status == "quarantined" else None,
                    "1" if status == "duplicate" else None,
                ),
            )
            if status != "pending":
                db.execute(
                    "INSERT INTO articles(article_id, url, title, body, content_hash, indexed_hash) VALUES (?, ?, ?, 'x', 'h', ?)",
                    (aid, url, f"Title {aid}", None if aid == "3" else "sig"),
                )
        db.execute(
            "INSERT INTO page_history(article_id, event, detail) VALUES ('2', 'archived', 'HTTP 404 and missing from its sitemap')"
        )
        db.execute(
            "INSERT INTO page_listings VALUES ('1', 'https://a.test/s.xml'), ('1', 'https://b.test/s.xml')"
        )
    archive = client.get("/archive", headers=H).json()
    assert archive["total"] == 1 and archive["items"][0]["title"] == "Title 2"
    assert client.get("/history?article_id=2", headers=H).json()["items"][0]["event"] == "archived"
    dups = client.get("/duplicates", headers=H).json()
    assert dups["same_content"][0]["original_url"] == "https://h/1"
    assert dups["listed_twice"][0]["sources"] == ["https://a.test/s.xml", "https://b.test/s.xml"]
    counts = client.get("/ingestion/status", headers=H).json()["counts"]
    assert counts == {
        "pages_total": 4,
        "embedded": 1,
        "pending_embed": 0,
        "pending_fetch": 1,
        "duplicates": 1,
        "listed_twice": 1,
        "archived": 1,
        "failed": 0,
    }
