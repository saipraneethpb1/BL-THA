"""The daily refresh: crawl what changed, describe new screenshots, re-index.

One refresh at a time (a lock), so the scheduler and a manual "run now" can never
collide. Each step is incremental, so a normal day touches only a handful of pages.
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
    status: str = "running"  # running | finished | failed
    trigger: str = "manual"
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
        self.describer = describer  # None = no API key: screenshots stay undescribed
        self.transport = transport
        self._lock = asyncio.Lock()
        self.last: RefreshResult | None = None

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run(self, trigger: str = "manual") -> RefreshResult:
        if self._lock.locked():
            raise RuntimeError("a refresh is already running")
        async with self._lock:
            result = RefreshResult(started_at=_now(), trigger=trigger)
            self.last = result
            try:
                ingest = await run_ingestion(self.settings, transport=self.transport)
                result.ingestion = ingest.as_dict()
                async with connect(self.settings.db_path) as db:
                    if self.describer is not None:
                        vision = await describe_pending_images(db, self.describer)
                        result.vision = {**asdict(vision), "cost_usd": round(vision.cost_usd, 4)}
                    index = await sync_index(db, self.embedder)
                    result.index = asdict(index)
                result.status = "finished"
            except Exception as exc:  # keep the service alive; the error is reported
                log.exception("refresh failed")
                result.status, result.error = "failed", repr(exc)
            result.finished_at = _now()
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
        result = await refresher.run(trigger="schedule")
        log.info("scheduled refresh %s: %s", result.status, result.ingestion)
