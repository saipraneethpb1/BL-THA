"""A scripted agent team for end-to-end tests: each fake answer is derived from the
input it receives, the way a real model would, so the plumbing is exercised for real."""

import json
import re
from typing import Any

from scope.models import (
    ChangeDetection,
    ChunkExtraction,
    EstimateDraft,
    FeasibilityJudgement,
    MergeResult,
    ProposalText,
    ScopeOfWork,
)
from tests.fakes import FakeLLM

FOLLOWUP = """[00:00:03] Sam (Agency): Welcome back. Anything changed since we approved the scope?
[00:00:10] Priya (Client): Yes. We don't need card payments any more, clinics will be invoiced offline.
[00:00:40] Priya (Client): The map should also show opening hours for each clinic.
[00:01:20] Priya (Client): And we now want a blog for pet care tips.
[00:02:00] Priya (Client): Launch moves to April.
"""

QUOTES = {
    "map": (
        "Show a map of nearby clinics on search results",
        "We definitely need a map on the search results",
    ),
    "pay": (
        "Clinics pay a yearly listing fee by card",
        "clinics must pay a yearly listing fee by card",
    ),
    "nopay": (
        "Drop card payments; invoice clinics offline",
        "We don't need card payments any more",
    ),
    "hours": (
        "Map shows opening hours for each clinic",
        "The map should also show opening hours for each clinic",
    ),
    "blog": ("Add a blog for pet care tips", "we now want a blog for pet care tips"),
}


def _item(key: str, kind: str = "requirement") -> dict[str, Any]:
    statement, quote = QUOTES[key]
    return {
        "kind": kind,
        "statement": statement,
        "source_quote": quote,
        "speaker": "Priya (Client)",
        "priority": "must",
        "origin": "client_stated",
    }


def extract(user: str) -> dict[str, Any]:
    items = []
    if "yearly listing fee" in user:
        items += [_item("map"), _item("pay")]
    if "don't need card payments" in user:
        items += [_item("nopay"), _item("hours"), _item("blog")]
    return {"items": items}


def merge(user: str) -> dict[str, Any]:
    ids = re.findall(r"id=(\S+) kind=requirement", user)
    return {
        "requirements": [
            {
                "statement": re.search(rf"id={re.escape(i)} .*?: (.*)", user).group(1),  # type: ignore[union-attr]
                "item_ids": [i],
                "priority": "must",
            }
            for i in ids
        ],
        "decisions": [],
        "constraints": [],
        "open_questions": [],
        "commitments": [],
    }


def feasibility(user: str) -> dict[str, Any]:
    if "map" in user.lower():
        return {
            "verdict": "existing_configuration",
            "explanation": "Built-in map.",
            "how_to": "Google Map Search Results",
        }
    return {"verdict": "custom_development", "explanation": "Needs building."}


def _json_block(user: str, tag: str) -> Any:
    return json.loads(re.search(rf"<{tag}>\n(.*)\n</{tag}>", user, re.S).group(1))  # type: ignore[union-attr]


def scope(user: str) -> dict[str, Any]:
    reqs = _json_block(user, "requirements")
    return {
        "deliverables": [
            {
                "id": f"D-{i:02d}",
                "title": r["description"][:40],
                "description": r["description"],
                "requirement_ids": [r["id"]],
                "approach": "configuration"
                if r["feasibility"].startswith("existing")
                else "custom",
                "acceptance_criteria": [f"Client can verify: {r['description']}"],
            }
            for i, r in enumerate(reqs, start=1)
        ],
        "excluded": [],
        "exclusions": ["Content writing"],
        "assumptions": ["Client supplies clinic data"],
        "dependencies": [],
        "client_responsibilities": ["Send clinic list"],
        "open_questions": [],
    }


def estimate(user: str) -> dict[str, Any]:
    deliverables = _json_block(user, "scope")["deliverables"]
    return {
        "tasks": [
            {
                "deliverable_id": d["id"],
                "name": f"Build {d['id']}",
                "low_hours": 2,
                "likely_hours": 4 if d["approach"] == "configuration" else 8,
                "high_hours": 6 if d["approach"] == "configuration" else 14,
                "assumptions": "none",
                "confidence": "medium",
            }
            for d in deliverables
        ],
        "risks": ["Data quality"],
    }


def changes(user: str) -> dict[str, Any]:
    body = _json_block(user, "input")
    base = {r["description"]: r["id"] for r in body["approved_requirements"]}
    new = {r["description"]: r["id"] for r in body["new_meeting_requirements"]}
    pay = next(v for k, v in base.items() if "fee" in k)
    mapr = next(v for k, v in base.items() if "map" in k.lower())
    return {
        "links": [
            {
                "new_item_id": new["Drop card payments; invoice clinics offline"],
                "baseline_id": pay,
                "change": "removed",
                "summary": "Card payments dropped",
            },
            {
                "new_item_id": new["Map shows opening hours for each clinic"],
                "baseline_id": mapr,
                "change": "modified",
                "summary": "Map now also shows opening hours",
                "revised_statement": "Show a map of nearby clinics on search results, "
                "including each clinic's opening hours",
            },
            # the blog is deliberately left unlinked: code must still treat it as added
        ],
        "constraint_changes": ["Launch moved from March to April"],
    }


def proposal(_: str) -> dict[str, Any]:
    return {
        "title": "Green Valley Vets directory",
        "executive_summary": "We propose a simple plan.",
        "client_problem": "Pet owners cannot find vets.",
        "proposed_solution": "Use the built-in map.",
        "timeline": "Week 1 configure, week 2 review.",
        "next_steps": "Answer questions and approve.",
    }


def team() -> FakeLLM:
    return FakeLLM(
        {
            ChunkExtraction: extract,
            MergeResult: merge,
            FeasibilityJudgement: feasibility,
            ScopeOfWork: scope,
            EstimateDraft: estimate,
            ChangeDetection: changes,
            ProposalText: proposal,
        }
    )
