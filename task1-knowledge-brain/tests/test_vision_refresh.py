import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import httpx
import pytest

from kb.config import Settings
from kb.db import connect, init_db
from kb.ingestion.vision import (
    DescribeResult,
    ImageDescription,
    ImageJob,
    describe_pending_images,
)
from kb.refresh import Refresher, seconds_until
from tests.fakes import FakeEmbedder
from tests.test_pipeline import FakeSite, sitemap


class FakeDescriber:
    model = "fake-vision"

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.seen: list[ImageJob] = []

    async def describe(self, job: ImageJob) -> DescribeResult:
        self.seen.append(job)
        if job.src in self.fail_on:
            raise ValueError("unreadable image")
        kind = "decorative" if "logo" in job.src else "screenshot"
        desc = ImageDescription(
            kind=kind, description=f"Panel for {job.title}", ui_labels=["Save Changes"]
        )
        return DescribeResult(desc, input_tokens=1000, output_tokens=50)


async def seed_images(db: aiosqlite.Connection) -> None:
    for aid in ("1", "2"):
        await db.execute(
            "INSERT INTO pages(article_id, url, status) VALUES (?, ?, 'done')", (aid, f"u{aid}")
        )
        await db.execute(
            """INSERT INTO articles(article_id, url, title, body, content_hash)
               VALUES (?, ?, ?, 'b', 'h')""",
            (aid, f"u{aid}", f"Article {aid}"),
        )
    rows = [
        ("a", "1", "http://img/shared.png"),
        ("b", "2", "http://img/shared.png"),  # same screenshot reused in article 2
        ("c", "1", "http://img/logo.png"),
        ("d", "2", "http://img/broken.png"),
    ]
    for pos, (iid, aid, src) in enumerate(rows):
        await db.execute(
            "INSERT INTO images(image_id, article_id, position, src) VALUES (?, ?, ?, ?)",
            (iid, aid, pos, src),
        )
    await db.commit()


@pytest.fixture
async def db(tmp_path: Path):  # type: ignore[no-untyped-def]
    await init_db(tmp_path / "kb.db")
    async with connect(tmp_path / "kb.db") as conn:
        await seed_images(conn)
        yield conn


async def descriptions(db: aiosqlite.Connection) -> dict[str, tuple[str | None, str | None]]:
    rows = await db.execute_fetchall("SELECT image_id, description, described_with FROM images")
    return {r["image_id"]: (r["description"], r["described_with"]) for r in rows}


async def test_each_url_described_once_and_failures_isolated(db: aiosqlite.Connection) -> None:
    fake = FakeDescriber(fail_on={"http://img/broken.png"})
    report = await describe_pending_images(db, fake)
    assert sorted(j.src for j in fake.seen) == [
        "http://img/broken.png",
        "http://img/logo.png",
        "http://img/shared.png",
    ]
    assert (report.described, report.failed) == (2, 1)
    d = await descriptions(db)
    assert d["a"] == d["b"]  # shared screenshot: one call, both rows filled
    assert d["a"][0] == "Panel for Article 1 Visible labels: Save Changes."
    assert d["c"] == ("", "fake-vision")  # decorative: stored empty, never indexed
    assert d["d"][1] == "failed:fake-vision:ValueError"


async def test_second_run_does_nothing_and_new_rows_reuse(db: aiosqlite.Connection) -> None:
    fake = FakeDescriber()
    await describe_pending_images(db, fake)
    await db.execute(
        """INSERT INTO images(image_id, article_id, position, src)
           VALUES ('e', '2', 9, 'http://img/shared.png')"""
    )
    await db.commit()
    fake.seen.clear()
    report = await describe_pending_images(db, fake)
    assert fake.seen == [] and report.reused == 1


async def test_retry_failed(db: aiosqlite.Connection) -> None:
    await describe_pending_images(db, FakeDescriber(fail_on={"http://img/broken.png"}))
    fake = FakeDescriber()
    await describe_pending_images(db, fake)
    assert fake.seen == []  # failures are not retried by default
    report = await describe_pending_images(db, fake, retry_failed=True)
    assert report.described == 1


def test_seconds_until_next_run() -> None:
    now = datetime(2026, 10, 17, 1, 30, tzinfo=UTC)
    assert seconds_until(2, now) == 30 * 60
    assert seconds_until(1, now) == 23.5 * 3600


async def test_refresh_runs_all_steps_and_refuses_overlap(tmp_path: Path) -> None:
    site = FakeSite()
    site.sitemap = sitemap(("1", "d1"))
    settings = Settings(
        db_path=tmp_path / "kb.db", sitemap_url="https://help.test/sitemap.xml", min_interval_s=0
    )
    describer = FakeDescriber()
    refresher = Refresher(settings, FakeEmbedder(), describer, httpx.MockTransport(site.handler))
    result = await refresher.run()
    assert result.status == "finished", result.error
    assert result.ingestion["fetched"] == 1
    assert result.vision is not None and result.vision["described"] == 4  # fixture article
    assert result.index["reindexed"] == 1
    with sqlite3.connect(settings.db_path) as c:
        text = "".join(t for (t,) in c.execute("SELECT text FROM chunks"))
    assert "[Screenshot: Panel for How To Import Categories Via CSV File" in text

    await refresher._lock.acquire()
    try:
        with pytest.raises(RuntimeError):
            await refresher.run()
    finally:
        refresher._lock.release()


async def test_refresh_reports_failure_instead_of_crashing(tmp_path: Path) -> None:
    def down(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    settings = Settings(
        db_path=tmp_path / "kb.db",
        sitemap_url="https://help.test/sitemap.xml",
        min_interval_s=0,
        max_attempts=1,
    )
    refresher = Refresher(settings, FakeEmbedder(), None, httpx.MockTransport(down))
    result = await refresher.run()
    assert result.status == "failed" and "FetchError" in (result.error or "")


def test_long_label_lists_are_trimmed_not_rejected() -> None:
    d = ImageDescription(
        kind="screenshot", description="Sheet", ui_labels=[f"L{i}" for i in range(14)]
    )
    text = d.as_text()
    assert "L11" in text and "L12" not in text
