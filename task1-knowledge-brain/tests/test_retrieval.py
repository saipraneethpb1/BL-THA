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


def test_changelog_titles_are_recognised() -> None:
    from kb.retrieval.search import is_changelog

    assert is_changelog("Changelog - October 2019")
    assert is_changelog("07/10/20 - Software Updates")
    assert not is_changelog("Google Map Search Results")
    assert not is_changelog("How to read the software updates page")  # only as a suffix


async def test_changelogs_rank_lower_and_articles_are_capped(db: aiosqlite.Connection) -> None:
    emb = FakeEmbedder()
    await add_article(db, "9", "Changelog - March 2026", "Refund a member: new refund button.")
    long_body = "\n\n".join(
        f"### Part {i}\n\nRefund member step {i} " + "word " * 300 for i in range(4)
    )
    await add_article(db, "8", "Refund guide", long_body)
    await sync_index(db, emb)
    hits = await Searcher(emb, changelog_weight=0.5, max_per_article=2).search(
        db, "refund a member", k=6
    )
    per_article = {}
    for h in hits:
        per_article[h.article_id] = per_article.get(h.article_id, 0) + 1
    assert max(per_article.values()) <= 2
    ranks = [h.article_id for h in hits]
    assert ranks.index("1") < ranks.index("9")  # the real article beats the release note
    uncapped = await Searcher(emb, changelog_weight=1.0, max_per_article=None).search(
        db, "refund a member", k=6
    )
    assert sum(h.article_id == "8" for h in uncapped) > 2


class FakeRewriter:
    def __init__(self, queries: list[str]) -> None:
        self.queries = queries
        self.calls = 0

    async def rewrite(self, question: str) -> list[str]:
        self.calls += 1
        return self.queries


async def test_rewriting_finds_articles_the_customer_wording_misses(
    db: aiosqlite.Connection,
) -> None:
    emb = FakeEmbedder()
    await sync_index(db, emb)
    question = "my company picture at the top looks old"  # shares no words with "logo"
    plain = await Searcher(emb).search(db, question, k=1)
    rewriter = FakeRewriter(["change the site logo", "Design Settings Logo"])
    rewritten = await Searcher(emb, rewriter=rewriter).search(db, question, k=1)
    assert rewritten[0].article_id == "2" and rewriter.calls == 1
    # The best similarity across queries feeds the evidence gate: a weak chance match
    # on the original wording becomes a strong match through the rewrite.
    assert plain[0].score < 0.3 < 0.5 < rewritten[0].score


async def test_failed_rewrite_falls_back_to_the_question(db: aiosqlite.Connection) -> None:
    emb = FakeEmbedder()
    await sync_index(db, emb)
    hits = await Searcher(emb, rewriter=FakeRewriter([])).search(db, "refund a member", k=1)
    assert hits[0].article_id == "1"
    vector_only = FakeRewriter(["unused"])
    await Searcher(emb, rewriter=vector_only).search(db, "refund a member", k=1, mode="vector")
    assert vector_only.calls == 0  # only hybrid search rewrites
