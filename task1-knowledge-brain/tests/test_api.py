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
    monkeypatch.setattr(api.settings, "bootstrap_crawl", False)  # never crawl from tests
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


def test_claude_failure_is_a_clear_502(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic
    import httpx

    msg = "Your credit balance is too low to access the Anthropic API."

    async def out_of_credit(*_args: object, **_kwargs: object) -> None:
        response = httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com"))
        raise anthropic.BadRequestError(msg, response=response, body={"error": {"message": msg}})

    api.app.state.answerer = FakeAnswerer()
    monkeypatch.setattr(api, "answer_question", out_of_credit)
    r = client.post("/query", json={"question": "refund a member"}, headers=H)
    assert r.status_code == 502 and r.json() == {"detail": f"Claude API error: {msg}"}


def test_usage_sums_recorded_spend_and_remembers_claude_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3
    from contextlib import closing

    api.app.state.last_claude_error = None
    assert client.get("/usage").status_code == 401
    empty = client.get("/usage", headers=H).json()
    assert empty["total_usd"] == 0 and empty["last_success_at"] is None
    assert empty["last_error"] is None
    with closing(sqlite3.connect(api.settings.db_path)) as db:
        db.executemany(
            "INSERT INTO query_log (question, status, cost_usd, caller) VALUES (?, 'answered', ?, ?)",
            [("a", 0.02, "chat-ui"), ("b", 0.01, "inbox-agent"), ("c", 0.03, "inbox-agent")],
        )
        db.commit()
    u = client.get("/usage", headers=H).json()
    assert u["total_usd"] == pytest.approx(0.06) and u["calls"] == 3
    assert u["by_caller"]["inbox-agent"]["usd"] == pytest.approx(0.04)
    assert u["last_success_at"] is not None

    test_claude_failure_is_a_clear_502(client, monkeypatch)
    assert "credit balance" in client.get("/usage", headers=H).json()["last_error"]["message"]
