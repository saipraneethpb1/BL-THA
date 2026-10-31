"""The daily refresh: crawl what changed, describe new screenshots, re-index.

One refresh at a time (a lock), so the scheduler and a manual "run now" can never
collide. Each step is incremental, so a normal day touches only a handful of pages.

Stop: a refresh can be stopped at any time. It ends at the next safe point: pages being
fetched are finished and stored with their passages, no new page starts, and screenshot
descriptions stop between images. The pages that did finish are still indexed, so they
can be cited straight away; everything else is picked up by the next refresh.
"""

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from kb.config import Settings
from kb.db import connect
from kb.ingestion.pipeline import run_ingestion
from kb.ingestion.vision import ImageDescriber, describe_pending_images
from kb.retrieval.embedder import Embedder
from kb.retrieval.indexer import sync_index

log = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    started_at: str
    finished_at: str | None = None
    status: str = "running"  # running | finished | stopped | failed
    trigger: str = "manual"
    sources: list[str] | None = None  # None = every source (or every due one, on schedule)
    ingestion: dict[str, int] = field(default_factory=dict)
    vision: dict[str, object] | None = None
    index: dict[str, int] = field(default_factory=dict)
    error: str | None = None


class Refresher:
    def __init__(
        self,
        settings: Settings,
        embedder: Embedder,
        describer: ImageDescriber | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.embedder = embedder
        self.describer = describer  # None = Claude not configured: screenshots stay undescribed
        self.transport = transport
        self._lock = asyncio.Lock()
        self.last: RefreshResult | None = None
        # Sources confirmed while a refresh was running: crawled right after it.
        self.queued: set[str] = set()
        self._stop = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    @property
    def stopping(self) -> bool:
        return self.running and self._stop.is_set()

    def stop(self) -> bool:
        """Ask the running refresh to stop at the next safe point (see the module notes).
        Sources queued behind it are dropped too: they stay added, and are crawled at their
        next scheduled check. Returns False if nothing was running."""
        if not self.running:
            return False
        self._stop.set()
        self.queued.clear()
        return True

    async def ingest_sources(self, sources: set[str]) -> RefreshResult | None:
        """Crawl just these sources now (a sitemap someone confirmed). If a refresh is
        already running, they're queued and crawled straight after it."""
        if self.running:
            self.queued |= sources
            return None
        return await self.run(trigger="confirmed", sources=sources)

    async def run(
        self, trigger: str = "manual", sources: set[str] | None = None, scheduled: bool = False
    ) -> RefreshResult:
        if self._lock.locked():
            raise RuntimeError("a refresh is already running")
        async with self._lock:
            result = RefreshResult(
                started_at=_now(), trigger=trigger, sources=sorted(sources) if sources else None
            )
            self.last = result
            self._stop.clear()
            stopped = self._stop.is_set
            try:
                ingest = await run_ingestion(
                    self.settings,
                    transport=self.transport,
                    sources=sources,
                    scheduled=scheduled,
                    should_stop=stopped,
                )
                result.ingestion = ingest.as_dict()
                async with connect(self.settings.db_path) as db:
                    if self.describer is not None and not stopped():
                        vision = await describe_pending_images(
                            db, self.describer, should_stop=stopped
                        )
                        result.vision = {**asdict(vision), "cost_usd": round(vision.cost_usd, 4)}
                    # Always index what was stored, so finished pages are searchable now.
                    index = await sync_index(db, self.embedder)
                    result.index = asdict(index)
                result.status = "stopped" if stopped() else "finished"
            except Exception as exc:  # keep the service alive; the error is reported
                log.exception("refresh failed")
                result.status, result.error = "failed", repr(exc)
            result.finished_at = _now()
        if self.queued:
            queued, self.queued = self.queued, set()
            await self.run(trigger="confirmed", sources=queued)
        return result


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def seconds_until(hour_utc: int, now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def daily_loop(refresher: Refresher, hour_utc: int) -> None:
    """Sleep until the next run time, refresh, repeat. Cancelled on shutdown."""
    while True:
        await asyncio.sleep(seconds_until(hour_utc))
        if refresher.running:
            continue
        # Only the sources that are due (each has its own interval: kb/ingestion/catalog.py).
        result = await refresher.run(trigger="schedule", scheduled=True)
        log.info("scheduled refresh %s: %s", result.status, result.ingestion)
