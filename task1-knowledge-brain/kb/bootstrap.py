"""First start: get a knowledge base without making the user wait 40 minutes.

Order of preference:
1. A database already exists and has articles: use it.
2. KB_SEED_PATH points at a snapshot file (e.g. mounted into the container): copy it.
3. KB_SEED_URL points at a snapshot (e.g. a GitHub release asset): download it.
4. Otherwise the caller starts a full polite crawl in the background.
The daily refresh then brings any snapshot up to date, fetching only what changed.
"""

import asyncio
import logging
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx

log = logging.getLogger(__name__)
SQLITE_HEADER = b"SQLite format 3\x00"


def article_count(path: Path, read_only: bool = False) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    try:
        # A seed may sit on a read-only mount: open it immutable so SQLite never tries
        # to create journal or WAL files next to it.
        target = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1" if read_only else str(path)
        # closing(): sqlite3's own context manager commits but does not close, which on
        # Windows keeps the file locked and breaks the rename below.
        with closing(sqlite3.connect(target, uri=read_only)) as c:
            return int(c.execute("SELECT count(*) FROM articles").fetchone()[0])
    except sqlite3.DatabaseError:
        return 0


def _usable(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as f:
        if f.read(16) != SQLITE_HEADER:
            return False
    return article_count(path, read_only=True) > 0


def _seed_from_file(db_path: Path, seed_path: Path | None) -> bool:
    if seed_path is None or not _usable(seed_path):
        return False
    shutil.copyfile(seed_path, db_path)
    return True


def _install_download(db_path: Path, data: bytes) -> bool:
    tmp = db_path.with_suffix(".download")
    try:
        tmp.write_bytes(data)
        if not _usable(tmp):
            return False
        tmp.replace(db_path)
        return True
    finally:
        tmp.unlink(missing_ok=True)


async def seed_database(db_path: Path, seed_path: Path | None, seed_url: str | None) -> str:
    """Returns what happened, for the logs and the status endpoint."""
    if await asyncio.to_thread(article_count, db_path) > 0:
        return "existing database"
    await asyncio.to_thread(db_path.parent.mkdir, parents=True, exist_ok=True)

    if await asyncio.to_thread(_seed_from_file, db_path, seed_path):
        return f"copied snapshot from {seed_path}"

    if seed_url:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                resp = await client.get(seed_url)
                resp.raise_for_status()
            if await asyncio.to_thread(_install_download, db_path, resp.content):
                return f"downloaded snapshot from {seed_url}"
            log.warning("seed download is not a usable knowledge database; ignoring it")
        except httpx.HTTPError as exc:
            log.warning("seed download failed: %s", exc)
    return "empty"
