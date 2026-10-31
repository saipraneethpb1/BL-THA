"""Demo accounts: each has its own Scope Builder workspace; guest is shared; usage is site-wide."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scope import api
from scope.session import COOKIE, make_token
from scope.store import Store
from scope.workspaces import Workspaces, merge_usage


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for attr in ("store", "builder", "workspaces"):
        if hasattr(api.app.state, attr):
            delattr(api.app.state, attr)
    monkeypatch.setattr(api.settings, "db_path", tmp_path / "scope.db")
    monkeypatch.setattr(api.settings, "starter_db_path", None)
    monkeypatch.setenv("CLAUDE_PROVIDER", "anthropic")  # no Claude configured: building off
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return TestClient(api.app)


async def test_new_accounts_start_from_the_starter_project(tmp_path: Path) -> None:
    starter = Store(tmp_path / "starter.db")
    await starter.init()
    await starter.create_project("TV remote control (demo)", "Real Reaction")
    ws = Workspaces(tmp_path / "scope.db", lambda s: None, tmp_path / "starter.db")
    demo1, _ = await ws.get("demo1")
    assert [p["name"] for p in await demo1.projects()] == ["TV remote control (demo)"]
    guest, _ = await ws.get("guest")
    assert await guest.projects() == []  # guest is the existing shared database, not a copy
    with pytest.raises(ValueError):
        ws.path_for("../../etc/passwd")
    assert len(await ws.all_stores()) == 2


def test_accounts_only_see_their_own_projects(client: TestClient) -> None:
    with client:

        def as_(account: str | None) -> dict[str, str]:
            return {"cookie": f"{COOKIE}={make_token(account)}"} if account else {}

        for who in ("demo1", "demo2", None):
            r = client.post(
                "/api/projects",
                json={"name": f"Site for {who or 'guest'}", "client": "Acme"},
                headers=as_(who),
            )
            assert r.status_code == 201
        names = {
            who: [p["name"] for p in client.get("/api/projects", headers=as_(who)).json()]
            for who in ("demo1", "demo2", None)
        }
        assert names == {
            "demo1": ["Site for demo1"],
            "demo2": ["Site for demo2"],
            None: ["Site for guest"],
        }
        forged = {"cookie": f"{COOKIE}=demo1.9999999999.{'0' * 64}"}
        assert [p["name"] for p in client.get("/api/projects", headers=forged).json()] == [
            "Site for guest"
        ]
        assert client.get("/health").json() == {"status": "ok", "building_enabled": False}
    files = sorted(p.name for p in (api.settings.db_path.parent / "accounts").glob("*.db"))
    assert files == ["demo1.db", "demo2.db"]
    with sqlite3.connect(api.settings.db_path) as db:  # guest stays in the main database
        assert db.execute("SELECT name FROM projects").fetchall() == [("Site for guest",)]


def test_usage_adds_up_across_workspaces() -> None:
    a = {
        "total_usd": 0.5,
        "today_usd": 0.1,
        "calls": 2,
        "last_success_at": "2026-10-20T10:00:00+00:00",
        "last_error": None,
    }
    b = {
        "total_usd": 0.25,
        "today_usd": 0.25,
        "calls": 1,
        "last_success_at": "2026-10-21T10:00:00+00:00",
        "last_error": {"message": "out of credit", "at": "2026-10-20T09:00:00+00:00"},
    }
    u = merge_usage([a, b])
    assert (u["total_usd"], u["calls"], u["last_success_at"]) == (
        0.75,
        3,
        "2026-10-21T10:00:00+00:00",
    )
    assert u["last_error"] is None  # an older failure than the latest success is history
