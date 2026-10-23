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
    assert client.post("/query/stream", json={"question": "refund a member"}, headers=H).status_code == 503
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

    async def out_of_credit(*_args: object, progress: Progress | None = None, **_kw: object) -> None:
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
        "Added: 7 pages found",
    ]
    assert second[-1] == "Not added" and ev[-2]["detail"].startswith("The server answered HTTP 404")
    assert {e.get("group_label") for e in ev if "group" in e} == set(urls)
    assert [r["ok"] for r in ev[-1]["done"]["results"]] == [True, False]
