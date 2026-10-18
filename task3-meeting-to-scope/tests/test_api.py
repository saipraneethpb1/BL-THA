import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scope import api
from scope.config import Settings
from scope.pipeline import ScopeBuilder
from scope.store import Store
from tests.fakes import TRANSCRIPT, FakeKB, kb_answer
from tests.team import team


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    store = Store(tmp_path / "scope.db")
    api.app.state.store = store
    api.app.state.builder = ScopeBuilder(
        store, team(), FakeKB({"map": kb_answer("Map.")}), Settings()
    )
    with TestClient(api.app) as c:
        yield c
    del api.app.state.store, api.app.state.builder


def test_full_flow_over_http(client: TestClient) -> None:
    pid = client.post("/api/projects", json={"name": "Vets", "client": "Green Valley"}).json()["id"]
    m = client.post(
        f"/api/projects/{pid}/meetings",
        json={"title": "Kickoff", "date": "2026-10-10", "transcript": TRANSCRIPT},
    )
    assert m.status_code == 201 and m.json()["turns"] == 7
    r = client.post(f"/api/projects/{pid}/build")
    assert r.status_code == 202
    n = r.json()["version"]
    # The build runs in the background; poll like the UI does.
    for _ in range(100):
        v = client.get(f"/api/projects/{pid}/versions/{n}").json()
        if v["status"] != "building":
            break
        time.sleep(0.05)
    assert v["status"] == "draft"
    assert client.get(f"/api/projects/{pid}/versions/{n}/export").status_code == 409
    assert client.post(
        f"/api/projects/{pid}/versions/{n}/approve", json={"actor": "Sai"}
    ).json() == {"status": "approved"}
    md = client.get(f"/api/projects/{pid}/versions/{n}/export?actor=Sai")
    assert md.status_code == 200 and md.text.startswith("# Green Valley Vets directory")
    p = client.get(f"/api/projects/{pid}").json()
    assert p["versions"][0]["status"] == "approved" and "transcript" not in p["meetings"][0]


def test_validation_and_state_errors(client: TestClient) -> None:
    pid = client.post("/api/projects", json={"name": "Vets", "client": "GV"}).json()["id"]
    bad = client.post(
        f"/api/projects/{pid}/meetings",
        json={
            "title": "Notes",
            "date": "2026-10-10",
            "transcript": "no speakers here at all, just one long paragraph of notes",
        },
    )
    assert bad.status_code == 422 and "speaker" in bad.json()["detail"]
    assert (
        client.post(
            f"/api/projects/{pid}/meetings",
            json={"title": "x", "date": "10/10/2026", "transcript": TRANSCRIPT},
        ).status_code
        == 422
    )
    assert client.post(f"/api/projects/{pid}/build").status_code == 409  # no meetings yet
    assert client.get("/api/projects/999").status_code == 404
    assert client.get(f"/api/projects/{pid}/versions/7/export").status_code == 404


def test_building_disabled_without_a_key(client: TestClient) -> None:
    api.app.state.builder = None
    assert client.get("/health").json() == {"status": "ok", "building_enabled": False}
    pid = client.post("/api/projects", json={"name": "Vets", "client": "GV"}).json()["id"]
    assert client.post(f"/api/projects/{pid}/build").status_code == 503
    assert "<title>Scope Builder</title>" in client.get("/").text


def test_sample_meetings_are_listed_and_served_safely(client: TestClient) -> None:
    samples = client.get("/api/samples").json()
    assert [s["name"] for s in samples] == ["ami_ES2002b", "ami_ES2002c", "ami_ES2002d"]
    text = client.get("/api/samples/ami_ES2002c").text
    assert len(text) > 10_000 and text.splitlines()[1].startswith("[00:00:00] Speaker A:")
    assert client.get("/api/samples/..%2F..%2Fpyproject").status_code == 404
    assert client.get("/api/samples/unknown").status_code == 404


def test_public_demo_guards_limit_builds_per_visitor_and_cap_spend(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = client.post("/api/projects", json={"name": "Vets", "client": "GV"}).json()["id"]
    client.post(
        f"/api/projects/{pid}/meetings",
        json={"title": "Kickoff", "date": "2026-10-10", "transcript": TRANSCRIPT},
    )
    api.build_limiter.calls.clear()
    monkeypatch.setattr(api.build_limiter, "limit", 1)
    who = {"X-Forwarded-For": "203.0.113.7"}
    assert client.post(f"/api/projects/{pid}/build", headers=who).status_code == 202
    second = client.post(f"/api/projects/{pid}/build", headers=who)
    assert second.status_code == 429 and "try again" in second.json()["detail"]

    monkeypatch.setattr(api.settings, "budget_usd", 0.0)
    capped = client.post(f"/api/projects/{pid}/build")
    assert capped.status_code == 429 and "budget" in capped.json()["detail"]
    assert client.get("/api/usage").json()["over_budget"] is True
    api.build_limiter.calls.clear()
