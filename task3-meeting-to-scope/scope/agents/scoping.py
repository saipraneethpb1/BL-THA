"""Scope agent: requirements + feasibility -> deliverables with acceptance criteria.
Code checks that every requirement is either delivered or explicitly excluded."""

import json

from scope.agents.base import run_checked
from scope.llm import Effort, StructuredLLM, load_prompt
from scope.models import FeasibilityResult, MeetingAnalysis, ScopeOfWork


def check_scope(scope: ScopeOfWork, requirement_ids: set[str]) -> list[str]:
    problems: list[str] = []
    placed: dict[str, list[str]] = {}
    for d in scope.deliverables:
        if not d.acceptance_criteria:
            problems.append(f"{d.id} has no acceptance criteria")
        for rid in d.requirement_ids:
            placed.setdefault(rid, []).append(d.id)
    for e in scope.excluded:
        placed.setdefault(e.requirement_id, []).append("excluded")
    for rid, where in placed.items():
        if rid not in requirement_ids:
            problems.append(f"{rid} does not exist; use only the given requirement ids")
        elif len(where) > 1:
            problems.append(f"{rid} appears in more than one place: {', '.join(where)}")
    for rid in sorted(requirement_ids - placed.keys()):
        problems.append(f"{rid} is in no deliverable and not excluded")
    ids = [d.id for d in scope.deliverables]
    if len(ids) != len(set(ids)):
        problems.append("deliverable ids must be unique")
    return problems


class ScopeAgent:
    def __init__(self, llm: StructuredLLM, model: str, effort: Effort = "medium") -> None:
        self.llm = llm
        self.model = model
        self.effort: Effort = effort
        self.prompt = load_prompt("scope_v1")

    async def build(
        self, analysis: MeetingAnalysis, feasibility: list[FeasibilityResult]
    ) -> tuple[ScopeOfWork, list[str]]:
        verdicts = {f.requirement_id: f for f in feasibility}
        reqs = [
            {
                "id": r.id,
                "description": r.description,
                "status": r.status,
                "priority": r.priority,
                "client_words": [e.quote for e in r.evidence][:2],
                "feasibility": verdicts[r.id].verdict if r.id in verdicts else "unknown",
                "how_to": verdicts[r.id].how_to if r.id in verdicts else None,
                "limitation": verdicts[r.id].limitation if r.id in verdicts else None,
            }
            for r in analysis.requirements
        ]
        notes = {
            "constraints": [n.statement for n in analysis.constraints],
            "decisions": [n.statement for n in analysis.decisions],
            "open_questions": [n.statement for n in analysis.open_questions],
            "client_commitments": [n.statement for n in analysis.commitments],
        }
        user = (
            f"<requirements>\n{json.dumps(reqs, indent=1)}\n</requirements>\n\n"
            f"<meeting_notes>\n{json.dumps(notes, indent=1)}\n</meeting_notes>"
        )
        ids = {r.id for r in analysis.requirements}
        return await run_checked(
            self.llm,
            step="scope",
            model=self.model,
            system=self.prompt,
            user=user,
            schema=ScopeOfWork,
            effort=self.effort,
            check=lambda s: check_scope(s, ids),
        )
