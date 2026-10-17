import re

import httpx
import pytest
import respx

from scope.agents.analyst import TranscriptAnalyst
from scope.agents.feasibility import FeasibilityAgent
from scope.knowledge import HttpKnowledgeClient, KnowledgeUnavailableError
from scope.models import (
    ChunkExtraction,
    FeasibilityJudgement,
    MeetingAnalysis,
    MergeResult,
    Requirement,
)
from scope.transcript import parse_transcript
from tests.fakes import TRANSCRIPT, FakeKB, FakeLLM, kb_answer


def extraction(_: str) -> dict[str, object]:
    return {
        "items": [
            {
                "kind": "requirement",
                "statement": "Show a map on search results",
                "source_quote": "We definitely need a map on the search results",
                "speaker": "Priya (Client)",
                "priority": "must",
                "origin": "client_stated",
            },
            {
                "kind": "requirement",
                "statement": "Clinics pay a yearly listing fee by card",
                "source_quote": "clinics must pay a yearly listing fee by card",
                "speaker": "Priya (Client)",
                "priority": "must",
                "origin": "client_stated",
            },
            {
                "kind": "requirement",
                "statement": "Online appointment booking",
                "source_quote": "we want online booking for every clinic",  # never said
                "speaker": "Priya (Client)",
                "priority": "should",
                "origin": "client_stated",
            },
            {
                "kind": "constraint",
                "statement": "Budget about $5,000; launch by March",
                "source_quote": "Our budget is around five thousand dollars and we want to launch by March",
                "speaker": "Priya (Client)",
                "priority": "must",
                "origin": "client_stated",
            },
            {
                "kind": "question",
                "statement": "What exactly is wrong with search today?",
                "source_quote": "Honestly the search is bad",
                "speaker": "Priya (Client)",
                "priority": "unknown",
                "origin": "inferred",
            },
        ]
    }


def ids_of(user: str, word: str) -> list[str]:
    return [m.group(1) for m in re.finditer(rf"id=(\S+) kind={word}", user)]


def merge_dropping_one(user: str) -> dict[str, object]:
    reqs = ids_of(user, "requirement")
    return {
        "requirements": [
            # map: merged duplicates from overlapping chunks
            {
                "statement": "Map of nearby clinics on search results",
                "item_ids": [i for i in reqs if i.endswith("-1")],
                "priority": "must",
            },
            # booking: only supported by an unverifiable quote
            {
                "statement": "Online booking",
                "item_ids": [i for i in reqs if i.endswith("-3")],
                "status": "confirmed",
            },
            # invented: refers to ids that do not exist
            {"statement": "Mobile app", "item_ids": ["c9-9"]},
            # the payment requirement (-2) is forgotten on purpose
        ],
        "decisions": [],
        "constraints": [
            {"statement": "Budget ~$5k, launch by March", "item_ids": ids_of(user, "constraint")}
        ],
        "open_questions": [
            {"statement": "What is wrong with search?", "item_ids": ids_of(user, "question")}
        ],
        "commitments": [],
    }


async def analyse(chunk_chars: int = 8000) -> tuple[MeetingAnalysis, FakeLLM]:
    llm = FakeLLM({ChunkExtraction: extraction, MergeResult: merge_dropping_one})
    t = parse_transcript(TRANSCRIPT, "m1", "Kickoff", "2026-10-10")
    return await TranscriptAnalyst(llm, "fake", max_chunk_chars=chunk_chars).analyse([t]), llm


async def test_analyst_keeps_only_supported_requirements_with_verified_quotes() -> None:
    a, _ = await analyse()
    by_desc = {r.description: r for r in a.requirements}
    assert "Mobile app" not in by_desc  # invented by the merge: dropped

    m = by_desc["Map of nearby clinics on search results"]
    assert m.id == "REQ-001" and m.status == "confirmed"
    assert m.evidence[0].verified and m.evidence[0].timestamp == "00:01:02"

    booking = by_desc["Online booking"]
    assert booking.status == "unclear"  # "confirmed" downgraded: quote not in transcript
    assert not booking.evidence[0].verified


async def test_analyst_restores_requirements_the_merge_forgot() -> None:
    a, _ = await analyse()
    restored = [r for r in a.requirements if "yearly listing fee" in r.description]
    assert len(restored) == 1 and restored[0].status == "unclear"
    assert [r.id for r in a.requirements] == ["REQ-001", "REQ-002", "REQ-003"]


async def test_analyst_reads_every_chunk_and_keeps_notes() -> None:
    a, llm = await analyse(chunk_chars=250)
    extract_calls = [c for c in llm.calls if c[0] == "analyst.extract"]
    assert len(extract_calls) > 1
    assert "Part 1 of" in extract_calls[0][1]
    assert a.constraints[0].evidence and a.open_questions[0].statement
    assert llm.usage.calls == len(extract_calls) + 1


def req(desc: str = "Map of nearby clinics on search results") -> Requirement:
    return Requirement(
        id="REQ-001",
        description=desc,
        status="confirmed",
        priority="must",
        origin="client_stated",
        evidence=[],
    )


def judge(verdict: str) -> FeasibilityJudgement:
    return FeasibilityJudgement(
        verdict=verdict, explanation="Because.", how_to="Design Settings > Map"
    )  # type: ignore[arg-type]


async def test_feasibility_existing_with_evidence() -> None:
    kb = FakeKB({"map": kb_answer("Enable the Google Map on search results pages.")})
    agent = FeasibilityAgent(
        FakeLLM({FeasibilityJudgement: lambda _: judge("existing_configuration")}), kb, "fake"
    )
    r = await agent.check(req())
    assert (r.verdict, r.kb_status, r.how_to) == (
        "existing_configuration",
        "answered",
        "Design Settings > Map",
    )
    assert r.kb_sources[0].url == "https://help.test/map"


async def test_feasibility_cannot_claim_existing_without_evidence() -> None:
    agent = FeasibilityAgent(
        FakeLLM({FeasibilityJudgement: lambda _: judge("existing_configuration")}), FakeKB(), "fake"
    )
    r = await agent.check(req("Online booking"))
    assert r.verdict == "needs_clarification" and r.how_to is None
    assert "does not confirm" in r.explanation


async def test_feasibility_when_knowledge_brain_is_down() -> None:
    llm = FakeLLM({})
    r = await FeasibilityAgent(llm, FakeKB(down=True), "fake").check(req())
    assert (r.verdict, r.kb_status) == ("needs_clarification", "unavailable")
    assert llm.calls == []  # no model call without evidence


@respx.mock
async def test_http_client_retries_then_fails_cleanly() -> None:
    route = respx.post("http://kb/query").mock(
        side_effect=[httpx.Response(503), httpx.Response(503)]
    )
    client = HttpKnowledgeClient("http://kb", "key", retries=1)
    with pytest.raises(KnowledgeUnavailableError):
        await client.query("q")
    assert route.call_count == 2
    assert route.calls[0].request.headers["X-API-Key"] == "key"


@respx.mock
async def test_http_client_parses_answers() -> None:
    respx.post("http://kb/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "answered",
                "answer": "Yes.",
                "needs_human": False,
                "extra": 1,
                "sources": [{"title": "T", "url": None, "source_type": "expert", "section": None}],
            },
        )
    )
    ans = await HttpKnowledgeClient("http://kb", None).query("q")
    assert ans.status == "answered" and ans.sources[0].source_type == "expert"
