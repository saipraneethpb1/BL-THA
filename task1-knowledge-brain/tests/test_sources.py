"""Custom sitemaps: validating what people type, the private-address guard, and crawling."""

import sqlite3
from pathlib import Path

import httpx
import pytest

from kb.config import Settings
from kb.db import init_db
from kb.ingestion.pipeline import run_ingestion
from kb.ingestion.sources import (
    HostGuard,
    SitemapError,
    check_url_syntax,
    fetch_sitemap,
    guarded_client,
    parse_any_sitemap,
    web_article_id,
)

PUBLIC = {"help.test": "93.184.216.34", "shop.test": "93.184.216.35"}
PRIVATE = {
    "intranet.test": "10.0.0.5",
    "metadata.test": "169.254.169.254",
    "loop.test": "127.0.0.1",
}


async def fake_resolver(host: str) -> list[str]:
    ip = {**PUBLIC, **PRIVATE}.get(host)
    if ip is None:
        raise OSError("not found")
    return [ip]


def urlset(*urls: str, namespace: bool = True) -> str:
    ns = ' xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"' if namespace else ""
    items = "".join(f"<url><loc>{u}</loc><lastmod>2026-10-01</lastmod></url>" for u in urls)
    return f"<?xml version='1.0'?><urlset{ns}>{items}</urlset>"


WEB_PAGE = """<html><head><title>{title}</title></head><body>
<nav>Home | Shop | Contact</nav><main><h1>{title}</h1>
<p>{text}</p><p>Opening hours are Monday to Friday, nine to five, and we answer every
message within one working day. Delivery takes three to five days inside the country.</p>
</main><footer>Copyright</footer></body></html>"""


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "Enter a sitemap URL"),
        ("ftp://shop.test/sitemap.xml", "must start with http"),
        ("https://localhost/sitemap.xml", "full domain name"),
        ("https://user:pw@shop.test/sitemap.xml", "username or password"),
        ("https://shop.test/" + "a" * 3000, "too long"),
    ],
)
def test_syntax_errors_are_explained(raw: str, message: str) -> None:
    with pytest.raises(SitemapError, match=message):
        check_url_syntax(raw)


def test_syntax_cleans_up() -> None:
    assert (
        check_url_syntax("  https://shop.test/sitemap.xml#top ") == "https://shop.test/sitemap.xml"
    )


@pytest.mark.parametrize("host", sorted(PRIVATE))
async def test_private_and_internal_hosts_are_refused(host: str) -> None:
    with pytest.raises(SitemapError, match="private or internal"):
        await HostGuard(fake_resolver).check(host)


async def test_public_hosts_pass_and_unknown_ones_are_explained() -> None:
    guard = HostGuard(fake_resolver)
    await guard.check("shop.test")
    with pytest.raises(SitemapError, match="couldn't be found"):
        await guard.check("nowhere.test")


def test_parse_any_sitemap_handles_indexes_and_missing_namespace() -> None:
    assert parse_any_sitemap(urlset("https://shop.test/a", namespace=False)).locs == [
        ("https://shop.test/a", "2026-10-01")
    ]
    index = "<sitemapindex><sitemap><loc>https://shop.test/s1.xml</loc></sitemap></sitemapindex>"
    assert parse_any_sitemap(index).kind == "sitemapindex"
    for bad, msg in (
        ("<html><body>hi</body></html>", "isn't a sitemap"),
        ("not xml", "doesn't return XML"),
        (urlset(), "empty"),
    ):
        with pytest.raises(SitemapError, match=msg):
            parse_any_sitemap(bad)


async def test_fetch_checks_every_redirect_and_the_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/moved.xml":
            return httpx.Response(301, headers={"location": "http://metadata.test/latest/"})
        if request.url.path == "/gone.xml":
            return httpx.Response(404)
        return httpx.Response(200, text=urlset("https://shop.test/a"))

    async with guarded_client(
        HostGuard(fake_resolver), transport=httpx.MockTransport(handler)
    ) as c:
        assert "urlset" in await fetch_sitemap(c, "https://shop.test/sitemap.xml")
        with pytest.raises(SitemapError, match="private or internal"):
            await fetch_sitemap(c, "https://shop.test/moved.xml")
        with pytest.raises(SitemapError, match="HTTP 404"):
            await fetch_sitemap(c, "https://shop.test/gone.xml")


class Sites:
    """The help center plus one customer site, behind one fake network."""

    def __init__(self) -> None:
        self.custom_ok = True
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if url == "https://help.test/sitemap.xml":  # an empty help center keeps the test focused
            return httpx.Response(
                200, text='<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
            )
        if url == "https://shop.test/sitemap_index.xml":
            if not self.custom_ok:
                return httpx.Response(500)
            return httpx.Response(
                200,
                text="<sitemapindex><sitemap><loc>https://shop.test/pages.xml</loc></sitemap></sitemapindex>",
            )
        if url == "https://shop.test/pages.xml":
            return httpx.Response(
                200,
                text=urlset(
                    "https://shop.test/hours",
                    "https://shop.test/delivery",
                    "http://intranet.test/admin",
                ),
            )
        if url.startswith("https://shop.test/"):
            name = url.rsplit("/", 1)[-1]
            return httpx.Response(
                200, text=WEB_PAGE.format(title=f"Shop {name}", text=f"All about {name}. " * 5)
            )
        return httpx.Response(404)


async def test_custom_sitemaps_are_crawled_kept_on_outage_and_quarantined_when_removed(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "kb.db",
        sitemap_url="https://help.test/sitemap.xml",
        min_interval_s=0,
        max_attempts=1,
    )
    await init_db(settings.db_path)
    with sqlite3.connect(settings.db_path) as db:
        db.execute(
            "INSERT INTO custom_sitemaps(url, kind) VALUES ('https://shop.test/sitemap_index.xml', 'sitemapindex')"
        )
    sites = Sites()
    t = httpx.MockTransport(sites.handler)

    report = await run_ingestion(settings, transport=t, resolver=fake_resolver)
    assert (report.new, report.fetched) == (3, 2)  # the intranet page is refused, not fetched
    assert not any("intranet.test" in c for c in sites.calls)
    with sqlite3.connect(settings.db_path) as db:
        pages = dict(db.execute("SELECT url, status FROM pages").fetchall())
        titles = sorted(r[0] for r in db.execute("SELECT title FROM articles"))
        body = db.execute(
            "SELECT body FROM articles WHERE url='https://shop.test/hours'"
        ).fetchone()[0]
        checked = db.execute("SELECT url_count, last_error FROM custom_sitemaps").fetchone()
    assert pages["http://intranet.test/admin"] == "quarantined"
    assert titles == ["Shop delivery", "Shop hours"]
    assert "Opening hours" in body and "Home | Shop" not in body and "Copyright" not in body
    assert checked == (3, None)

    # The customer's site is down at the next refresh: its pages must survive.
    sites.custom_ok = False
    await run_ingestion(settings, transport=t, resolver=fake_resolver)
    with sqlite3.connect(settings.db_path) as db:
        assert db.execute("SELECT count(*) FROM articles").fetchone()[0] == 2
        assert "HTTP 500" in db.execute("SELECT last_error FROM custom_sitemaps").fetchone()[0]

    # The sitemap is removed: its pages are quarantined, never deleted.
    with sqlite3.connect(settings.db_path) as db:
        db.execute("DELETE FROM custom_sitemaps")
    report = await run_ingestion(settings, transport=t, resolver=fake_resolver)
    assert report.removed == 2
    with sqlite3.connect(settings.db_path) as db:
        assert db.execute("SELECT count(*) FROM articles").fetchone()[0] == 2
        statuses = {r[0] for r in db.execute("SELECT status FROM pages WHERE kind='web'")}
    assert statuses == {"quarantined"}
    assert web_article_id("https://shop.test/hours").startswith("web-")


async def test_search_skips_quarantined_pages_but_keeps_their_passages(tmp_path: Path) -> None:
    from kb.db import connect
    from kb.retrieval.indexer import sync_index
    from kb.retrieval.search import Searcher
    from tests.fakes import FakeEmbedder

    settings = Settings(
        db_path=tmp_path / "kb.db",
        sitemap_url="https://help.test/sitemap.xml",
        min_interval_s=0,
        max_attempts=1,
    )
    await init_db(settings.db_path)
    with sqlite3.connect(settings.db_path) as db:
        db.execute(
            "INSERT INTO custom_sitemaps(url, kind) VALUES ('https://shop.test/pages.xml', 'urlset')"
        )
    await run_ingestion(
        settings, transport=httpx.MockTransport(Sites().handler), resolver=fake_resolver
    )
    emb = FakeEmbedder()
    searcher = Searcher(emb)
    async with connect(settings.db_path) as db:
        await sync_index(db, emb)
        found = {h.url for h in await searcher.search(db, "opening hours delivery", mode="keyword")}
        assert found == {"https://shop.test/hours", "https://shop.test/delivery"}

        await db.execute(
            "UPDATE pages SET status='quarantined' WHERE url='https://shop.test/hours'"
        )
        await db.commit()
        await sync_index(db, emb)
        found = {h.url for h in await searcher.search(db, "opening hours delivery", mode="keyword")}
        assert found == {"https://shop.test/delivery"}
        kept = await db.execute_fetchall(
            "SELECT count(*) AS n FROM chunks c JOIN pages p USING (article_id) WHERE p.status='quarantined'"
        )
        assert kept[0]["n"] > 0  # still stored, just not searched
