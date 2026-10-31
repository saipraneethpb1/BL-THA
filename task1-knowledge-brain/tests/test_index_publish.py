"""Stable chunk keys, publish-on-finish, vector reuse, and the new chunking options."""

from collections.abc import Sequence
from pathlib import Path

import aiosqlite
import pytest

from kb.db import connect, init_db
from kb.retrieval.chunker import ChunkingConfig, chunk_article
from kb.retrieval.embedder import Vector
from kb.retrieval.indexer import sync_index
from kb.retrieval.search import Searcher
from tests.fakes import FakeEmbedder
from tests.test_retrieval import ARTICLES, add_article


class CountingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        self.embedded = 0

    def embed_documents(self, texts: Sequence[str]) -> Vector:
        self.embedded += len(texts)
        return super().embed_documents(texts)


@pytest.fixture
async def db(tmp_path: Path):  # type: ignore[no-untyped-def]
    await init_db(tmp_path / "kb.db")
    async with connect(tmp_path / "kb.db") as conn:
        for aid, (title, body) in ARTICLES.items():
            await add_article(conn, aid, title, body)
        yield conn


async def test_chunks_get_stable_keys_and_index_metadata(db: aiosqlite.Connection) -> None:
    await sync_index(db, FakeEmbedder(), run_id=7)
    rows = list(await db.execute_fetchall("SELECT * FROM chunks WHERE article_id='1'"))
    assert rows and all(r["is_active"] == 1 and r["run_id"] == 7 for r in rows)
    assert rows[0]["chunk_key"].startswith("1:0:")
    assert (rows[0]["embedding_model"], rows[0]["embedding_dim"]) == ("fake-bag-of-words", 64)
    # The same text gives the same key on every run.
    await sync_index(db, FakeEmbedder(), force=True)
    again = list(await db.execute_fetchall("SELECT chunk_key FROM chunks WHERE article_id='1'"))
    assert [r["chunk_key"] for r in again] == [r["chunk_key"] for r in rows]


async def test_unchanged_chunks_reuse_their_vectors(db: aiosqlite.Connection) -> None:
    emb = CountingEmbedder()
    await sync_index(db, emb)
    first = emb.embedded
    report = await sync_index(db, emb, force=True)  # same text: nothing to embed
    assert emb.embedded == first and report.vectors_reused == report.chunks_written
    await add_article(db, "1", "Refund a member payment", "Refunds now live under Billing.")
    await sync_index(db, emb)
    assert emb.embedded == first + 1  # only the changed chunk


async def test_staged_chunks_are_invisible_and_dropped_after_a_crash(
    db: aiosqlite.Connection,
) -> None:
    emb = FakeEmbedder()
    await sync_index(db, emb)
    # A crashed run left a staged (inactive) chunk with a new text for article 1.
    await db.execute(
        """INSERT INTO chunks(article_id, source, position, section, text, embedding, is_active)
           VALUES ('1', 'article', 0, 's', 'Half-written refund text', ?, 0)""",
        (emb.embed_documents(["Half-written refund text"])[0].tobytes(),),
    )
    await db.commit()
    searcher = Searcher(emb)
    for mode in ("keyword", "vector"):
        hits = await searcher.search(db, "Half-written refund text", mode=mode)  # type: ignore[arg-type]
        assert all("Half-written" not in h.text for h in hits)
    await sync_index(db, emb)
    [left] = await db.execute_fetchall("SELECT count(*) AS n FROM chunks WHERE is_active=0")
    assert left["n"] == 0


async def test_reindexed_page_is_switched_over_whole(db: aiosqlite.Connection) -> None:
    emb = FakeEmbedder()
    await sync_index(db, emb)
    long_body = "\n\n".join(f"Step {i}. " + "Refund details here. " * 30 for i in range(6))
    await add_article(db, "1", "Refund a member payment", long_body)
    await sync_index(db, emb, config=ChunkingConfig(max_chars=600, min_chars=100))
    rows = list(
        await db.execute_fetchall("SELECT text, is_active FROM chunks WHERE article_id='1'")
    )
    assert len(rows) > 1 and all(r["is_active"] == 1 for r in rows)
    assert all("Finance >> Invoices" not in r["text"] for r in rows)  # no old chunk left


def test_parent_section_heading_path_and_overlap_options() -> None:
    body = (
        "## Setup\n\nIntro "
        + "word " * 50
        + "\n\n### Step 2\n\n"
        + "\n\n".join(
            f"Paragraph {i}. It has a sentence. And another one." + " filler" * 40 for i in range(4)
        )
    )
    plain = chunk_article("Guide", body, config=ChunkingConfig(max_chars=500, min_chars=100))
    assert all(c.text.startswith(("Guide > Setup", "Guide > Step 2")) for c in plain)
    step2 = [c for c in plain if c.section == "Step 2"]
    assert step2 and all(c.parent == "Setup" for c in step2)

    path = chunk_article(
        "Guide", body, config=ChunkingConfig(max_chars=500, min_chars=100, heading_path=True)
    )
    assert any(c.text.startswith("Guide > Setup > Step 2") for c in path)

    overlap = chunk_article(
        "Guide", body, config=ChunkingConfig(max_chars=500, min_chars=100, overlap=0.15)
    )
    later = [c for c in overlap if c.section == "Step 2"][1:]
    assert later and all("And another one." in c.text.split("\n\n")[1] for c in later)
    assert len(overlap) == len(plain)  # overlap repeats text, it does not add chunks


def test_overlap_never_copies_a_screenshot_into_the_next_chunk() -> None:
    body = "\n\n".join([f"Para {i}. Click Save. [IMAGE:img{i}]" + " text" * 60 for i in range(3)])
    chunks = chunk_article(
        "T",
        body,
        {"img0": "Save button"},
        ChunkingConfig(max_chars=400, min_chars=50, overlap=0.15),
    )
    assert [c.image_ids for c in chunks] == [["img0"], ["img1"], ["img2"]]
    assert sum("[Screenshot: Save button]" in c.text for c in chunks) == 1
