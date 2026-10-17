import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
import respx

from kb.bootstrap import article_count, seed_database


def make_db(path: Path, articles: int) -> Path:
    with closing(sqlite3.connect(path)) as c:
        c.execute("CREATE TABLE articles (article_id TEXT)")
        c.executemany("INSERT INTO articles VALUES (?)", [(str(i),) for i in range(articles)])
        c.commit()
    return path


async def test_keeps_an_existing_database(tmp_path: Path) -> None:
    db = make_db(tmp_path / "kb.db", 3)
    assert await seed_database(db, make_db(tmp_path / "seed.db", 9), None) == "existing database"
    assert article_count(db) == 3


async def test_copies_a_local_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "data" / "kb.db"
    result = await seed_database(db, make_db(tmp_path / "seed.db", 5), None)
    assert result.startswith("copied snapshot") and article_count(db) == 5


async def test_ignores_files_that_are_not_knowledge_databases(tmp_path: Path) -> None:
    junk = tmp_path / "seed.db"
    junk.write_text("not a database")
    empty = make_db(tmp_path / "empty.db", 0)
    assert await seed_database(tmp_path / "kb.db", junk, None) == "empty"
    assert await seed_database(tmp_path / "kb.db", empty, None) == "empty"


@respx.mock
async def test_downloads_a_snapshot_and_rejects_bad_ones(tmp_path: Path) -> None:
    good = make_db(tmp_path / "good.db", 4).read_bytes()
    respx.get("https://seed.test/good.db").mock(return_value=httpx.Response(200, content=good))
    respx.get("https://seed.test/html").mock(return_value=httpx.Response(200, text="<html>"))
    respx.get("https://seed.test/missing").mock(return_value=httpx.Response(404))

    for url in ("https://seed.test/html", "https://seed.test/missing"):
        assert await seed_database(tmp_path / "a.db", None, url) == "empty"
        assert not (tmp_path / "a.download").exists()
    result = await seed_database(tmp_path / "b.db", None, "https://seed.test/good.db")
    assert result.startswith("downloaded snapshot") and article_count(tmp_path / "b.db") == 4


def test_snapshot_is_self_contained_and_seeds_a_fresh_install(tmp_path: Path) -> None:
    from kb.snapshot import make_snapshot

    live = make_db(tmp_path / "live.db", 6)
    with closing(sqlite3.connect(live)) as c:  # recent writes sitting in the WAL file
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("INSERT INTO articles VALUES ('late')")
        c.commit()
        snap = tmp_path / "snap" / "knowledge.db"
        assert make_snapshot(live, snap) == 7
    assert not (tmp_path / "snap" / "knowledge.db-wal").exists()


async def test_seed_from_snapshot_after_vacuum(tmp_path: Path) -> None:
    from kb.snapshot import make_snapshot

    snap = tmp_path / "snap.db"
    make_snapshot(make_db(tmp_path / "live.db", 3), snap)
    result = await seed_database(tmp_path / "fresh" / "kb.db", snap, None)
    assert result.startswith("copied snapshot") and article_count(tmp_path / "fresh" / "kb.db") == 3
