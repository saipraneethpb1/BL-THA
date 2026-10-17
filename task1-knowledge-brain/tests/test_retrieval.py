from pathlib import Path

import aiosqlite
import pytest

from kb.db import connect, init_db
from kb.retrieval.indexer import sync_index
from kb.retrieval.search import Searcher, fts_query
from tests.fakes import FakeEmbedder

ARTICLES = {
    "1": (
        "Refund a member payment",
        "To refund a member go to Finance >> Invoices and click Refund.",
    ),
    "2": ("Change the site logo", "Upload a new logo under Design Settings >> Logo. [IMAGE:9]"),
    "3": ("Enable Lead Matching", "Turn on the Enable Lead Matching switch in Lead Settings."),
}


async def add_article(db: aiosqlite.Connection, aid: str, title: str, body: str) -> None:
    await db.execute(
        "INSERT INTO pages(article_id, url, status) VALUES (?, ?, 'done') ON CONFLICT DO NOTHING",
        (aid, f"https://help.test/{aid}"),
    )
    await db.execute(
        """INSERT INTO articles(article_id, url, title, body, content_hash) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(article_id) DO UPDATE SET body=excluded.body,
           content_hash=excluded.content_hash""",
        (aid, f"https://help.test/{aid}", title, body, str(hash(body))),
    )
    await db.commit()


@pytest.fixture
async def db(tmp_path: Path):  # type: ignore[no-untyped-def]
    await init_db(tmp_path / "kb.db")
    async with connect(tmp_path / "kb.db") as conn:
        for aid, (title, body) in ARTICLES.items():
            await add_article(conn, aid, title, body)
        yield conn


async def test_first_index_then_only_changes(db: aiosqlite.Connection) -> None:
    emb = FakeEmbedder()
    first = await sync_index(db, emb)
    assert (first.reindexed, first.dropped) == (3, 0)

    again = await sync_index(db, emb)
    assert again.reindexed == 0  # nothing changed, nothing re-embedded

    await add_article(db, "1", "Refund a member payment", "Refunds now live under Billing.")
    await db.execute("DELETE FROM articles WHERE article_id='3'")
    await db.commit()
    third = await sync_index(db, emb)
    assert third.reindexed == 1 and third.dropped >= 1


async def test_new_image_description_triggers_reindex(db: aiosqlite.Connection) -> None:
    emb = FakeEmbedder()
    await sync_index(db, emb)
    await db.execute(
        """INSERT INTO images(image_id, article_id, position, src, description)
           VALUES ('9', '2', 0, 'http://img', 'Logo upload box with a Browse button')"""
    )
    await db.commit()
    report = await sync_index(db, emb)
    assert report.reindexed == 1
    hits = await Searcher(emb).search(db, "Browse button", mode="keyword")
    assert hits and hits[0].article_id == "2" and hits[0].image_ids == ["9"]


@pytest.mark.parametrize("mode", ["hybrid", "vector", "keyword"])
async def test_search_finds_the_right_article(db: aiosqlite.Connection, mode: str) -> None:
    await sync_index(db, FakeEmbedder())
    hits = await Searcher(FakeEmbedder()).search(db, "how do I refund a member?", mode=mode)  # type: ignore[arg-type]
    assert hits[0].article_id == "1"
    assert hits[0].url == "https://help.test/1"


async def test_removed_article_never_returned(db: aiosqlite.Connection) -> None:
    emb = FakeEmbedder()
    await sync_index(db, emb)
    await db.execute("DELETE FROM articles WHERE article_id='3'")
    await db.commit()
    await sync_index(db, emb)
    hits = await Searcher(emb).search(db, "Enable Lead Matching", k=10)
    assert "3" not in {h.article_id for h in hits}


def test_fts_query_is_safe_and_drops_stopwords() -> None:
    assert fts_query('How do I "refund" a member?') == '"refund" OR "member"'
    assert fts_query("how do I") is None
    assert fts_query('x" OR 1=1 --') == '"x" OR "1"'
