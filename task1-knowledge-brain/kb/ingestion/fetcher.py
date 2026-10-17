"""A polite HTTP fetcher: few parallel requests, a minimum gap between them,
and backoff when the server pushes back."""

import asyncio
import random
import time
from dataclasses import dataclass

import httpx

RETRYABLE = {429, 500, 502, 503, 504}


class FetchError(Exception):
    def __init__(self, message: str, *, permanent: bool) -> None:
        super().__init__(message)
        self.permanent = permanent  # True for 404/410: retrying will not help


@dataclass
class PoliteFetcher:
    client: httpx.AsyncClient
    max_concurrency: int = 2
    min_interval_s: float = 1.0
    max_attempts: int = 4

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(self.max_concurrency)
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    async def _wait_turn(self) -> None:
        # Global spacing across all workers, not per worker.
        async with self._lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self.min_interval_s
        if wait > 0:
            await asyncio.sleep(wait)

    async def get_text(self, url: str) -> str:
        async with self._sem:
            last_error = ""
            for attempt in range(1, self.max_attempts + 1):
                await self._wait_turn()
                try:
                    resp = await self.client.get(url)
                except httpx.TransportError as exc:
                    last_error = f"transport error: {exc!r}"
                else:
                    if resp.status_code == 200:
                        self._respect_rate_limit(resp)
                        return resp.text
                    if resp.status_code in (404, 410):
                        raise FetchError(f"HTTP {resp.status_code}", permanent=True)
                    if resp.status_code not in RETRYABLE:
                        raise FetchError(f"HTTP {resp.status_code}", permanent=False)
                    last_error = f"HTTP {resp.status_code}"
                    retry_after = resp.headers.get("retry-after")
                    if retry_after and retry_after.isdigit():
                        await asyncio.sleep(min(int(retry_after), 120))
                        continue
                if attempt < self.max_attempts:
                    await asyncio.sleep(2**attempt + random.uniform(0, 1))
            raise FetchError(
                f"gave up after {self.max_attempts} attempts: {last_error}", permanent=False
            )

    def _respect_rate_limit(self, resp: httpx.Response) -> None:
        # Freshdesk reports remaining calls (sometimes in two headers); slow down if close.
        values: list[float] = []
        for raw in resp.headers.get_list("x-ratelimit-remaining"):
            try:
                values.append(float(raw))
            except ValueError:
                continue
        if values and min(values) < 20:
            self._next_slot = time.monotonic() + 30
