"""HTTP API and screens for the Scope Builder."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from scope.config import settings
from scope.knowledge import HttpKnowledgeClient
from scope.llm import ClaudeLLM
from scope.pipeline import ScopeBuilder, export_proposal
from scope.store import StateError, Store, Version
from scope.transcript import TranscriptError, parse_transcript

UI = Path(__file__).parent / "ui" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if not hasattr(app.state, "store"):
        app.state.store = Store(settings.db_path)
    await app.state.store.init()
    if not hasattr(app.state, "builder"):
        app.state.builder = (
            ScopeBuilder(
                app.state.store,
                ClaudeLLM(),
                HttpKnowledgeClient(settings.kb_url, settings.kb_api_key),
                settings,
            )
            if os.environ.get("ANTHROPIC_API_KEY")
            else None
        )
    app.state.tasks = set()
    yield


app = FastAPI(
    title="Scope Builder",
    version="0.1.0",
    lifespan=lifespan,
    description="Turns client meeting transcripts into a scope of work, an estimate and a proposal.",
)


def store(request: Request) -> Store:
    s: Store = request.app.state.store
    return s


class NewProject(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    client: str = Field(min_length=2, max_length=200)


class NewMeeting(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    transcript: str = Field(min_length=50, max_length=500_000)


class Approval(BaseModel):
    actor: str = Field(min_length=2, max_length=100)
    note: str | None = Field(default=None, max_length=2000)


@app.get("/", include_in_schema=False)
async def ui() -> FileResponse:
    return FileResponse(UI)


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    return {"status": "ok", "building_enabled": request.app.state.builder is not None}


@app.get("/api/projects")
async def list_projects(request: Request) -> list[dict[str, Any]]:
    return await store(request).projects()


@app.post("/api/projects", status_code=201)
async def create_project(body: NewProject, request: Request) -> dict[str, int]:
    return {"id": await store(request).create_project(body.name, body.client)}


@app.get("/api/projects/{pid}")
async def get_project(pid: int, request: Request) -> dict[str, Any]:
    s = store(request)
    project = await s.project(pid)
    if project is None:
        raise HTTPException(404, "project not found")
    versions = await s.versions(pid)
    return {
        "project": project,
        "meetings": [
            {k: v for k, v in m.items() if k != "transcript"} for m in await s.meetings(pid)
        ],
        "versions": [
            {
                "number": v.number,
                "status": v.status,
                "baseline_version": v.baseline_version,
                "created_at": v.created_at,
                "approved_by": v.approved_by,
                "error": v.error,
                "blocking": sum(i.severity == "block" for i in v.issues),
            }
            for v in versions
        ],
        "audit": await s.audit_log(pid),
    }


@app.post("/api/projects/{pid}/meetings", status_code=201)
async def add_meeting(pid: int, body: NewMeeting, request: Request) -> dict[str, Any]:
    s = store(request)
    if await s.project(pid) is None:
        raise HTTPException(404, "project not found")
    try:
        parsed = parse_transcript(body.transcript, "check", body.title, body.date)
    except TranscriptError as exc:
        raise HTTPException(422, str(exc)) from exc
    mid = await s.add_meeting(pid, body.title, body.date, body.transcript)
    return {
        "id": mid,
        "turns": len(parsed.turns),
        "speakers": sorted({t.speaker for t in parsed.turns}),
    }


@app.post("/api/projects/{pid}/build", status_code=202)
async def build(pid: int, request: Request) -> dict[str, int]:
    """Starts building a new draft version in the background; poll the version for status."""
    builder: ScopeBuilder | None = request.app.state.builder
    if builder is None:
        raise HTTPException(503, "building is disabled: ANTHROPIC_API_KEY is not configured")
    try:
        number = await builder.start(pid)
    except StateError as exc:
        raise HTTPException(409, str(exc)) from exc
    task = asyncio.create_task(builder.build(pid, number))
    request.app.state.tasks.add(task)
    task.add_done_callback(request.app.state.tasks.discard)
    return {"version": number}


@app.get("/api/projects/{pid}/versions/{number}")
async def get_version(pid: int, number: int, request: Request) -> Version:
    v = await store(request).version(pid, number)
    if v is None:
        raise HTTPException(404, "version not found")
    return v


@app.post("/api/projects/{pid}/versions/{number}/approve")
async def approve(pid: int, number: int, body: Approval, request: Request) -> dict[str, str]:
    try:
        await store(request).approve(pid, number, body.actor, body.note)
    except StateError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"status": "approved"}


@app.get("/api/projects/{pid}/versions/{number}/export", response_class=PlainTextResponse)
async def export(pid: int, number: int, request: Request, actor: str = "user") -> str:
    """Client-ready proposal as Markdown. 409 unless the version is approved."""
    try:
        return await export_proposal(store(request), pid, number, actor)
    except StateError as exc:
        status = 404 if "not found" in str(exc) else 409
        raise HTTPException(status, str(exc)) from exc
