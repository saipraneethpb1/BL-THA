"""HTTP API of the Knowledge Brain. Tasks 2 and 3 talk to this, never to the internals."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from kb.config import settings
from kb.db import connect, init_db


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await init_db(settings.db_path)
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Knowledge Brain",
    version="0.1.0",
    description="Answers product questions from the Brilliant Directories help center, "
    "with citations. Used by the Inbox Agent and the Meeting-to-Scope service.",
)


class Health(BaseModel):
    status: Literal["ok"]
    articles: int


class IngestionStatus(BaseModel):
    pages_by_status: dict[str, int]
    last_run: dict[str, str | int | None] | None


@app.get("/health", response_model=Health)
async def health() -> Health:
    async with connect(settings.db_path) as db:
        rows = list(await db.execute_fetchall("SELECT count(*) AS n FROM articles"))
    return Health(status="ok", articles=rows[0]["n"])


@app.get("/ingestion/status", response_model=IngestionStatus)
async def ingestion_status() -> IngestionStatus:
    async with connect(settings.db_path) as db:
        counts = await db.execute_fetchall(
            "SELECT status, count(*) AS n FROM pages GROUP BY status"
        )
        last = list(
            await db.execute_fetchall("SELECT * FROM ingestion_runs ORDER BY id DESC LIMIT 1")
        )
    return IngestionStatus(
        pages_by_status={r["status"]: r["n"] for r in counts},
        last_run=dict(last[0]) if last else None,
    )
