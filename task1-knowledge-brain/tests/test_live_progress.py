"""The streaming endpoints report each step as it happens, then the result."""

import asyncio
import json
from typing import Any

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from kb import api
from kb.progress import Progress, report
from tests.fakes import FakeAnswerer
from tests.test_api import H, client  # noqa: F401  (shared fixture)


@pytest.mark.asyncio
async def test_closing_a_stream_cancels_its_running_work() -> None:
    cancelled = asyncio.Event()

    async def work(progress: Progress) -> dict[str, Any]:
        try:
            await report(progress, "think", "Writing the answer")
            await asyncio.Event().wait()
            return {}
        finally:
            cancelled.set()

    response = api._event_stream(work)
    stream = response.body_iterator
    first = await anext(stream)
    assert "Writing the answer" in first
    await stream.aclose()
    assert cancelled.is_set()


def events(r: httpx.Response) -> list[dict[str, Any]]:
    assert r.headers["content-type"].startswith("text/event-stream")
    return [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]


def test_a_question_streams_its_steps_then_the_answer(client: TestClient) -> None:  # noqa: F811
    assert (
        client.post("/query/stream", json={"question": "refund a member"}, headers=H).status_code
        == 503
    )
    api.app.state.answerer = FakeAnswerer()
    ev = events(client.post("/query/stream", json={"question": "refund a member"}, headers=H))
    steps = [e["text"] for e in ev if "text" in e]
    assert steps[0] == "Ran 1 search (meaning + keywords)"
    assert steps[1].startswith("Found 0 passages")
    assert any("not close enough" in s or "No passage" in s for s in steps)
    assert any(s.startswith("Handed to a human expert") for s in steps)
    assert all(e["t_ms"] >= 0 for e in ev)
    done = ev[-1]["done"]
    assert done["status"] == "insufficient_evidence" and done["escalation_id"]


def test_a_failing_claude_call_ends_the_stream_with_its_reason(
    client: TestClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    msg = "Your credit balance is too low to access the Anthropic API."

    async def out_of_credit(
        *_args: object, progress: Progress | None = None, **_kw: object
    ) -> None:
        await report(progress, "think", "Writing the answer from 6 passages")
        response = httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com"))
        raise anthropic.BadRequestError(msg, response=response, body={"error": {"message": msg}})

    api.app.state.answerer = FakeAnswerer()
    monkeypatch.setattr(api, "answer_question", out_of_credit)
    ev = events(client.post("/query/stream", json={"question": "refund a member"}, headers=H))
    assert ev[0]["text"] == "Writing the answer from 6 passages"
    assert ev[-1] == {"error": f"Claude API error: {msg}", "status": 502, "t_ms": ev[-1]["t_ms"]}


def test_adding_sitemaps_streams_each_check_per_url(client: TestClient) -> None:  # noqa: F811
    from kb.ingestion.sources import SitemapCheck, SitemapError

    async def fake_validate(url: str, progress: Progress | None = None) -> SitemapCheck:
        await report(progress, "shield", "Checking that the host is a public site")
        if "broken" in url:
            raise SitemapError("The server answered HTTP 404, not a sitemap.")
        await report(progress, "globe", "Downloading the sitemap", url)
        return SitemapCheck(url, "urlset", 7)

    api.app.state.sitemap_validator = fake_validate
    api.sitemap_limiter.calls.clear()
    urls = ["https://shop.example.com/sitemap.xml", "https://broken.example.com/sitemap.xml"]
    try:
        assert client.post("/sitemaps/stream", json={"urls": urls}).status_code == 401
        ev = events(client.post("/sitemaps/stream", json={"urls": urls}, headers=H))
    finally:
        del api.app.state.sitemap_validator
    first = [e["text"] for e in ev if e.get("group") == 0]
    second = [e["text"] for e in ev if e.get("group") == 1]
    assert first == [
        "The address is a valid URL",
        "Checking that the host is a public site",
        "Downloading the sitemap",
        "Added: 7 pages",
    ]
    assert second[-1] == "Not added" and ev[-2]["detail"].startswith("The server answered HTTP 404")
    assert {e.get("group_label") for e in ev if "group" in e} == set(urls)
    assert [r["ok"] for r in ev[-1]["done"]["results"]] == [True, False]


def test_chat_history_is_kept_per_person_and_only_for_the_chat(client: TestClient) -> None:  # noqa: F811
    api.app.state.answerer = FakeAnswerer()
    me, other = {**H, "X-Chat-Id": "a" * 32}, {**H, "X-Chat-Id": "b" * 32}
    ask = {"question": "refund a member", "caller": "chat-ui"}
    done = events(client.post("/query/stream", json=ask, headers=me))[-1]["done"]
    assert done["history_id"]
    client.post("/query/stream", json={**ask, "caller": "inbox-agent"}, headers=me)  # not the chat
    client.post("/query/stream", json=ask, headers={**H, "X-Chat-Id": "not-hex"})  # no valid id
    mine = client.get("/chat/history", headers=me).json()["items"]
    assert [h["question"] for h in mine] == ["refund a member"]
    assert mine[0]["response"]["status"] == done["status"]
    assert client.get("/chat/history", headers=other).json()["items"] == []
    assert client.delete("/chat/history", headers=other).json() == {"cleared": 0}
    assert client.delete("/chat/history", headers=me).json() == {"cleared": 1}


def test_chat_history_pages_keep_older_answers_accessible(client: TestClient) -> None:  # noqa: F811
    api.app.state.answerer = FakeAnswerer()
    me = {**H, "X-Chat-Id": "c" * 32}
    ids = []
    for question in ["first question", "second question", "third question"]:
        result = events(
            client.post(
                "/query/stream", json={"question": question, "caller": "chat-ui"}, headers=me
            )
        )[-1]["done"]
        ids.append(result["history_id"])
    # Separate requests restore the persisted answer, including its citations.
    first = client.get("/chat/history?limit=2", headers=me).json()
    assert [h["id"] for h in first["items"]] == ids[:0:-1]
    assert first["next_before"] == ids[1]
    older = client.get(f"/chat/history?limit=2&before={first['next_before']}", headers=me).json()
    assert [h["id"] for h in older["items"]] == ids[:1]
    assert older["items"][0]["question"] == "first question"
    assert "sources" in older["items"][0]["response"]
    assert older["next_before"] is None
    other = client.get(
        f"/chat/history?before={ids[-1]}", headers={**H, "X-Chat-Id": "d" * 32}
    ).json()
    assert other["items"] == []


def test_followups_share_a_thread_and_receive_context(
    client: TestClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api.app.state.answerer = FakeAnswerer()
    me = {**H, "X-Chat-Id": "e" * 32}
    ask = {"question": "How do member refunds work?", "caller": "chat-ui"}
    first = events(client.post("/query/stream", json=ask, headers=me))[-1]["done"]
    original = api.answer_question
    questions = []

    async def capture(*args: Any, **kwargs: Any) -> Any:
        questions.append(args[3])
        return await original(*args, **kwargs)

    monkeypatch.setattr(api, "answer_question", capture)
    reply = {
        "question": "Can you explain that again?",
        "caller": "chat-ui",
        "thread_id": first["thread_id"],
    }
    second = events(client.post("/query/stream", json=reply, headers=me))[-1]["done"]
    assert second["thread_id"] == first["thread_id"]
    assert ask["question"] in questions[0] and reply["question"] in questions[0]
    thread = client.get(f"/chat/history?thread_id={first['thread_id']}", headers=me).json()["items"]
    assert [row["id"] for row in thread] == [second["history_id"], first["history_id"]]
    assert all(row["title"] == ask["question"] for row in thread)
    other = {**H, "X-Chat-Id": "f" * 32}
    assert (
        client.get(f"/chat/history?thread_id={first['thread_id']}", headers=other).json()["items"]
        == []
    )
    denied = events(client.post("/query/stream", json=reply, headers=other))[-1]
    assert "error" in denied
    assert len(questions) == 1


def test_rename_and_delete_apply_to_only_the_owned_conversation(client: TestClient) -> None:  # noqa: F811
    api.app.state.answerer = FakeAnswerer()
    me = {**H, "X-Chat-Id": "a" * 32}
    other = {**H, "X-Chat-Id": "b" * 32}
    ask = {"question": "How to submit a ticket?", "caller": "chat-ui"}
    first = events(client.post("/query/stream", json=ask, headers=me))[-1]["done"]
    thread_id = first["thread_id"]
    events(client.post("/query/stream", json={**ask, "thread_id": thread_id}, headers=me))
    separate = events(client.post("/query/stream", json=ask, headers=me))[-1]["done"]
    path = f"/chat/threads/{thread_id}"
    assert client.patch(path, json={"title": "Tickets"}, headers=other).status_code == 404
    assert client.delete(path, headers=other).status_code == 404
    assert client.patch(path, json={"title": "   "}, headers=me).status_code == 422
    assert client.patch(path, json={"title": "x" * 121}, headers=me).status_code == 422
    assert client.patch(path, json={"title": "  My support tickets  "}, headers=me).json() == {
        "title": "My support tickets"
    }
    rows = client.get(f"/chat/history?thread_id={thread_id}", headers=me).json()["items"]
    assert len(rows) == 2 and all(h["title"] == "My support tickets" for h in rows)
    assert client.delete(path, headers=me).json() == {"deleted": 2}
    rows = client.get("/chat/history", headers=me).json()["items"]
    assert [h["id"] for h in rows] == [separate["history_id"]]
    assert client.delete(path, headers=me).status_code == 404
