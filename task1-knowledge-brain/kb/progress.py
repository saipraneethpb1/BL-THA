"""Live progress for slow requests: what the agent is doing right now.

A step is reported when it starts or finishes, as a small dict the page shows in its
activity timeline: `{"icon": ..., "text": ..., "detail": ...}`. Icons: think, search, doc,
check, warn, globe, shield, person. Code that accepts a `progress` callback works the same
without one (it's None for the plain JSON endpoints and in most tests).
"""

from collections.abc import Awaitable, Callable
from typing import Any

Progress = Callable[[dict[str, Any]], Awaitable[None]]


async def report(
    progress: Progress | None, icon: str, text: str, detail: str | None = None, **extra: Any
) -> None:
    if progress is not None:
        await progress(
            {"icon": icon, "text": text, **({"detail": detail} if detail else {}), **extra}
        )
