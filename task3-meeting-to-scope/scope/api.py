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

from scope import claude
from scope.config import settings
from scope.guard import RateLimiter, over_budget
from scope.knowledge import HttpKnowledgeClient
from scope.llm import ClaudeLLM
from scope.pipeline import ScopeBuilder, export_proposal
from scope.session import COOKIE, GUEST, account_from_token
from scope.store import StateError, Store, Version
from scope.transcript import TranscriptError, parse_transcript
from scope.workspaces import SingleWorkspace, Workspaces, merge_usage

UI = Path(__file__).parent / "ui" / "index.html"
build_limiter = RateLimiter(settings.build_limit_per_hour, 3600, "scope builds")
SAMPLES_DIR = Path(
    os.environ.get("SCOPE_SAMPLES_DIR", Path(__file__).parent.parent / "transcripts")
)
# Real recorded meetings from the AMI corpus (CC BY 4.0, see transcripts/AMI_ATTRIBUTION.md),
# so a reviewer can try the Scope Builder without writing a transcript.
SAMPLES = {
    "ami_ES2002b": ("Remote control: functional design (AMI ES2002b)", "2026-10-05"),
    "ami_ES2002c": ("Remote control: conceptual design (AMI ES2002c)", "2026-10-12"),
    "ami_ES2002d": ("Remote control: follow-up, detailed design (AMI ES2002d)", "2026-10-19"),
}


def _make_builder(store: Store) -> ScopeBuilder | None:
    if not claude.configured():
        return None
    return ScopeBuilder(
        store, ClaudeLLM(), HttpKnowledgeClient(settings.kb_url, settings.kb_api_key), settings
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if hasattr(app.state, "store"):  # a test injected one store (and builder) for everyone
        await app.state.store.init()
        app.state.workspaces = SingleWorkspace(app.state)
    else:
        starter = settings.starter_db_path or settings.db_path.parent / "starter.db"
        app.state.workspaces = Workspaces(settings.db_path, _make_builder, starter)
        await app.state.workspaces.get(GUEST)
    app.state.tasks = set()
    yield


app = FastAPI(
    title="Scope Builder",
    version="0.1.0",
    lifespan=lifespan,
    description="Turns client meeting transcripts into a scope of work, an estimate and a proposal.",
)


def account(request: Request) -> str:
    """The signed-in demo account, or "guest" (the shared workspace)."""
    return account_from_token(request.cookies.get(COOKIE)) or GUEST


async def workspace(request: Request) -> tuple[Store, ScopeBuilder | None]:
    pair: tuple[Store, ScopeBuilder | None] = await request.app.state.workspaces.get(
        account(request)
    )
    return pair


async def store(request: Request) -> Store:
    return (await workspace(request))[0]


async def site_usage(request: Request) -> dict[str, Any]:
    """Spend across every workspace: budgets and the usage bar are site-wide."""
    stores = await request.app.state.workspaces.all_stores()
    return merge_usage([await s.usage() for s in stores])


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
    return {"status": "ok", "building_enabled": request.app.state.workspaces.building}


@app.get("/api/usage")
async def usage(request: Request) -> dict[str, Any]:
    """For the usage bar: Claude spend recorded on builds, and the latest build failure."""
    u = await site_usage(request)
    budget = settings.budget_usd
    return {
        **u,
        "budget_usd": budget,
        "over_budget": budget is not None and u["total_usd"] >= budget,
    }


@app.get("/api/samples")
async def list_samples() -> list[dict[str, str]]:
    return [
        {"name": name, "title": title, "date": date}
        for name, (title, date) in SAMPLES.items()
        if (SAMPLES_DIR / f"{name}.txt").exists()
    ]


@app.get("/api/samples/{name}", response_class=PlainTextResponse)
async def get_sample(name: str) -> str:
    path = SAMPLES_DIR / f"{name}.txt"
    if name not in SAMPLES or not path.exists():
        raise HTTPException(404, "sample not found")
    return path.read_text(encoding="utf-8")


@app.get("/api/projects")
async def list_projects(request: Request) -> list[dict[str, Any]]:
    return await (await store(request)).projects()


@app.post("/api/projects", status_code=201)
async def create_project(body: NewProject, request: Request) -> dict[str, int]:
    return {"id": await (await store(request)).create_project(body.name, body.client)}


@app.get("/api/projects/{pid}")
async def get_project(pid: int, request: Request) -> dict[str, Any]:
    s = await store(request)
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
    s = await store(request)
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
    builder = (await workspace(request))[1]
    if builder is None:
        raise HTTPException(503, f"building is disabled: {claude.missing()}")
    build_limiter.check(request)
    over_budget((await site_usage(request))["total_usd"], settings.budget_usd)
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
    v = await (await store(request)).version(pid, number)
    if v is None:
        raise HTTPException(404, "version not found")
    return v


@app.post("/api/projects/{pid}/versions/{number}/approve")
async def approve(pid: int, number: int, body: Approval, request: Request) -> dict[str, str]:
    try:
        await (await store(request)).approve(pid, number, body.actor, body.note)
    except StateError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"status": "approved"}


@app.get("/api/projects/{pid}/versions/{number}/export", response_class=PlainTextResponse)
async def export(pid: int, number: int, request: Request, actor: str = "user") -> str:
    """Client-ready proposal as Markdown. 409 unless the version is approved."""
    try:
        return await export_proposal(await store(request), pid, number, actor)
    except StateError as exc:
        status = 404 if "not found" in str(exc) else 409
        raise HTTPException(status, str(exc)) from exc
