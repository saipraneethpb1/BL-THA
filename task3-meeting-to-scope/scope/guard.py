"""Guards for a public demo with no login: per-visitor rate limits and a spending cap.

Visitors reach the service through the proxy, which adds X-Forwarded-For; requests without
it (tests, local use) aren't rate-limited.
"""

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


def visitor(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    return forwarded.split(",")[0].strip() if forwarded else None


class RateLimiter:
    """At most `limit` calls per `window_s` seconds per visitor (sliding window, in memory)."""

    def __init__(self, limit: int, window_s: float, what: str) -> None:
        self.limit, self.window_s, self.what = limit, window_s, what
        self.calls: dict[str, deque[float]] = defaultdict(deque)

    def check(self, request: Request) -> None:
        who = visitor(request)
        if who is None:
            return
        now = time.monotonic()
        recent = self.calls[who]
        while recent and now - recent[0] > self.window_s:
            recent.popleft()
        if len(recent) >= self.limit:
            wait = int(self.window_s - (now - recent[0])) + 1
            raise HTTPException(
                429,
                f"Too many {self.what} from your connection (this public demo allows "
                f"{self.limit} per hour). Please try again in about {max(1, wait // 60)} minute(s), "
                "or look at the pre-built demo project meanwhile.",
                headers={"Retry-After": str(wait)},
            )
        recent.append(now)


def over_budget(spent_usd: float, budget_usd: float | None) -> None:
    if budget_usd is not None and spent_usd >= budget_usd:
        raise HTTPException(
            429,
            f"This demo's AI budget for the Scope Builder (${budget_usd:.2f}) is used up, so new "
            "builds are paused. The existing projects and versions can still be reviewed and approved.",
        )
