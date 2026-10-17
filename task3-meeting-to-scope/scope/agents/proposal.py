"""Proposal writer and reviewer.

The model writes prose only. Code renders every number, table and list from the
structured scope and estimate, so the proposal can never disagree with the estimate.
The reviewer is code: it lists what a human must look at before approving.
"""

import json
import re

from scope.agents.base import run_checked
from scope.llm import Effort, StructuredLLM, load_prompt
from scope.models import (
    Estimate,
    FeasibilityResult,
    MeetingAnalysis,
    ProposalText,
    ReviewIssue,
    ScopeDiff,
    ScopeOfWork,
)

NUMBERS = re.compile(
    r"[$€£₹]\s?\d|\b\d+(?:\.\d+)?\s?(?:hours?|hrs?|%|usd|inr|dollars|rupees)\b", re.I
)


def check_prose(p: ProposalText) -> list[str]:
    text = " ".join(p.model_dump().values())
    return [
        f"remove prices/hours/percentages from the prose: '{m.group(0)}'"
        for m in NUMBERS.finditer(text)
    ]


class ProposalWriter:
    def __init__(self, llm: StructuredLLM, model: str, effort: Effort = "medium") -> None:
        self.llm = llm
        self.model = model
        self.effort: Effort = effort
        self.prompt = load_prompt("proposal_v1")

    async def write(
        self,
        project_name: str,
        scope: ScopeOfWork,
        feasibility: list[FeasibilityResult],
        diff: ScopeDiff | None,
    ) -> tuple[ProposalText, list[str]]:
        existing = [
            {"requirement_id": f.requirement_id, "how_to": f.how_to, "limitation": f.limitation}
            for f in feasibility
            if f.verdict.startswith("existing")
        ]
        body = {
            "project": project_name,
            "deliverables": [
                {"id": d.id, "title": d.title, "description": d.description, "approach": d.approach}
                for d in scope.deliverables
            ],
            "covered_by_existing_features": existing,
            "open_questions": scope.open_questions,
            "client_responsibilities": scope.client_responsibilities,
            "changes_since_approved_version": [
                c.summary for c in diff.changes if c.change != "unchanged"
            ]
            if diff
            else None,
        }
        return await run_checked(
            self.llm,
            step="proposal",
            model=self.model,
            system=self.prompt,
            user=f"<input>\n{json.dumps(body, indent=1)}\n</input>",
            schema=ProposalText,
            effort=self.effort,
            check=check_prose,
        )


def _money(x: float, currency: str) -> str:
    return f"{currency} {x:,.0f}"


def render_markdown(
    text: ProposalText,
    analysis: MeetingAnalysis,
    feasibility: list[FeasibilityResult],
    scope: ScopeOfWork,
    estimate: Estimate,
    diff: ScopeDiff | None,
    version: int,
    status: str,
) -> str:
    reqs = {r.id: r for r in analysis.requirements}
    per = {d.deliverable_id: d for d in estimate.per_deliverable}
    out = [f"# {text.title}", f"*Version {version} · {status.upper()}*", ""]
    if status != "approved":
        out += ["> DRAFT: not approved for sending to the client.", ""]
    out += [
        "## Summary",
        text.executive_summary,
        "",
        "## Your goals",
        text.client_problem,
        "",
        "## Our approach",
        text.proposed_solution,
        "",
    ]

    existing = [f for f in feasibility if f.verdict.startswith("existing")]
    if existing:
        out += ["## Already possible with built-in features", ""]
        for f in existing:
            r = reqs.get(f.requirement_id)
            src = ", ".join(f"[{s.title}]({s.url})" if s.url else s.title for s in f.kb_sources[:2])
            out.append(
                f"- **{r.description if r else f.requirement_id}**: {f.how_to or f.explanation}"
                + (f" (limitation: {f.limitation})" if f.limitation else "")
                + (f". See {src}." if src else "")
            )
        out.append("")

    out += ["## Deliverables", ""]
    for d in scope.deliverables:
        t = per.get(d.id)
        hours = f"{t.low_hours:g}-{t.high_hours:g} h" if t else "not estimated"
        out += [f"### {d.id} {d.title} ({d.approach}, {hours})", d.description, "", "Done when:"]
        out += [f"- [ ] {c}" for c in d.acceptance_criteria]
        covers = ", ".join(
            f"{rid} {reqs[rid].description}" for rid in d.requirement_ids if rid in reqs
        )
        out += ["", f"*Covers: {covers}*", ""]

    out += ["## Estimate", "", "| Deliverable | Low | Likely | High |", "|---|---|---|---|"]
    for d in scope.deliverables:
        if t := per.get(d.id):
            out.append(
                f"| {d.id} {d.title} | {t.low_hours:g} h | {t.likely_hours:g} h | {t.high_hours:g} h |"
            )
    out += [
        f"| **Total** | **{estimate.low_hours:g} h** | **{estimate.likely_hours:g} h** | **{estimate.high_hours:g} h** |",
        "",
        f"**Price range: {_money(estimate.price_low, estimate.currency)} to {_money(estimate.price_high, estimate.currency)}** "
        f"(expected {estimate.expected_hours:g} h to high {estimate.high_hours:g} h at {_money(estimate.hourly_rate, estimate.currency)}/h, "
        f"plus {estimate.contingency_pct:g}% contingency).",
        "",
    ]
    if diff:
        out += [f"## Changes since approved version {diff.baseline_version}", ""]
        labels = {"added": "New", "modified": "Changed", "removed": "Removed"}
        for c in diff.changes:
            if c.change == "unchanged":
                continue
            flag = " **(outside the approved scope: quoted separately)**" if c.out_of_scope else ""
            out.append(f"- {labels[c.change]}: {c.summary}{flag}")
        out += [f"- Constraint change: {s}" for s in diff.constraint_changes]
        sign = "+" if diff.hours_delta_expected >= 0 else ""
        out += [
            "",
            f"Effect on the estimate: {sign}{diff.hours_delta_expected:g} expected hours "
            f"({sign}{_money(diff.price_delta_low, estimate.currency)} to {sign}{_money(diff.price_delta_high, estimate.currency)}).",
            "",
        ]

    def section(title: str, items: list[str]) -> list[str]:
        return [f"## {title}", *[f"- {i}" for i in items], ""] if items else []

    out += ["## Timeline", text.timeline, ""]
    out += section("Assumptions", scope.assumptions)
    out += section("Dependencies", scope.dependencies)
    out += section("What we need from you", scope.client_responsibilities)
    out += section(
        "Not included",
        scope.exclusions
        + [
            f"{e.requirement_id} {reqs[e.requirement_id].description if e.requirement_id in reqs else ''}: {e.reason}"
            for e in scope.excluded
        ],
    )
    out += section(
        "Questions to answer before we start",
        scope.open_questions
        + [f.clarifying_question for f in feasibility if f.clarifying_question],
    )
    out += ["## Next steps", text.next_steps, ""]
    return "\n".join(out)


def review(
    analysis: MeetingAnalysis,
    feasibility: list[FeasibilityResult],
    scope: ScopeOfWork,
    estimate: Estimate,
    problems: dict[str, list[str]],
) -> list[ReviewIssue]:
    issues: list[ReviewIssue] = []
    for step, ps in problems.items():
        issues += [ReviewIssue(severity="block", message=f"{step}: {p}") for p in ps]
    if not scope.deliverables:
        issues.append(ReviewIssue(severity="block", message="The scope has no deliverables."))
    for r in analysis.requirements:
        if r.status != "confirmed":
            issues.append(
                ReviewIssue(severity="warn", message=f"{r.id} is {r.status}: {r.description}")
            )
        if not any(e.verified for e in r.evidence):
            issues.append(
                ReviewIssue(
                    severity="warn",
                    message=f"{r.id}: the supporting quote was not found in the transcript; check it.",
                )
            )
    down = [f.requirement_id for f in feasibility if f.kb_status == "unavailable"]
    if down:
        issues.append(
            ReviewIssue(
                severity="warn",
                message=f"Product features not checked (Knowledge Brain down) for {', '.join(down)}.",
            )
        )
    low = [t.name for t in estimate.tasks if t.confidence == "low"]
    if low:
        issues.append(
            ReviewIssue(
                severity="warn",
                message=f"{len(low)} task estimate(s) have low confidence: {', '.join(low[:4])}.",
            )
        )
    if scope.open_questions:
        issues.append(
            ReviewIssue(
                severity="warn",
                message=f"{len(scope.open_questions)} open question(s) for the client.",
            )
        )
    return issues
