from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kb import api
from kb.refresh import Refresher
from kb.retrieval.search import Searcher
from tests.fakes import FakeAnswerer, FakeEmbedder

H = {"X-API-Key": "secret"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(api.settings, "db_path", tmp_path / "kb.db")
    monkeypatch.setattr(api.settings, "api_key", "secret")
    api.app.state.searcher = Searcher(FakeEmbedder())
    monkeypatch.setattr(api.settings, "refresh_enabled", False)
    api.app.state.answerer = None
    api.app.state.refresher = Refresher(api.settings, FakeEmbedder(), None)
    with TestClient(api.app) as c:
        yield c
    del api.app.state.searcher
    del api.app.state.answerer
    del api.app.state.refresher


def test_health_is_public(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok", "articles": 0}


def test_protected_endpoints_need_the_key(client: TestClient) -> None:
    assert client.get("/ingestion/status").status_code == 401
    assert client.get("/ingestion/status", headers={"X-API-Key": "wrong"}).status_code == 401
    ok = client.get("/ingestion/status", headers=H).json()
    assert ok["pages_by_status"] == {} and ok["last_run"] is None
    assert ok["answering_enabled"] is False and ok["refresh_running"] is False
    assert ok["next_scheduled_refresh"] is None  # scheduler disabled in tests


def test_search_validates_input(client: TestClient) -> None:
    assert client.post("/search", json={"query": "x"}, headers=H).status_code == 422
    assert client.post("/search", json={"query": "refund", "k": 99}, headers=H).status_code == 422
    resp = client.post("/search", json={"query": "refund a member"}, headers=H)
    assert resp.status_code == 200 and resp.json()["passages"] == []


def test_query_is_503_without_a_model(client: TestClient) -> None:
    r = client.post("/query", json={"question": "refund a member"}, headers=H)
    assert r.status_code == 503


def test_query_and_escalation_flow(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    api.app.state.answerer = FakeAnswerer()
    monkeypatch.setattr(api.settings, "min_evidence_score", 0.99)  # empty db: nothing found
    r = client.post("/query", json={"question": "do you support SAP?", "caller": "t"}, headers=H)
    body = r.json()
    assert r.status_code == 200 and body["status"] == "insufficient_evidence"
    eid = body["escalation_id"]

    [item] = client.get("/escalations", headers=H).json()
    assert item["id"] == eid and item["caller"] == "t"

    def resolve(escalation_id: int, answer: str) -> int:
        payload = {"answer": answer, "author": "Priya"}
        return client.post(
            f"/escalations/{escalation_id}/resolve", json=payload, headers=H
        ).status_code

    assert resolve(eid, "short") == 422
    ok = client.post(
        f"/escalations/{eid}/resolve",
        json={"answer": "No, there is no SAP integration.", "author": "Priya"},
        headers=H,
    )
    assert ok.status_code == 200
    assert resolve(eid, "No, there is no SAP integration.") == 409
    assert resolve(999, "No, there is no SAP integration.") == 404
    aid = ok.json()["expert_answer_id"]
    assert client.post(f"/expert-answers/{aid}/retract", headers=H).json() == {"retracted": True}


def test_ui_is_served(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200 and "<title>Knowledge Brain</title>" in r.text


def test_manual_refresh_conflict(client: TestClient) -> None:
    refresher: Refresher = api.app.state.refresher

    async def hold() -> None:
        await refresher._lock.acquire()

    import asyncio

    asyncio.run(hold())
    try:
        assert client.post("/ingestion/run", headers=H).status_code == 409
    finally:
        refresher._lock.release()
