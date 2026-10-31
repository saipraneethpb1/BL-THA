"""HTTP API of the Knowledge Brain. Tasks 2 and 3 talk to this, never to the internals."""

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import anthropic
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from kb import claude, session
from kb.answer import escalations as esc
from kb.answer.llm import Answerer, ClaudeAnswerer
from kb.answer.local import OllamaAnswerer, OllamaQueryRewriter
from kb.answer.service import answer_question, with_context
from kb.bootstrap import article_count, seed_database
from kb.config import settings
from kb.db import connect, init_db
from kb.guard import RateLimiter, over_budget, visitor
from kb.ingestion import catalog
from kb.ingestion import health as url_health_rules
from kb.ingestion.pipeline import _known_urls
from kb.ingestion.sources import (
    HostGuard,
    SitemapCheck,
    SitemapError,
    check_url_syntax,
    guarded_client,
    normalize_url,
    validate_sitemap,
    walk_sitemap,
)
from kb.ingestion.vision import ClaudeImageDescriber
from kb.progress import Progress, report
from kb.refresh import Refresher, daily_loop, seconds_until
from kb.retrieval.embedder import make_embedder
from kb.retrieval.rerank import CrossEncoderReranker
from kb.retrieval.rewrite import ClaudeQueryRewriter
from kb.retrieval.search import Searcher
from kb.speech import Transcriber, WhisperTranscriber
from kb.tuning import registry as prompt_registry
from kb.tuning import robustness
from kb.tuning import writer as prompt_tuning

UI_FILE = Path(__file__).parent / "ui" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.seed = await seed_database(settings.db_path, settings.seed_path, settings.seed_url)
    await init_db(settings.db_path)
    if not hasattr(app.state, "searcher"):  # tests inject a searcher with a fake embedder
        rewriter: ClaudeQueryRewriter | OllamaQueryRewriter | None = None
        if settings.query_rewrite and settings.rewrite_model.startswith("ollama:"):
            rewriter = OllamaQueryRewriter(settings.rewrite_model.removeprefix("ollama:"))
        elif settings.query_rewrite and claude.configured():
            rewriter = ClaudeQueryRewriter(settings.rewrite_model)
        app.state.searcher = Searcher(
            make_embedder(settings.embedding_model, settings.model_cache_dir),
            rewriter=rewriter,
            reranker=(
                CrossEncoderReranker(settings.reranker_model, settings.model_cache_dir)
                if settings.reranker_model
                else None
            ),
            rerank_chars=settings.rerank_chars,
            rewrite_skip_above=settings.rewrite_skip_above,
        )
    # The rewrite prompt is data the agent maintains: start with its active version.
    async with connect(settings.db_path) as db:
        await app.state.searcher.vectors.refresh(db)
        await prompt_registry.ensure_seeded(db, prompt_tuning.NAME, "rewrite_v1")
        live = await prompt_registry.active(db, prompt_tuning.NAME)
    rw = getattr(app.state.searcher, "rewriter", None)
    if isinstance(rw, ClaudeQueryRewriter | OllamaQueryRewriter):
        rw.use_prompt(live["text"], f"rewrite v{live['version']}")
    if not hasattr(app.state, "prompt_writer"):
        app.state.prompt_writer = (
            prompt_tuning.ClaudePromptWriter(settings.prompt_writer_model)
            if claude.configured()
            else None
        )
    app.state.tuning = asyncio.Lock()
    if not hasattr(app.state, "answerer"):
        # A local model ("ollama:<name>") needs no key. With Claude and no key the service
        # still searches; only /query is unavailable.
        answerer: Answerer | None = None
        if settings.answer_model.startswith("ollama:"):
            answerer = OllamaAnswerer(
                settings.answer_model.removeprefix("ollama:"), settings.answer_prompt_version
            )
        elif claude.configured():
            answerer = ClaudeAnswerer(
                settings.answer_model, settings.answer_prompt_version, settings.answer_effort
            )
        app.state.answerer = answerer
    if not hasattr(app.state, "sitemap_client"):  # tests inject a fake site
        app.state.sitemap_client = lambda: guarded_client(HostGuard(), timeout=15.0)
    app.state.previews = {}  # sitemap URL -> its last preview, saved with it when confirmed
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
        describer = ClaudeImageDescriber(settings.vision_model) if claude.configured() else None
        app.state.refresher = Refresher(settings, app.state.searcher.embedder, describer)
    scheduler = (
        asyncio.create_task(daily_loop(app.state.refresher, settings.refresh_hour_utc))
        if settings.refresh_enabled
        else None
    )
    # The agent re-checks its own rewrite prompt every few days and improves it if it can.
    maintainer = (
        asyncio.create_task(prompt_maintenance_loop(app))
        if settings.refresh_enabled and settings.prompt_tune_days > 0
        else None
    )
    # Nothing to start from: crawl the help center in the background. The API is
    # usable (and reports progress on /ingestion/status) while it runs.
    if settings.bootstrap_crawl and article_count(settings.db_path) == 0:
        app.state.bootstrap_task = asyncio.create_task(app.state.refresher.run(trigger="bootstrap"))
    yield
    for task in (scheduler, maintainer):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


log = logging.getLogger(__name__)

app = FastAPI(
    lifespan=lifespan,
    title="Knowledge Brain",
    version="0.1.0",
    description="Answers product questions from the Brilliant Directories help center, "
    "with citations. Used by the Inbox Agent and the Meeting-to-Scope service.\n\n"
    "All endpoints except `/health` need the header `X-API-Key`.",
)


def claude_error_message(exc: anthropic.APIError) -> str:
    """The provider's own one-line reason (e.g. out of credit, no model access), not the
    whole error body."""
    body = exc.body if isinstance(exc.body, dict) else {}
    inner = body.get("error")
    message = inner.get("message") if isinstance(inner, dict) else None
    # Bedrock puts the reason at the top level: {"message": "You don't have access to ..."}.
    return str(message or body.get("message") or exc.message)


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
    counts: dict[str, int] = Field(
        description="Pages: total, embedded, pending_embed, pending_fetch, duplicates, "
        "listed_twice, archived, failed"
    )
    current_run: dict[str, Any] | None = Field(
        default=None, description="The refresh running now: pages planned and done"
    )
    pages_by_status: dict[str, int]
    last_run: dict[str, str | int | None] | None = Field(description="Last crawl run")
    chunks: int
    images_total: int
    images_described: int
    images_failed: int
    open_escalations: int
    answering_enabled: bool
    refresh_running: bool
    refresh_stopping: bool = Field(
        False, description="Stop was pressed; finishing the pages in progress, then indexing"
    )
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


def chat_owner(request: Request) -> str | None:
    """Whose chat history this is: the signed-in account, else the guest's browser (a
    random id it keeps and sends as X-Chat-Id). None: nothing is kept."""
    account = signed_in(request)
    if account:
        return f"acct:{account}"
    anon = request.headers.get("x-chat-id", "")
    return (
        f"anon:{anon}" if len(anon) == 32 and all(c in "0123456789abcdef" for c in anon) else None
    )


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


RecheckDays = Literal[1, 7, 15, 30]


class SitemapsIn(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=10, description="One or more sitemap URLs")
    recheck_days: RecheckDays = Field(
        default=1, description="Re-check every 1 (daily), 7, 15 or 30 days"
    )
    ingest: bool = Field(
        default=False, description="Crawl the new pages now instead of at the next check"
    )


class PreviewIn(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=10)


class ScheduleIn(BaseModel):
    recheck_days: RecheckDays


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"about {max(1, round(seconds))} s"
    if seconds < 5400:
        return f"about {round(seconds / 60)} min"
    return f"about {seconds / 3600:.1f} h"


@app.get("/sitemaps", dependencies=[Protected])
async def list_sitemaps() -> dict[str, Any]:
    """The help center's own sitemap plus any custom ones, with how many pages each holds."""
    async with connect(settings.db_path) as db:
        rows = await db.execute_fetchall(
            """SELECT s.*, (SELECT count(*) FROM pages p WHERE p.source = s.url
                              AND p.status = 'done') AS pages_indexed,
                      (SELECT count(*) FROM page_listings l WHERE l.source = s.url
                         AND (SELECT count(*) FROM page_listings x WHERE x.article_id = l.article_id) > 1
                      ) AS listed_elsewhere
               FROM custom_sitemaps s ORDER BY s.id"""
        )
        builtin = list(
            await db.execute_fetchall(
                "SELECT count(*) AS n FROM pages WHERE source = 'help-center' AND status = 'done'"
            )
        )
        hc_days, hc_last = await catalog.helpcenter_schedule(db, settings)
    return {
        "builtin": {
            "url": settings.sitemap_url,
            "pages_indexed": builtin[0]["n"],
            "recheck_days": hc_days,
            "last_checked_at": _utc(hc_last),
            "next_check_at": catalog.next_check(hc_last, hc_days),
        },
        "custom": [
            {
                **{k: r[k] for k in r.keys() if k != "summary"},
                "summary": json.loads(r["summary"]) if r["summary"] else None,
                "last_checked_at": _utc(r["last_checked_at"]),
                "next_check_at": catalog.next_check(r["last_checked_at"], r["recheck_days"]),
            }
            for r in rows
        ],
        "max_custom": settings.max_custom_sitemaps,
        "recheck_choices": catalog.RECHECK_CHOICES,
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


async def _preview(urls: list[str], request: Request, progress: Progress) -> dict[str, Any]:
    """Read each sitemap and every sitemap nested in it, without storing anything: what
    would be ingested, how many pages are new, and roughly how long it would take."""
    previews: list[dict[str, Any]] = []
    async with connect(settings.db_path) as db:
        existing = {r["url"] for r in await db.execute_fetchall("SELECT url FROM custom_sitemaps")}
        known = await _known_urls(db)
    for i, raw in enumerate(urls):
        out: dict[str, Any] = {"input": raw, "ok": False}
        previews.append(out)

        async def step(event: dict[str, Any], i: int = i, raw: str = raw) -> None:
            await progress({**event, "group": i, "group_label": raw})

        try:
            url = check_url_syntax(raw)
            if url == settings.sitemap_url:
                raise SitemapError("That's the help center's own sitemap: it's always included.")
            if url in existing:
                raise SitemapError("That sitemap is already added.")
            host = urlsplit(url).hostname or ""
            await report(step, "shield", f"Checking that {host} is a public site")
            async with request.app.state.sitemap_client() as client:
                await report(step, "globe", "Reading the sitemap", url)
                tree = await walk_sitemap(
                    client,
                    url,
                    max_pages=settings.custom_sitemap_max_pages,
                    max_children=settings.custom_sitemap_max_children,
                    progress=step,
                )
        except SitemapError as exc:
            out["error"] = str(exc)
            await report(step, "warn", "Can't be added", str(exc))
            continue
        keys = [normalize_url(loc) for loc, _ in tree.locs]
        already = sum(1 for k in keys if k in known)
        new = len(keys) - already
        estimate = catalog.estimate_seconds(new, settings)
        out.update(
            ok=True,
            url=url,
            kind=tree.kind,
            total=len(keys),
            new=new,
            already_stored=already,
            listed_twice=tree.listed_twice,
            first=tree.locs[0][0] if tree.locs else None,
            last=tree.locs[-1][0] if tree.locs else None,
            children=[vars(c) for c in tree.children],
            failed_children=sum(1 for c in tree.children if c.error),
            truncated=tree.truncated,
            estimate_s=estimate,
            estimate=_duration(estimate),
        )
        request.app.state.previews[url] = {
            k: out[k]
            for k in (
                "total",
                "new",
                "already_stored",
                "listed_twice",
                "first",
                "last",
                "children",
                "truncated",
            )
        }
        nested = f" in {len(tree.children)} nested sitemaps" if tree.children else ""
        await report(
            step,
            "check",
            f"{len(keys):,} pages{nested}: {new:,} new, {already:,} already stored",
            f"ingesting them takes {_duration(estimate)}"
            + (
                f" · {tree.listed_twice:,} listed twice (counted once)" if tree.listed_twice else ""
            ),
        )
    return {"previews": previews}


@app.post("/sitemaps/preview/stream", dependencies=[Protected])
async def preview_sitemaps(body: PreviewIn, request: Request) -> StreamingResponse:
    """Before anything is ingested: every nested sitemap with its page count and first and
    last URL, the totals, what's new, and a time estimate. Nothing is stored."""
    sitemap_limiter.check(request)
    return _event_stream(lambda progress: _preview(body.urls, request, progress))


async def _add_sitemaps(
    urls: list[str],
    request: Request,
    progress: Progress | None = None,
    recheck_days: int = 1,
    ingest: bool = False,
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
            summary = request.app.state.previews.get(check.url)
            cur = await db.execute(
                """INSERT INTO custom_sitemaps(url, kind, url_count, added_by, recheck_days, summary)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    check.url,
                    check.kind,
                    summary["total"] if summary else check.url_count,
                    visitor(request) or "api",
                    recheck_days,
                    json.dumps(summary) if summary else None,
                ),
            )
            existing.add(check.url)
            result.update(
                ok=True, id=cur.lastrowid, url=check.url, kind=check.kind, url_count=check.url_count
            )
            pages = summary["total"] if summary else check.url_count
            what = "pages" if summary or check.kind == "urlset" else "nested sitemaps"
            when = (
                f"crawling now, {_duration(catalog.estimate_seconds(summary['new'], settings))}"
                if ingest and summary
                else "crawling now"
                if ingest
                else f"crawled at the next check ({catalog.RECHECK_CHOICES[recheck_days].lower()})"
            )
            await report(step, "check", f"Added: {pages:,} {what}", when)
        await db.commit()
    added = {r["url"] for r in results if r["ok"]}
    if ingest and added:
        refresher: Refresher = request.app.state.refresher
        request.app.state.refresh_task = asyncio.create_task(refresher.ingest_sources(added))
    return {"results": results, "ingesting": bool(ingest and added)}


@app.post("/sitemaps", dependencies=[Protected, SignedIn])
async def add_sitemaps(body: SitemapsIn, request: Request) -> dict[str, Any]:
    """Validate each URL (format, public host, reachable, a real sitemap) and add the good
    ones. Each URL gets its own result, so one bad line doesn't block the rest. Their pages
    are crawled at the next refresh."""
    sitemap_limiter.check(request)
    return await _add_sitemaps(body.urls, request, None, body.recheck_days, body.ingest)


@app.post("/sitemaps/stream", dependencies=[Protected, SignedIn])
async def add_sitemaps_stream(body: SitemapsIn, request: Request) -> StreamingResponse:
    """Same as POST /sitemaps, reporting each check as it happens (server-sent events)."""
    sitemap_limiter.check(request)
    return _event_stream(
        lambda progress: _add_sitemaps(body.urls, request, progress, body.recheck_days, body.ingest)
    )


@app.put("/sitemaps/{sitemap_id}/schedule", dependencies=[Protected, SignedIn])
async def set_sitemap_schedule(sitemap_id: int, body: ScheduleIn) -> dict[str, Any]:
    """How often a custom sitemap is re-checked: daily, weekly, every 15 or 30 days."""
    async with connect(settings.db_path) as db:
        cur = await db.execute(
            "UPDATE custom_sitemaps SET recheck_days=? WHERE id=?", (body.recheck_days, sitemap_id)
        )
        if not cur.rowcount:
            raise HTTPException(404, "no such custom sitemap")
        await db.commit()
    return {"id": sitemap_id, "recheck_days": body.recheck_days}


@app.put("/sources/help-center/schedule", dependencies=[Protected, SignedIn])
async def set_helpcenter_schedule(body: ScheduleIn) -> dict[str, Any]:
    """How often the help center's own sitemap is re-checked."""
    async with connect(settings.db_path) as db:
        await catalog.set_helpcenter_days(db, body.recheck_days)
        await db.commit()
    return {"source": "help-center", "recheck_days": body.recheck_days}


@app.post("/sitemaps/{sitemap_id}/ingest", status_code=202, dependencies=[Protected, SignedIn])
async def ingest_sitemap(sitemap_id: int, request: Request) -> dict[str, Any]:
    """Check one custom sitemap now and crawl what changed (queued if a refresh is running)."""
    refresh_limiter.check(request)
    async with connect(settings.db_path) as db:
        rows = list(
            await db.execute_fetchall("SELECT url FROM custom_sitemaps WHERE id=?", (sitemap_id,))
        )
    if not rows:
        raise HTTPException(404, "no such custom sitemap")
    refresher: Refresher = request.app.state.refresher
    queued = refresher.running
    request.app.state.refresh_task = asyncio.create_task(refresher.ingest_sources({rows[0]["url"]}))
    return {"url": rows[0]["url"], "status": "queued" if queued else "started"}


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
        ge=1,
        le=30,
        description="Checks in a row before a URL needs a decision (presets: 2, 15, 30)",
    )
    action_gone: Literal["quarantine", "flag"] = Field(
        description="404 while still listed in its sitemap (a 404 that's also missing from it "
        "is archived at once)"
    )
    action_moved: Literal["quarantine", "flag"] = Field(
        description="301/308 with no new address (with one, the page is re-pointed and re-indexed)"
    )


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
        "threshold_presets": url_health_rules.THRESHOLD_PRESETS,
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


@app.get("/archive", dependencies=[Protected])
async def archived_kb(request: Request, q: str | None = None, limit: int = 100) -> dict[str, Any]:
    """The Archived KB: pages taken out of normal answers (gone, or archived after review),
    kept with their text and passages. `q` searches only the archive."""
    limit = max(1, min(limit, 500))
    async with connect(settings.db_path) as db:
        if q and len(q.strip()) >= 3:
            searcher: Searcher = request.app.state.searcher
            hits = await searcher.search(db, q.strip(), k=20, mode="hybrid", archived=True)
            ids = list(dict.fromkeys(h.article_id for h in hits))[:limit]
            snippets = {h.article_id: h.text[:240] for h in reversed(hits)}
        else:
            ids, snippets = [], {}
        where = (
            f"AND p.article_id IN ({','.join('?' * len(ids))})" if q and len(q.strip()) >= 3 else ""
        )
        rows = await db.execute_fetchall(
            f"""SELECT p.article_id, p.url, p.source, p.last_error AS reason, p.quarantined_at,
                       a.title, (SELECT count(*) FROM chunks c WHERE c.article_id = p.article_id AND c.is_active = 1) AS passages
                FROM pages p LEFT JOIN articles a USING (article_id)
                WHERE p.status = 'quarantined' {where}
                ORDER BY p.quarantined_at DESC, p.url LIMIT ?""",
            [*ids, limit],
        )
        [total] = list(
            await db.execute_fetchall("SELECT count(*) AS n FROM pages WHERE status='quarantined'")
        )
    order = {aid: i for i, aid in enumerate(ids)}
    items = [
        {
            **dict(r),
            "archived_at": _utc(r["quarantined_at"]),
            "snippet": snippets.get(r["article_id"]),
        }
        for r in rows
    ]
    if ids:
        items.sort(key=lambda r: order.get(r["article_id"], 1_000_000))
    return {"total": total["n"], "query": q, "items": items}


@app.get("/history", dependencies=[Protected])
async def change_history(article_id: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Every change to a page (added, content changed, moved, archived, restored, marked
    duplicate, broken), newest first: for one page, or across the knowledge base."""
    limit = max(1, min(limit, 500))
    async with connect(settings.db_path) as db:
        rows = await db.execute_fetchall(
            f"""SELECT h.*, a.title FROM page_history h LEFT JOIN articles a USING (article_id)
                {"WHERE h.article_id = ?" if article_id else ""}
                ORDER BY h.id DESC LIMIT ?""",
            [article_id, limit] if article_id else [limit],
        )
    return {"items": [dict(r) for r in rows]}


@app.get("/duplicates", dependencies=[Protected])
async def duplicates() -> dict[str, Any]:
    """Pages stored once but found twice: the same content at another URL (searched once),
    and pages listed by more than one sitemap."""
    async with connect(settings.db_path) as db:
        same_content = await db.execute_fetchall(
            """SELECT p.article_id, p.url, p.source, p.duplicate_of, o.url AS original_url,
                      a.title FROM pages p LEFT JOIN pages o ON o.article_id = p.duplicate_of
                      LEFT JOIN articles a ON a.article_id = p.article_id
               WHERE p.status = 'duplicate' ORDER BY p.url"""
        )
        multi = await db.execute_fetchall(
            """SELECT l.article_id, p.url, a.title, group_concat(l.source, '
') AS sources
               FROM page_listings l JOIN pages p USING (article_id)
               LEFT JOIN articles a USING (article_id)
               GROUP BY l.article_id HAVING count(*) > 1 ORDER BY p.url"""
        )
    return {
        "same_content": [dict(r) for r in same_content],
        "listed_twice": [{**dict(r), "sources": r["sources"].split("\n")} for r in multi],
    }


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
                """SELECT (SELECT count(*) FROM chunks WHERE is_active = 1) AS chunks,
                          (SELECT count(*) FROM images) AS images_total,
                          (SELECT count(*) FROM images WHERE described_with IS NOT NULL
                             AND described_with NOT LIKE 'failed:%') AS images_described,
                          (SELECT count(*) FROM images WHERE described_with LIKE 'failed:%')
                             AS images_failed,
                          (SELECT count(*) FROM escalations WHERE status='open') AS open_esc,
                          (SELECT count(*) FROM pages) AS pages_total,
                          (SELECT count(*) FROM articles a JOIN pages p USING (article_id)
                             WHERE p.status = 'done' AND a.indexed_hash IS NOT NULL) AS embedded,
                          (SELECT count(*) FROM articles a JOIN pages p USING (article_id)
                             WHERE p.status = 'done' AND a.indexed_hash IS NULL) AS pending_embed,
                          (SELECT count(*) FROM pages WHERE status = 'pending'
                             OR (status = 'failed' AND attempts < ?)) AS pending_fetch,
                          (SELECT count(*) FROM pages WHERE status = 'duplicate') AS duplicates,
                          (SELECT count(*) FROM (SELECT article_id FROM page_listings
                             GROUP BY article_id HAVING count(*) > 1)) AS listed_twice,
                          (SELECT count(*) FROM pages WHERE status = 'quarantined') AS archived,
                          (SELECT count(*) FROM pages WHERE status = 'failed'
                             AND attempts >= ?) AS failed""",
                [settings.max_attempts, settings.max_attempts],
            )
        )
        progress_row = None
        if request.app.state.refresher.running and last:
            [progress_row] = list(
                await db.execute_fetchall(
                    "SELECT count(*) AS done FROM fetch_log WHERE run_id=?", (last[0]["id"],)
                )
            )
    refresher: Refresher = request.app.state.refresher
    next_run = None
    if settings.refresh_enabled:
        next_run = (
            datetime.now(UTC) + timedelta(seconds=seconds_until(settings.refresh_hour_utc))
        ).isoformat(timespec="minutes")
    return IngestionStatus(
        counts={
            k: stats[k]
            for k in (
                "pages_total",
                "embedded",
                "pending_embed",
                "pending_fetch",
                "duplicates",
                "listed_twice",
                "archived",
                "failed",
            )
        },
        current_run=(
            {
                "run_id": last[0]["id"],
                "planned": last[0]["planned"],
                "done": progress_row["done"],
                "sources": json.loads(last[0]["sources"]) if last[0]["sources"] else None,
            }
            if progress_row is not None and last
            else None
        ),
        pages_by_status={r["status"]: r["n"] for r in counts},
        last_run=dict(last[0]) if last else None,
        chunks=stats["chunks"],
        images_total=stats["images_total"],
        images_described=stats["images_described"],
        images_failed=stats["images_failed"],
        open_escalations=stats["open_esc"],
        answering_enabled=request.app.state.answerer is not None,
        refresh_running=refresher.running,
        refresh_stopping=refresher.stopping,
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


@app.post("/ingestion/stop", status_code=202, dependencies=[Protected, SignedIn])
async def stop_refresh(request: Request) -> dict[str, str]:
    """Stop the running refresh or sitemap crawl at the next safe point: pages being fetched
    are finished and stored with their passages, no new page starts, and the finished pages
    are indexed. The rest stay pending for the next refresh. Sources added are kept."""
    refresher: Refresher = request.app.state.refresher
    if not refresher.stop():
        raise HTTPException(409, "nothing is running")
    return {"status": "stopping"}


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
    thread_id: int | None = Field(default=None, ge=1)
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
    claims: int = 0
    claims_supported: int = 0
    status_overridden: bool = False
    status: Literal["answered", "partial", "insufficient_evidence", "conflict"] = Field(
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
        raise HTTPException(503, f"answering is disabled: {claude.missing() or 'no answer model'}")
    query_limiter.check(request)
    over_budget(await spent_usd(), settings.budget_usd)
    return await _answer(req, request, answerer)


async def _answer(
    req: QueryRequest, request: Request, answerer: Answerer, progress: Progress | None = None
) -> QueryResponse:
    async with connect(settings.db_path) as db:
        question = req.question
        if req.thread_id and req.caller == "chat-ui":
            rows = await db.execute_fetchall(
                "SELECT question, response FROM chat_history WHERE owner=? "
                "AND COALESCE(thread_id, id)=? ORDER BY id DESC LIMIT 6",
                (chat_owner(request), req.thread_id),
            )
            if not rows:
                raise HTTPException(404, "Conversation not found")
            context = [
                {"user": row["question"], "assistant": json.loads(row["response"])["answer"][:3000]}
                for row in reversed(list(rows))
            ]
            question = with_context(req.question, context)
        result = await answer_question(
            db,
            request.app.state.searcher,
            answerer,
            question,
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
        raise HTTPException(503, f"answering is disabled: {claude.missing() or 'no answer model'}")
    query_limiter.check(request)
    over_budget(await spent_usd(), settings.budget_usd)

    owner = chat_owner(request) if req.caller == "chat-ui" else None

    async def work(progress: Progress) -> dict[str, Any]:
        result = (await _answer(req, request, answerer, progress)).model_dump()
        if owner:
            async with connect(settings.db_path) as db:
                cur = await db.execute(
                    "INSERT INTO chat_history(owner, question, response, thread_id) VALUES (?, ?, ?, ?)",
                    (owner, req.question, json.dumps(result), req.thread_id),
                )
                await db.commit()
            result["history_id"] = cur.lastrowid
            result["thread_id"] = req.thread_id or cur.lastrowid
        return result

    return _event_stream(work)


@app.get("/chat/history", dependencies=[Protected])
async def chat_history(
    request: Request, limit: int = 50, before: int | None = None, thread_id: int | None = None
) -> dict[str, Any]:
    """This person's past questions, newest first (each opens its full answer)."""
    owner = chat_owner(request)
    if not owner:
        return {"items": [], "next_before": None}
    page_size = max(1, min(limit, 200))
    async with connect(settings.db_path) as db:
        rows = await db.execute_fetchall(
            "SELECT h.id, h.question, h.response, h.created_at, COALESCE(h.thread_id, h.id) AS thread_id, (SELECT COALESCE(title, question) FROM chat_history root WHERE root.id=COALESCE(h.thread_id,h.id) AND root.owner=h.owner) AS title FROM chat_history h "
            "WHERE owner=? AND (? IS NULL OR id < ?) AND (? IS NULL OR COALESCE(thread_id,id)=?) ORDER BY id DESC LIMIT ?",
            (owner, before, before, thread_id, thread_id, page_size + 1),
        )
    page_rows = list(rows)
    return {
        "items": [
            {
                "id": r["id"],
                "thread_id": r["thread_id"],
                "title": r["title"] or r["question"],
                "question": r["question"],
                "created_at": r["created_at"],
                "response": json.loads(r["response"]),
            }
            for r in page_rows[:page_size]
        ],
        "next_before": page_rows[page_size - 1]["id"] if len(page_rows) > page_size else None,
    }


class ChatTitleIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)


@app.patch("/chat/threads/{thread_id}", dependencies=[Protected])
async def rename_chat_thread(thread_id: int, body: ChatTitleIn, request: Request) -> dict[str, Any]:
    title = body.title.strip()
    if not title:
        raise HTTPException(422, "Enter a conversation title")
    async with connect(settings.db_path) as db:
        cur = await db.execute(
            "UPDATE chat_history SET title=? WHERE owner=? AND id=? AND COALESCE(thread_id,id)=?",
            (title, chat_owner(request), thread_id, thread_id),
        )
        if not cur.rowcount:
            raise HTTPException(404, "Conversation not found")
        await db.commit()
    return {"title": title}


@app.delete("/chat/threads/{thread_id}", dependencies=[Protected])
async def delete_chat_thread(thread_id: int, request: Request) -> dict[str, Any]:
    async with connect(settings.db_path) as db:
        cur = await db.execute(
            "DELETE FROM chat_history WHERE owner=? AND COALESCE(thread_id,id)=?",
            (chat_owner(request), thread_id),
        )
        if not cur.rowcount:
            raise HTTPException(404, "Conversation not found")
        await db.commit()
    return {"deleted": cur.rowcount}


@app.delete("/chat/history", dependencies=[Protected])
async def clear_chat_history(request: Request) -> dict[str, Any]:
    """Clears this person's own chat history (nobody else's)."""
    owner = chat_owner(request)
    if not owner:
        return {"cleared": 0}
    async with connect(settings.db_path) as db:
        cur = await db.execute("DELETE FROM chat_history WHERE owner=?", (owner,))
        await db.commit()
    return {"cleared": cur.rowcount}


# ---------- Self-directed prompt engineering ----------

tune_limiter = RateLimiter(3, 3600, "prompt improvement rounds")


class PromptDecisionIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


def _apply_live_prompt(app: FastAPI, version: dict[str, Any]) -> None:
    rw = getattr(app.state.searcher, "rewriter", None)
    if isinstance(rw, ClaudeQueryRewriter):
        rw.use_prompt(version["text"], f"rewrite v{version['version']}")


def _brief(v: dict[str, Any]) -> dict[str, Any]:
    ev = v.get("evaluation") or {}
    return {
        **{
            k: v[k]
            for k in (
                "version",
                "status",
                "author",
                "based_on",
                "rationale",
                "created_at",
                "decided_at",
                "decided_by",
                "note",
            )
        },
        "summary": ev.get("summary"),
        "recommended": ev.get("recommended"),
        "verdict": ev.get("verdict"),
        "chars": len(v["text"]),
    }


@app.get("/prompts", dependencies=[Protected])
async def prompts(request: Request) -> dict[str, Any]:
    """The prompts the agent maintains itself: every version with its robustness scores."""
    async with connect(settings.db_path) as db:
        vs = await prompt_registry.versions(db, prompt_tuning.NAME)
        last = list(
            await db.execute_fetchall(
                "SELECT value FROM kb_settings WHERE key='prompt_last_tuned_at'"
            )
        )
    cases = robustness.load_cases()
    return {
        "name": prompt_tuning.NAME,
        "purpose": "Rewrites customer questions into help-center vocabulary before searching",
        "active_version": next((v["version"] for v in vs if v["status"] == "active"), None),
        "versions": [_brief(v) for v in vs],
        "tune_every_days": settings.prompt_tune_days,
        "last_tuned_at": last[0]["value"] if last else None,
        "can_tune": request.app.state.prompt_writer is not None
        and isinstance(getattr(request.app.state.searcher, "rewriter", None), ClaudeQueryRewriter),
        "test_set": {
            "groups": len(cases),
            "wordings": len(cases) * len(robustness.KINDS),
            "kinds": list(robustness.KINDS),
        },
    }


@app.get("/prompts/rewrite/{version}", dependencies=[Protected])
async def prompt_version(version: int) -> dict[str, Any]:
    """One version: its full text, what changed from the one it was based on, and how every
    question group scored."""
    import difflib

    async with connect(settings.db_path) as db:
        try:
            v = await prompt_registry.get(db, prompt_tuning.NAME, version)
            base = (
                await prompt_registry.get(db, prompt_tuning.NAME, v["based_on"])
                if v["based_on"]
                else None
            )
        except prompt_registry.PromptError as exc:
            raise HTTPException(404, str(exc)) from exc
    diff = (
        list(
            difflib.unified_diff(
                base["text"].splitlines(),
                v["text"].splitlines(),
                f"v{base['version']}",
                f"v{version}",
                lineterm="",
            )
        )
        if base
        else []
    )
    return {**_brief(v), "text": v["text"], "diff": diff, "evaluation": v["evaluation"]}


async def _tune(app: FastAPI, actor: str, progress: Progress | None) -> dict[str, Any]:
    writer = app.state.prompt_writer
    if writer is None or not isinstance(
        getattr(app.state.searcher, "rewriter", None), ClaudeQueryRewriter
    ):
        raise HTTPException(
            503, "prompt improvement needs Claude (Amazon Bedrock) and query rewriting on"
        )

    async def before_spend() -> None:
        over_budget(await spent_usd(), settings.budget_usd)

    if app.state.tuning.locked():
        raise HTTPException(409, "an improvement round is already running")
    async with app.state.tuning, connect(settings.db_path) as db:
        r = await prompt_tuning.tune(
            db,
            app.state.searcher,
            writer,
            settings.rewrite_model,
            actor=actor,
            progress=progress,
            before_spend=before_spend,
        )
        await db.execute(
            "INSERT INTO kb_settings(key, value) VALUES ('prompt_last_tuned_at', strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        await db.commit()
        if r.promoted:
            _apply_live_prompt(app, await prompt_registry.active(db, prompt_tuning.NAME))
    return {
        "version": r.version,
        "promoted": r.promoted,
        "verdict": r.verdict,
        "before": r.before,
        "after": r.after,
        "cost_usd": round(r.cost_usd, 4),
    }


@app.post("/prompts/rewrite/tune/stream", dependencies=[Protected, SignedIn])
async def tune_prompt(request: Request) -> StreamingResponse:
    """One improvement round, live: test the current prompt on every wording, have the agent
    write a new version from what failed, test that, and make it live only if it's better on
    questions it never saw."""
    if request.app.state.prompt_writer is None or not isinstance(
        getattr(request.app.state.searcher, "rewriter", None), ClaudeQueryRewriter
    ):
        raise HTTPException(
            503, "prompt improvement needs Claude (Amazon Bedrock) and query rewriting on"
        )
    if request.app.state.tuning.locked():
        raise HTTPException(409, "an improvement round is already running")
    tune_limiter.check(request)
    over_budget(await spent_usd(), settings.budget_usd)
    actor = signed_in(request) or visitor(request) or "api"
    return _event_stream(lambda progress: _tune(request.app, actor, progress))


@app.post("/prompts/rewrite/{version}/promote", dependencies=[Protected, SignedIn])
async def promote_prompt(version: int, body: PromptDecisionIn, request: Request) -> dict[str, Any]:
    async with connect(settings.db_path) as db:
        try:
            v = await prompt_registry.get(db, prompt_tuning.NAME, version)
            if not (v.get("evaluation") or {}).get("recommended") and not body.note:
                raise HTTPException(
                    422, "the check didn't recommend this version: say why it should go live"
                )
            await prompt_registry.activate(
                db,
                prompt_tuning.NAME,
                version,
                signed_in(request) or "api",
                body.note,
                override=True,
            )
            _apply_live_prompt(request.app, await prompt_registry.active(db, prompt_tuning.NAME))
        except prompt_registry.PromptError as exc:
            raise HTTPException(409, str(exc)) from exc
    return {"active_version": version}


@app.post("/prompts/rewrite/{version}/reject", dependencies=[Protected, SignedIn])
async def reject_prompt(version: int, body: PromptDecisionIn, request: Request) -> dict[str, Any]:
    if not body.note:
        raise HTTPException(422, "say why")
    async with connect(settings.db_path) as db:
        try:
            await prompt_registry.reject(
                db, prompt_tuning.NAME, version, signed_in(request) or "api", body.note
            )
        except prompt_registry.PromptError as exc:
            raise HTTPException(409, str(exc)) from exc
    return {"rejected": version}


@app.post("/prompts/rewrite/rollback", dependencies=[Protected, SignedIn])
async def rollback_prompt(body: PromptDecisionIn, request: Request) -> dict[str, Any]:
    if not body.note:
        raise HTTPException(422, "say why")
    async with connect(settings.db_path) as db:
        try:
            version = await prompt_registry.rollback(
                db, prompt_tuning.NAME, signed_in(request) or "api", body.note
            )
        except prompt_registry.PromptError as exc:
            raise HTTPException(409, str(exc)) from exc
        _apply_live_prompt(request.app, await prompt_registry.active(db, prompt_tuning.NAME))
    return {"active_version": version}


async def prompt_maintenance_loop(app: FastAPI) -> None:
    """Every `prompt_tune_days`, an hour after the daily refresh, the agent re-checks its own
    rewrite prompt and improves it if it can (within the budget)."""
    while True:
        await asyncio.sleep(seconds_until((settings.refresh_hour_utc + 1) % 24))
        try:
            async with connect(settings.db_path) as db:
                last = list(
                    await db.execute_fetchall(
                        "SELECT value FROM kb_settings WHERE key='prompt_last_tuned_at'"
                    )
                )
            if last and datetime.now(UTC) - datetime.fromisoformat(
                last[0]["value"].replace("Z", "+00:00")
            ) < timedelta(days=settings.prompt_tune_days) - timedelta(hours=3):
                continue
            result = await _tune(app, "scheduled self-check", None)
            log.info("prompt self-check: %s", result["verdict"])
        except Exception as exc:  # out of budget, no key, or a failed call: try next time
            log.warning("prompt self-check skipped: %s", exc)


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
