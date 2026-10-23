"""One workspace (database + scope builder) per demo account; "guest" for everyone else.

Each account's projects, meetings, versions and approvals live in their own SQLite file
under data/accounts/, so accounts can't see or change each other's work and everything
persists across visits. A new account starts as a copy of the starter database (the
pre-built demo project), so nobody has to spend credit on a first build.
"""

import asyncio
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scope.pipeline import ScopeBuilder
from scope.session import DEMO_ACCOUNTS, GUEST
from scope.store import Store

MakeBuilder = Callable[[Store], ScopeBuilder | None]


class Workspaces:
    def __init__(
        self, guest_db: Path, make_builder: MakeBuilder, starter_db: Path | None = None
    ) -> None:
        self.guest_db = guest_db
        self.accounts_dir = guest_db.parent / "accounts"
        self.make_builder = make_builder
        self.starter_db = starter_db
        self._open: dict[str, tuple[Store, ScopeBuilder | None]] = {}
        self._lock = asyncio.Lock()
        self.building = bool(os.environ.get("ANTHROPIC_API_KEY"))

    def path_for(self, account: str) -> Path:
        if account == GUEST:
            return self.guest_db
        if account not in DEMO_ACCOUNTS:  # only known names ever become file names
            raise ValueError(f"unknown account {account!r}")
        return self.accounts_dir / f"{account}.db"

    async def get(self, account: str) -> tuple[Store, ScopeBuilder | None]:
        async with self._lock:
            if account not in self._open:
                path = self.path_for(account)
                if (
                    not path.exists()
                    and account != GUEST
                    and self.starter_db
                    and self.starter_db.exists()
                ):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(self.starter_db, path)
                store = Store(path)
                await store.init()
                self._open[account] = (store, self.make_builder(store))
            return self._open[account]

    async def all_stores(self) -> list[Store]:
        """Every workspace that exists on disk, for site-wide usage and budget."""
        names = [GUEST] + sorted(
            p.stem for p in self.accounts_dir.glob("*.db") if p.stem in DEMO_ACCOUNTS
        )
        return [(await self.get(n))[0] for n in names]


class SingleWorkspace:
    """One store for every account: used when a test injects its own store and builder.
    Reads them from the app state each time, so a test can swap the builder later."""

    def __init__(self, state: Any) -> None:
        self.state = state

    @property
    def building(self) -> bool:
        return getattr(self.state, "builder", None) is not None

    async def get(self, account: str) -> tuple[Store, ScopeBuilder | None]:
        return self.state.store, getattr(self.state, "builder", None)

    async def all_stores(self) -> list[Store]:
        return [self.state.store]


def merge_usage(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Add up per-workspace usage: totals sum; the newest success and failure win."""
    ok = max((p["last_success_at"] for p in parts if p["last_success_at"]), default=None)
    errors = [p["last_error"] for p in parts if p["last_error"]]
    error = max(errors, key=lambda e: e["at"] or "") if errors else None
    if error and ok and (error["at"] or "") < ok:
        error = None
    return {
        "service": "scope",
        "total_usd": round(sum(p["total_usd"] for p in parts), 6),
        "today_usd": round(sum(p["today_usd"] for p in parts), 6),
        "calls": sum(p["calls"] for p in parts),
        "last_success_at": ok,
        "last_error": error,
    }
