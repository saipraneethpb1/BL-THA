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
    monkeypatch.setattr(api.settings, "speech_preload", False)  # never download Whisper in tests
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


class FakeTranscriber:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None]] = []

    def transcribe(self, audio: bytes, language: str | None) -> tuple[str, str]:
        self.calls.append((len(audio), language))
        if audio == b"garbage":
            raise ValueError("cannot decode")
        return "how do I hide my site", language or "en"


def test_transcribe_turns_audio_into_text_and_validates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTranscriber()
    api.app.state.transcriber = fake
    audio = b"\x1aE\xdf\xa3" + b"\x00" * 100  # webm magic bytes; the fake doesn't decode it
    assert client.post("/transcribe", content=audio).status_code == 401
    r = client.post("/transcribe?language=hi", content=audio, headers=H)
    assert r.status_code == 200 and r.json()["text"] == "how do I hide my site"
    assert r.json()["language"] == "hi" and fake.calls == [(len(audio), "hi")]
    assert client.post("/transcribe", content=b"", headers=H).status_code == 422
    assert client.post("/transcribe?language=english", content=audio, headers=H).status_code == 422
    assert client.post("/transcribe", content=b"garbage", headers=H).status_code == 422
    monkeypatch.setattr(api.settings, "max_audio_bytes", 10)
    assert client.post("/transcribe", content=audio, headers=H).status_code == 413
    api.app.state.transcriber = None
    assert client.post("/transcribe", content=audio, headers=H).status_code == 503
    del api.app.state.transcriber


def test_public_demo_guards_limit_visitors_and_cap_spend(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3
    from contextlib import closing

    api.app.state.answerer = FakeAnswerer()
    monkeypatch.setattr(api.settings, "min_evidence_score", 0.99)
    api.query_limiter.calls.clear()
    monkeypatch.setattr(api.query_limiter, "limit", 2)
    visitor = {**H, "X-Forwarded-For": "203.0.113.7"}
    ask = {"question": "do you support SAP?"}
    assert client.post("/query", json=ask, headers=visitor).status_code == 200
    assert client.post("/query", json=ask, headers=visitor).status_code == 200
    third = client.post("/query", json=ask, headers=visitor)
    assert third.status_code == 429 and "try again" in third.json()["detail"]
    other = {**H, "X-Forwarded-For": "198.51.100.9"}
    assert client.post("/query", json=ask, headers=other).status_code == 200  # per visitor
    assert client.post("/query", json=ask, headers=H).status_code == 200  # internal: no limit

    with closing(sqlite3.connect(api.settings.db_path)) as db:
        db.execute(
            "INSERT INTO query_log (question, status, cost_usd) VALUES ('x', 'answered', 1.5)"
        )
        db.commit()
    monkeypatch.setattr(api.settings, "budget_usd", 1.0)
    capped = client.post("/query", json=ask, headers=H)
    assert capped.status_code == 429 and "budget" in capped.json()["detail"]
    u = client.get("/usage", headers=H).json()
    assert u["over_budget"] is True and u["budget_usd"] == 1.0
    api.query_limiter.calls.clear()


def test_custom_sitemaps_validate_each_url_on_entry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kb.ingestion.sources import SitemapCheck, SitemapError

    async def fake_validate(url: str, progress: object = None) -> SitemapCheck:
        if "broken" in url:
            raise SitemapError("The server answered HTTP 404, not a sitemap.")
        return SitemapCheck(url, "urlset", 12)

    api.app.state.sitemap_validator = fake_validate
    api.sitemap_limiter.calls.clear()
    monkeypatch.setattr(api.settings, "max_custom_sitemaps", 2)
    urls = [
        "https://shop.example.com/sitemap.xml",
        "not a url",
        "https://broken.example.com/sitemap.xml",
        "https://shop.example.com/sitemap.xml",
        "https://blog.example.com/sitemap.xml",
        "https://third.example.com/sitemap.xml",
    ]
    assert client.post("/sitemaps", json={"urls": urls}).status_code == 401
    r = client.post("/sitemaps", json={"urls": urls}, headers=H).json()["results"]
    assert [x["ok"] for x in r] == [True, False, False, False, True, False]
    assert "http" in r[1]["error"] and "404" in r[2]["error"]
    assert "already added" in r[3]["error"] and "allows 2" in r[5]["error"]
    assert r[0]["url_count"] == 12

    listed = client.get("/sitemaps", headers=H).json()
    assert [s["url"] for s in listed["custom"]] == [urls[0], urls[4]]
    assert listed["builtin"]["url"] == api.settings.sitemap_url
    builtin = client.post("/sitemaps", json={"urls": [api.settings.sitemap_url]}, headers=H).json()
    assert "always included" in builtin["results"][0]["error"]

    first = listed["custom"][0]["id"]
    assert client.delete(f"/sitemaps/{first}", headers=H).json() == {"removed": urls[0]}
    assert client.delete(f"/sitemaps/{first}", headers=H).status_code == 404
    del api.app.state.sitemap_validator


def test_url_health_settings_pending_list_and_decisions(client: TestClient) -> None:
    import sqlite3

    assert client.get("/url-health").status_code == 401
    assert client.get("/url-health", headers=H).json()["settings"] == {
        "threshold": 3,
        "action_gone": "quarantine",
        "action_moved": "quarantine",
    }
    bad = client.put(
        "/url-health/settings",
        json={"threshold": 0, "action_gone": "quarantine", "action_moved": "delete"},
        headers=H,
    )
    assert bad.status_code == 422  # threshold 1-30; there is no "delete" action
    with sqlite3.connect(api.settings.db_path) as db:
        db.executemany(
            "INSERT INTO pages(article_id, url, status) VALUES (?, ?, 'done')",
            [("a", "https://help.test/a"), ("b", "https://help.test/b")],
        )
        db.executemany(
            "INSERT INTO url_health(article_id, url, issue, count, state, last_status) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("a", "https://help.test/a", "gone", 2, "counting", 404),
                ("b", "https://help.test/b", "moved", 1, "counting", 301),
            ],
        )
    ok = client.put(
        "/url-health/settings",
        json={"threshold": 2, "action_gone": "quarantine", "action_moved": "flag"},
        headers=H,
    )
    assert ok.status_code == 200
    items = {i["article_id"]: i for i in client.get("/url-health", headers=H).json()["items"]}
    assert (items["a"]["state"], items["a"]["count"]) == ("pending", 2)  # shown before any action
    assert items["b"]["state"] == "counting"

    r = client.post(
        "/url-health/actions", json={"action": "quarantine", "article_ids": ["a"]}, headers=H
    )
    assert r.json() == {"action": "quarantine", "done": 1}
    body = client.get("/url-health", headers=H).json()
    assert [q["article_id"] for q in body["quarantined"]] == ["a"]
    assert "confirmed" in body["quarantined"][0]["reason"]
    assert (
        client.post(
            "/url-health/actions", json={"action": "keep", "article_ids": ["b"]}, headers=H
        ).json()["done"]
        == 1
    )
    assert (
        client.post(
            "/url-health/actions", json={"action": "restore", "article_ids": ["a"]}, headers=H
        ).json()["done"]
        == 1
    )
    body = client.get("/url-health", headers=H).json()
    assert body["items"] == [] and body["quarantined"] == []
    bad_action = client.post(
        "/url-health/actions", json={"action": "delete", "article_ids": ["a"]}, headers=H
    )
    assert bad_action.status_code == 422


def test_bedrock_error_reason_is_read_from_its_body() -> None:
    import anthropic
    import httpx

    msg = "You don't have access to the model with the specified model ID."
    response = httpx.Response(
        403, request=httpx.Request("POST", "https://bedrock-runtime.amazonaws.com")
    )
    exc = anthropic.PermissionDeniedError(msg, response=response, body={"message": msg})
    assert api.claude_error_message(exc) == msg
