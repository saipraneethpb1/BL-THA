"""HTTP API of the Knowledge Brain. Tasks 2 and 3 talk to this, never to the internals."""

import asyncio
import contextlib
import json
import os
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import anthropic
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from kb import session
from kb.answer import escalations as esc
from kb.answer.llm import Answerer, ClaudeAnswerer
from kb.answer.service import answer_question
from kb.bootstrap import article_count, seed_database
from kb.config import settings
from kb.db import connect, init_db
from kb.guard import RateLimiter, over_budget, visitor
from kb.ingestion import health as url_health_rules
from kb.ingestion.sources import (
    HostGuard,
    SitemapCheck,
    SitemapError,
    check_url_syntax,
    validate_sitemap,
)
from kb.ingestion.vision import ClaudeImageDescriber
from kb.progress import Progress, report
from kb.refresh import Refresher, daily_loop, seconds_until
from kb.retrieval.embedder import FastEmbedder
from kb.retrieval.rewrite import ClaudeQueryRewriter
from kb.retrieval.search import Searcher
from kb.speech import Transcriber, WhisperTranscriber

UI_FILE = Path(__file__).parent / "ui" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.seed = await seed_database(settings.db_path, settings.seed_path, settings.seed_url)
    await init_db(settings.db_path)
    if not hasattr(app.state, "searcher"):  # tests inject a searcher with a fake embedder
        rewriter = (
            ClaudeQueryRewriter(settings.rewrite_model)
            if settings.query_rewrite and os.environ.get("ANTHROPIC_API_KEY")
            else None
        )
        app.state.searcher = Searcher(
            FastEmbedder(settings.embedding_model, settings.model_cache_dir), rewriter=rewriter
        )
    if not hasattr(app.state, "answerer"):
        # Without a key the service still searches; only /query is unavailable.
        app.state.answerer = (
            ClaudeAnswerer(
                settings.answer_model, settings.answer_prompt_version, settings.answer_effort
            )
            if os.environ.get("ANTHROPIC_API_KEY")
            else None
        )
    if not hasattr(app.state, "sitemap_validator"):  # tests inject a fake
        guard = HostGuard()
        app.state.sitemap_validator = lambda url, progress=None: validate_sitemap(
            url, guard, progress=progress
        )
    if not hasattr(app.state, "transcriber"):
        app.state.transcriber = (
            WhisperTranscriber(settings.whisper_model, settings.model_cache_dir)
            if settings.speech_enabled
            else None
        )
        if app.state.transcriber is not None and settings.speech_preload:
            app.state.speech_warmup = asyncio.create_task(
                asyncio.to_thread(app.state.transcriber.warm)
            )
    if not hasattr(app.state, "refresher"):
        describer = (
            ClaudeImageDescriber(settings.vision_model)
            if os.environ.get("ANTHROPIC_API_KEY")
            else None
        )
        app.state.refresher = Refresher(settings, app.state.searcher.embedder, describer)
    scheduler = (
        asyncio.create_task(daily_loop(app.state.refresher, settings.refresh_hour_utc))
        if settings.refresh_enabled
        else None
    )
    # Nothing to start from: crawl the help center in the background. The API is
    # usable (and reports progress on /ingestion/status) while it runs.
    if settings.bootstrap_crawl and article_count(settings.db_path) == 0:
        app.state.bootstrap_task = asyncio.create_task(app.state.refresher.run(trigger="bootstrap"))
    yield
    if scheduler is not None:
        scheduler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler


app = FastAPI(
    lifespan=lifespan,
    title="Knowledge Brain",
    version="0.1.0",
    description="Answers product questions from the Brilliant Directories help center, "
    "with citations. Used by the Inbox Agent and the Meeting-to-Scope service.\n\n"
    "All endpoints except `/health` need the header `X-API-Key`.",
)


def claude_error_message(exc: anthropic.APIError) -> str:
    """Anthropic's own one-line reason (e.g. out of credit), not the whole error body."""
    body = exc.body if isinstance(exc.body, dict) else {}
    inner = body.get("error")
    message = inner.get("message") if isinstance(inner, dict) else None
    return str(message or exc.message)


@app.exception_handler(anthropic.APIError)
async def claude_unavailable(request: Request, exc: anthropic.APIError) -> JSONResponse:
    # The model call failed (no credit, overloaded, network). Say so plainly instead of a 500:
    # search, the expert queue and status keep working.
    message = claude_error_message(exc)
    request.app.state.last_claude_error = {"message": message, "at": datetime.now(UTC).isoformat()}
    return JSONResponse(status_code=502, content={"detail": f"Claude API error: {message}"})


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    if settings.api_key is None:  # local development without a key configured
        return
    if x_api_key is None or not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")


Protected = Depends(require_api_key)

# Public demo guards: per-visitor limits on the endpoints that cost money or CPU.
query_limiter = RateLimiter(settings.query_limit_per_10min, 600, "questions")
speech_limiter = RateLimiter(settings.transcribe_limit_per_10min, 600, "voice recordings")
refresh_limiter = RateLimiter(settings.refresh_limit_per_hour, 3600, "refreshes")
sitemap_limiter = RateLimiter(10, 600, "sitemap checks")


async def spent_usd() -> float:
    async with connect(settings.db_path) as db:
        rows = list(
            await db.execute_fetchall("SELECT coalesce(sum(cost_usd), 0) AS s FROM query_log")
        )
    return float(rows[0]["s"])


class Health(BaseModel):
    status: Literal["ok"]
    articles: int


class IngestionStatus(BaseModel):
    pages_by_status: dict[str, int]
    last_run: dict[str, str | int | None] | None = Field(description="Last crawl run")
    chunks: int
    images_total: int
    images_described: int
    images_failed: int
    open_escalations: int
    answering_enabled: bool
    refresh_running: bool
    last_refresh: dict[str, Any] | None
    seeded_from: str
    next_scheduled_refresh: str | None


class SearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=2000, description="Free-text question or keywords")
    k: int = Field(default=6, ge=1, le=20, description="Number of passages to return")
    mode: Literal["hybrid", "vector", "keyword"] = "hybrid"


class Passage(BaseModel):
    article_id: str
    title: str
    url: str | None
    section: str | None
    text: str
    image_ids: list[str]
    score: float = Field(description="Cosine similarity 0..1 (hybrid/vector); higher is closer")


class SearchResponse(BaseModel):
    passages: list[Passage]
    took_ms: float


@app.get("/health", response_model=Health)
async def health() -> Health:
    async with connect(settings.db_path) as db:
        rows = list(await db.execute_fetchall("SELECT count(*) AS n FROM articles"))
    return Health(status="ok", articles=rows[0]["n"])


@app.post("/transcribe", dependencies=[Protected])
async def transcribe(request: Request, language: str | None = None) -> dict[str, Any]:
    """Speech to text for the mic buttons. Send the recorded audio as the raw request body
    (webm/ogg/mp4/wav). Runs a local Whisper model: nothing leaves the server."""
    transcriber: Transcriber | None = request.app.state.transcriber
    if transcriber is None:
        raise HTTPException(503, "speech to text is disabled (KB_SPEECH_ENABLED=false)")
    speech_limiter.check(request)
    if language is not None and not (len(language) == 2 and language.isalpha()):
        raise HTTPException(422, "language must be a two-letter code such as en or hi")
    audio = await request.body()
    if not audio:
        raise HTTPException(422, "no audio received")
    if len(audio) > settings.max_audio_bytes:
        raise HTTPException(413, "recording too long: keep it under about a minute")
    started = time.perf_counter()
    try:
        text, detected = await asyncio.to_thread(transcriber.transcribe, audio, language)
    except Exception as exc:  # undecodable or corrupt audio
        raise HTTPException(422, f"could not read the recording ({type(exc).__name__})") from exc
    return {"text": text, "language": detected, "seconds": round(time.perf_counter() - started, 2)}


@app.get("/usage", dependencies=[Protected])
async def usage(request: Request) -> dict[str, Any]:
    """Claude spend recorded by this service (token counts at list prices), for the usage bar.
    `by_caller` lets the bar avoid double counting: the Inbox Agent already includes the cost
    of its lookups in its own figure."""
    async with connect(settings.db_path) as db:
        rows = await db.execute_fetchall(
            """SELECT coalesce(caller, '') AS caller, count(*) AS calls,
                      coalesce(sum(cost_usd), 0) AS usd,
                      coalesce(sum(CASE WHEN date(created_at) = date('now') THEN cost_usd END), 0)
                          AS today,
                      max(CASE WHEN cost_usd > 0 THEN created_at END) AS last_ok
               FROM query_log GROUP BY coalesce(caller, '')"""
        )
    by_caller = {
        r["caller"] or "direct": {"usd": r["usd"], "today_usd": r["today"], "calls": r["calls"]}
        for r in rows
    }
    last_ok = max((r["last_ok"] for r in rows if r["last_ok"]), default=None)
    return {
        "service": "kb",
        "total_usd": round(sum(r["usd"] for r in rows), 6),
        "today_usd": round(sum(r["today"] for r in rows), 6),
        "calls": sum(r["calls"] for r in rows),
        "by_caller": by_caller,
        "last_success_at": f"{last_ok.replace(' ', 'T')}+00:00" if last_ok else None,
        "last_error": getattr(request.app.state, "last_claude_error", None),
        "budget_usd": settings.budget_usd,
        "over_budget": settings.budget_usd is not None
        and sum(r["usd"] for r in rows) >= settings.budget_usd,
    }


class LoginIn(BaseModel):
    account: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=200)


login_limiter = RateLimiter(10, 600, "sign-in attempts")


def signed_in(request: Request) -> str | None:
    return session.account_from_token(request.cookies.get(session.COOKIE))


def require_signed_in(request: Request) -> None:
    """Changing shared Knowledge Brain settings needs a demo account when it comes through the
    public site. Internal calls (the other services, tests, local use) have no visitor address."""
    if visitor(request) is not None and signed_in(request) is None:
        raise HTTPException(401, "Sign in with a demo account to change this.")


SignedIn = Depends(require_signed_in)


@app.get("/auth/me")
async def auth_me(request: Request) -> dict[str, Any]:
    """Who is signed in, plus the demo accounts for the sign-in screen (their passwords are
    deliberately public: they separate demo workspaces, they don't protect secrets)."""
    account = signed_in(request)
    return {
        "account": account,
        "workspace": account or session.GUEST,
        "demo_accounts": [{"account": a, "password": p} for a, p in session.DEMO_ACCOUNTS.items()],
    }


@app.post("/auth/login")
async def auth_login(body: LoginIn, request: Request) -> JSONResponse:
    login_limiter.check(request)
    account = body.account.strip().lower()
    if not session.check_password(account, body.password):
        raise HTTPException(401, "That demo account and password don't match.")
    resp = JSONResponse({"account": account})
    resp.set_cookie(
        session.COOKIE,
        session.make_token(account),
        max_age=session.MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=request.headers.get("x-forwarded-proto") == "https",
        domain=session.cookie_domain(request.headers.get("host")),
        path="/",
    )
    return resp


@app.post("/auth/logout")
async def auth_logout(request: Request) -> JSONResponse:
    resp = JSONResponse({"account": None})
    domain = session.cookie_domain(request.headers.get("host"))
    resp.delete_cookie(session.COOKIE, domain=domain, path="/")
    resp.delete_cookie("bl_who", domain=domain, path="/")  # the Gmail address shown in the bar
    return resp


class SitemapsIn(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=10, description="One or more sitemap URLs")


@app.get("/sitemaps", dependencies=[Protected])
async def list_sitemaps() -> dict[str, Any]:
    """The help center's own sitemap plus any custom ones, with how many pages each holds."""
    async with connect(settings.db_path) as db:
        rows = await db.execute_fetchall(
            """SELECT s.*, (SELECT count(*) FROM pages p WHERE p.source = s.url
                              AND p.status = 'done') AS pages_indexed
               FROM custom_sitemaps s ORDER BY s.id"""
        )
        builtin = list(
            await db.execute_fetchall(
                "SELECT count(*) AS n FROM pages WHERE source = 'help-center' AND status = 'done'"
            )
        )
    return {
        "builtin": {"url": settings.sitemap_url, "pages_indexed": builtin[0]["n"]},
        "custom": [dict(r) for r in rows],
        "max_custom": settings.max_custom_sitemaps,
    }


def _event_stream(work: Callable[[Progress], Awaitable[dict[str, Any]]]) -> StreamingResponse:
    """Runs `work` and sends each step it reports as a server-sent event, then its result
    (`{"done": ...}`) or what went wrong (`{"error": ..., "status": ...}`). The page shows
    the steps live, so a slow request visibly does something."""

    async def events() -> AsyncIterator[str]:
        started = time.perf_counter()
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def progress(step: dict[str, Any]) -> None:
            await queue.put(step)

        async def run() -> None:
            try:
                await queue.put({"done": await work(progress)})
            except anthropic.APIError as exc:
                await queue.put(
                    {"error": f"Claude API error: {claude_error_message(exc)}", "status": 502}
                )
            except HTTPException as exc:
                await queue.put({"error": str(exc.detail), "status": exc.status_code})
            except Exception as exc:  # reported to the page, which shows it
                await queue.put({"error": str(exc) or type(exc).__name__, "status": 500})
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        try:
            while (item := await queue.get()) is not None:
                item["t_ms"] = round((time.perf_counter() - started) * 1000)
                yield f"data: {json.dumps(item)}\n\n"
            await task
        finally:
            # Closing the request must not leave paid work running in the background.
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # No buffering anywhere on the way, so each step reaches the page as it happens.
    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _add_sitemaps(
    urls: list[str], request: Request, progress: Progress | None = None
) -> dict[str, Any]:
    validate: Callable[..., Awaitable[SitemapCheck]] = request.app.state.sitemap_validator
    results: list[dict[str, Any]] = []
    async with connect(settings.db_path) as db:
        existing = {r["url"] for r in await db.execute_fetchall("SELECT url FROM custom_sitemaps")}
        for i, raw in enumerate(urls):
            result: dict[str, Any] = {"input": raw, "ok": False}
            results.append(result)

            async def step(event: dict[str, Any], i: int = i, raw: str = raw) -> None:
                if progress is not None:
                    await progress({**event, "group": i, "group_label": raw})

            try:
                url = check_url_syntax(raw)
                await report(step, "check", "The address is a valid URL")
                if url == settings.sitemap_url:
                    raise SitemapError(
                        "That's the help center's own sitemap: it's always included."
                    )
                if url in existing:
                    raise SitemapError("That sitemap is already added.")
                if len(existing) >= settings.max_custom_sitemaps:
                    raise SitemapError(
                        f"This demo allows {settings.max_custom_sitemaps} custom sitemaps; remove one first."
                    )
                check = await (validate(url, progress=step) if progress else validate(url))
            except SitemapError as exc:
                result["error"] = str(exc)
                await report(step, "warn", "Not added", str(exc))
                continue
            cur = await db.execute(
                """INSERT INTO custom_sitemaps(url, kind, url_count, added_by, last_checked_at)
                   VALUES (?, ?, ?, ?, datetime('now'))""",
                (check.url, check.kind, check.url_count, visitor(request) or "api"),
            )
            existing.add(check.url)
            result.update(
                ok=True, id=cur.lastrowid, url=check.url, kind=check.kind, url_count=check.url_count
            )
            what = "child sitemaps" if check.kind == "sitemapindex" else "pages"
            await report(
                step,
                "check",
                f"Added: {check.url_count} {what} found",
                "their text is fetched at the next refresh",
            )
        await db.commit()
    return {"results": results}


@app.post("/sitemaps", dependencies=[Protected, SignedIn])
async def add_sitemaps(body: SitemapsIn, request: Request) -> dict[str, Any]:
    """Validate each URL (format, public host, reachable, a real sitemap) and add the good
    ones. Each URL gets its own result, so one bad line doesn't block the rest. Their pages
    are crawled at the next refresh."""
    sitemap_limiter.check(request)
    return await _add_sitemaps(body.urls, request)


@app.post("/sitemaps/stream", dependencies=[Protected, SignedIn])
async def add_sitemaps_stream(body: SitemapsIn, request: Request) -> StreamingResponse:
    """Same as POST /sitemaps, reporting each check as it happens (server-sent events)."""
    sitemap_limiter.check(request)
    return _event_stream(lambda progress: _add_sitemaps(body.urls, request, progress))


@app.delete("/sitemaps/{sitemap_id}", dependencies=[Protected, SignedIn])
async def remove_sitemap(sitemap_id: int) -> dict[str, Any]:
    """Stop using a custom sitemap. Its pages are quarantined at the next refresh: kept, but
    no longer searched (nothing is deleted)."""
    async with connect(settings.db_path) as db:
        rows = list(
            await db.execute_fetchall("SELECT url FROM custom_sitemaps WHERE id = ?", (sitemap_id,))
        )
        if not rows:
            raise HTTPException(404, "no such custom sitemap")
        await db.execute("DELETE FROM custom_sitemaps WHERE id = ?", (sitemap_id,))
        await db.commit()
    return {"removed": rows[0]["url"]}


def _utc(t: str | None) -> str | None:
    """SQLite 'YYYY-MM-DD HH:MM:SS' (UTC) to ISO 8601, so browsers show local time."""
    if not t:
        return None
    return t if t.endswith("Z") else t.replace(" ", "T") + "Z"


@app.get("/ingestion/runs", dependencies=[Protected])
async def refresh_runs(limit: int = 10) -> list[dict[str, Any]]:
    """Recent refresh runs, newest first, each with its first and last fetch time and how many
    URLs it fetched (from the fetch log; runs from before the log existed show none)."""
    async with connect(settings.db_path) as db:
        rows = await db.execute_fetchall(
            """SELECT r.*,
                      (SELECT min(fetched_at) FROM fetch_log f WHERE f.run_id = r.id) AS first_fetch_at,
                      (SELECT max(fetched_at) FROM fetch_log f WHERE f.run_id = r.id) AS last_fetch_at,
                      (SELECT count(*) FROM fetch_log f WHERE f.run_id = r.id) AS fetched_urls
               FROM ingestion_runs r ORDER BY r.id DESC LIMIT ?""",
            (max(1, min(limit, 50)),),
        )
        outcomes = await db.execute_fetchall(
            """SELECT run_id, outcome, count(*) AS n FROM fetch_log
               WHERE run_id IN (SELECT id FROM ingestion_runs ORDER BY id DESC LIMIT ?)
               GROUP BY run_id, outcome""",
            (max(1, min(limit, 50)),),
        )
    by_run: dict[int, dict[str, int]] = {}
    for o in outcomes:
        by_run.setdefault(o["run_id"], {})[o["outcome"]] = o["n"]
    return [
        {
            "id": r["id"],
            "status": r["status"],
            "started_at": _utc(r["started_at"]),
            "finished_at": _utc(r["finished_at"]),
            "first_fetch_at": _utc(r["first_fetch_at"]),
            "last_fetch_at": _utc(r["last_fetch_at"]),
            "fetched_urls": r["fetched_urls"],
            "outcomes": by_run.get(r["id"], {}),
            "new": r["new"],
            "changed": r["changed"],
            "quarantined": r["removed"],
            "error": r["error"],
        }
        for r in rows
    ]


@app.get("/ingestion/runs/{run_id}/urls", dependencies=[Protected])
async def refresh_run_urls(run_id: int) -> dict[str, Any]:
    """Every URL one refresh run fetched, in the order it fetched them."""
    async with connect(settings.db_path) as db:
        run = list(
            await db.execute_fetchall("SELECT id FROM ingestion_runs WHERE id = ?", (run_id,))
        )
        if not run:
            raise HTTPException(404, "no such refresh run")
        rows = await db.execute_fetchall(
            """SELECT url, source, outcome, detail, fetched_at FROM fetch_log
               WHERE run_id = ? ORDER BY fetched_at, id LIMIT 20000""",
            (run_id,),
        )
    return {
        "run_id": run_id,
        "urls": [{**dict(r), "fetched_at": _utc(r["fetched_at"])} for r in rows],
    }


class HealthSettingsIn(BaseModel):
    threshold: int = Field(
        ge=1, le=30, description="Consecutive refreshes before a non-404 URL issue needs a decision"
    )
    action_gone: Literal["quarantine", "flag"] = Field(
        description="410 and no longer in its sitemap; HTTP 404 always quarantines automatically"
    )
    action_moved: Literal["quarantine", "flag"] = Field(description="301/308 permanent redirect")


class HealthActionIn(BaseModel):
    action: Literal["quarantine", "keep", "restore"]
    article_ids: list[str] = Field(min_length=1, max_length=500)


@app.get("/url-health", dependencies=[Protected])
async def url_health() -> dict[str, Any]:
    """Settings, every URL being counted (with its count), and every quarantined URL."""
    async with connect(settings.db_path) as db:
        hs = await url_health_rules.get_settings(db)
        items = await db.execute_fetchall(
            """SELECT h.*, a.title FROM url_health h LEFT JOIN articles a USING (article_id)
               WHERE h.state != 'quarantined'
               ORDER BY CASE h.state WHEN 'pending' THEN 0 WHEN 'flagged' THEN 1 ELSE 2 END,
                        h.count DESC, h.url"""
        )
        quarantined = await db.execute_fetchall(
            """SELECT p.article_id, p.url, p.last_error AS reason, p.quarantined_at, a.title
               FROM pages p LEFT JOIN articles a USING (article_id)
               WHERE p.status = 'quarantined' ORDER BY p.quarantined_at DESC, p.url"""
        )
    return {
        "settings": {
            "threshold": hs.threshold,
            "action_gone": hs.action_gone,
            "action_moved": hs.action_moved,
        },
        "issues": url_health_rules.ISSUES,
        "items": [
            {
                **dict(r),
                "first_seen_at": _utc(r["first_seen_at"]),
                "last_seen_at": _utc(r["last_seen_at"]),
            }
            for r in items
        ],
        "quarantined": [
            {**dict(r), "quarantined_at": _utc(r["quarantined_at"])} for r in quarantined
        ],
    }


@app.put("/url-health/settings", dependencies=[Protected, SignedIn])
async def save_url_health_settings(body: HealthSettingsIn) -> dict[str, Any]:
    """Change the threshold or the rules. Open URLs are re-sorted at once (pending, flagged or
    counting); nothing is quarantined by a settings change."""
    async with connect(settings.db_path) as db:
        await url_health_rules.save_settings(
            db, url_health_rules.HealthSettings(**body.model_dump())
        )
        await db.commit()
    return {"saved": body.model_dump()}


@app.post("/url-health/actions", dependencies=[Protected, SignedIn])
async def url_health_action(body: HealthActionIn) -> dict[str, Any]:
    """A person's decision: quarantine (kept, out of search), keep (reset the count), or
    restore a quarantined URL (fetched again at the next refresh)."""
    do = {
        "quarantine": url_health_rules.quarantine,
        "keep": url_health_rules.keep,
        "restore": url_health_rules.restore,
    }[body.action]
    async with connect(settings.db_path) as db:
        done = await do(db, body.article_ids)
        await db.commit()
    return {"action": body.action, "done": done}


@app.get("/ingestion/status", response_model=IngestionStatus, dependencies=[Protected])
async def ingestion_status(request: Request) -> IngestionStatus:
    """Crawl progress, index size, screenshot coverage and the refresh schedule."""
    async with connect(settings.db_path) as db:
        counts = await db.execute_fetchall(
            "SELECT status, count(*) AS n FROM pages GROUP BY status"
        )
        last = list(
            await db.execute_fetchall("SELECT * FROM ingestion_runs ORDER BY id DESC LIMIT 1")
        )
        [stats] = list(
            await db.execute_fetchall(
                """SELECT (SELECT count(*) FROM chunks) AS chunks,
                          (SELECT count(*) FROM images) AS images_total,
                          (SELECT count(*) FROM images WHERE described_with IS NOT NULL
                             AND described_with NOT LIKE 'failed:%') AS images_described,
                          (SELECT count(*) FROM images WHERE described_with LIKE 'failed:%')
                             AS images_failed,
                          (SELECT count(*) FROM escalations WHERE status='open') AS open_esc"""
            )
        )
    refresher: Refresher = request.app.state.refresher
    next_run = None
    if settings.refresh_enabled:
        next_run = (
            datetime.now(UTC) + timedelta(seconds=seconds_until(settings.refresh_hour_utc))
        ).isoformat(timespec="minutes")
    return IngestionStatus(
        pages_by_status={r["status"]: r["n"] for r in counts},
        last_run=dict(last[0]) if last else None,
        chunks=stats["chunks"],
        images_total=stats["images_total"],
        images_described=stats["images_described"],
        images_failed=stats["images_failed"],
        open_escalations=stats["open_esc"],
        answering_enabled=request.app.state.answerer is not None,
        refresh_running=refresher.running,
        last_refresh=asdict(refresher.last) if refresher.last else None,
        next_scheduled_refresh=next_run,
        seeded_from=getattr(request.app.state, "seed", "unknown"),
    )


@app.post("/ingestion/run", status_code=202, dependencies=[Protected, SignedIn])
async def run_refresh(request: Request) -> dict[str, str]:
    """Start a refresh now (crawl changes, describe new screenshots, re-index).
    Returns at once; poll `/ingestion/status` for progress."""
    refresh_limiter.check(request)
    refresher: Refresher = request.app.state.refresher
    if refresher.running:
        raise HTTPException(409, "a refresh is already running")
    request.app.state.refresh_task = asyncio.create_task(refresher.run(trigger="manual"))
    return {"status": "started"}


@app.get("/", include_in_schema=False)
async def ui() -> FileResponse:
    return FileResponse(UI_FILE)


@app.post("/search", response_model=SearchResponse, dependencies=[Protected])
async def search(req: SearchRequest, request: Request) -> SearchResponse:
    """Retrieval only: the passages most relevant to the query, no generated answer.
    Useful for agents that want raw evidence (e.g. the Scope feasibility check)."""
    searcher: Searcher = request.app.state.searcher
    t = time.perf_counter()
    async with connect(settings.db_path) as db:
        hits = await searcher.search(db, req.query, k=req.k, mode=req.mode)
    return SearchResponse(
        passages=[
            Passage(
                article_id=h.article_id,
                title=h.title,
                url=h.url,
                section=h.section,
                text=h.text,
                image_ids=h.image_ids,
                score=round(h.score, 4),
            )
            for h in hits
        ],
        took_ms=round((time.perf_counter() - t) * 1000, 1),
    )


# ---------- Answering ----------


class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    caller: str | None = Field(
        default=None, max_length=50, description="Who is asking, e.g. 'inbox-agent'. For audits."
    )


class SourceOut(BaseModel):
    title: str
    url: str | None
    source_type: Literal["article", "expert"]
    section: str | None
    score: float
    image_urls: list[str]


class QueryResponse(BaseModel):
    status: Literal["answered", "partial", "insufficient_evidence"] = Field(
        description="answered: fully supported by sources. partial: some of it is. "
        "insufficient_evidence: the help center does not cover it; a human task was opened."
    )
    answer: str
    sources: list[SourceOut] = Field(description="Only sources the answer actually cites")
    needs_human: bool
    escalation_id: int | None
    missing_info: str | None
    model: str | None
    prompt_version: str | None
    top_score: float = Field(description="Similarity of the best passage found (0..1)")
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float


@app.post("/query", response_model=QueryResponse, dependencies=[Protected])
async def query(req: QueryRequest, request: Request) -> QueryResponse:
    """Answer a product question from the help center, with citations.
    Never invents an answer: unsupported questions come back as
    `insufficient_evidence` and open a task for a human expert."""
    answerer: Answerer | None = request.app.state.answerer
    if answerer is None:
        raise HTTPException(503, "answering is disabled: ANTHROPIC_API_KEY is not configured")
    query_limiter.check(request)
    over_budget(await spent_usd(), settings.budget_usd)
    return await _answer(req, request, answerer)


async def _answer(
    req: QueryRequest, request: Request, answerer: Answerer, progress: Progress | None = None
) -> QueryResponse:
    async with connect(settings.db_path) as db:
        result = await answer_question(
            db,
            request.app.state.searcher,
            answerer,
            req.question,
            min_score=settings.min_evidence_score,
            caller=req.caller,
            progress=progress,
        )
    return QueryResponse.model_validate(asdict(result))


@app.post("/query/stream", dependencies=[Protected])
async def query_stream(req: QueryRequest, request: Request) -> StreamingResponse:
    """Same as POST /query, reporting each step as it happens (server-sent events): the
    rephrasings, the searches, what was found, the evidence check, writing, and the
    citation check. The last event carries the answer."""
    answerer: Answerer | None = request.app.state.answerer
    if answerer is None:
        raise HTTPException(503, "answering is disabled: ANTHROPIC_API_KEY is not configured")
    query_limiter.check(request)
    over_budget(await spent_usd(), settings.budget_usd)

    async def work(progress: Progress) -> dict[str, Any]:
        return (await _answer(req, request, answerer, progress)).model_dump()

    return _event_stream(work)


# ---------- Human experts ----------


class EscalationOut(BaseModel):
    id: int
    question: str
    status: str
    reason: str
    evidence: list[dict[str, object]]
    caller: str | None
    times_asked: int
    created_at: str
    resolved_at: str | None


class ResolveRequest(BaseModel):
    answer: str = Field(min_length=10, max_length=10_000)
    author: str = Field(min_length=2, max_length=100)


class ExpertAnswerOut(BaseModel):
    expert_answer_id: int
    escalation_id: int


@app.get("/escalations", response_model=list[EscalationOut], dependencies=[Protected])
async def list_escalations(
    status: Literal["open", "resolved", "dismissed"] | None = "open",
) -> list[EscalationOut]:
    """Questions waiting for an expert, most-asked first."""
    async with connect(settings.db_path) as db:
        items = await esc.list_escalations(db, status)
    return [EscalationOut.model_validate(asdict(e)) for e in items]


@app.post(
    "/escalations/{escalation_id}/resolve",
    response_model=ExpertAnswerOut,
    dependencies=[Protected],
)
async def resolve_escalation(
    escalation_id: int, req: ResolveRequest, request: Request
) -> ExpertAnswerOut:
    """Record an expert's answer. It becomes searchable immediately and is cited as
    an 'expert' source in later answers."""
    async with connect(settings.db_path) as db:
        item = await esc.get_escalation(db, escalation_id)
        if item is None:
            raise HTTPException(404, "escalation not found")
        if item.status != "open":
            raise HTTPException(409, f"escalation is already {item.status}")
        answer_id = await esc.add_expert_answer(
            db,
            request.app.state.searcher.embedder,
            item.question,
            req.answer,
            req.author,
            escalation_id,
        )
    return ExpertAnswerOut(expert_answer_id=answer_id, escalation_id=escalation_id)


@app.post("/expert-answers/{answer_id}/retract", dependencies=[Protected])
async def retract_expert_answer(answer_id: int) -> dict[str, bool]:
    """Undo an expert answer: it stops being retrieved, the record stays for audit."""
    async with connect(settings.db_path) as db:
        if not await esc.retract_expert_answer(db, answer_id):
            raise HTTPException(404, "no active expert answer with that id")
    return {"retracted": True}
