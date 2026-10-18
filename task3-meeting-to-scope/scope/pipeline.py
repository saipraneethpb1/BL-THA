"""The Scope Builder: runs the agent team and stores the result as a new draft version.

First build (no approved version yet):
    all meetings -> Analyst -> Feasibility -> Scope -> Estimator -> Proposal -> Review

Follow-up (an approved version exists):
    new meetings only -> Analyst -> Change detector (vs APPROVED requirements)
      -> Feasibility for added/modified requirements only -> Scope -> Estimator
      -> Diff (vs approved estimate) -> Proposal with a changes section -> Review
"""

from dataclasses import asdict
from typing import Any

from scope.agents.analyst import TranscriptAnalyst
from scope.agents.changes import ChangeDetector, apply_changes, make_diff
from scope.agents.estimator import Estimator
from scope.agents.feasibility import FeasibilityAgent
from scope.agents.proposal import ProposalWriter, render_markdown, review
from scope.agents.scoping import ScopeAgent
from scope.config import Settings
from scope.knowledge import KnowledgeClient
from scope.llm import StructuredLLM
from scope.models import FeasibilityResult, MeetingAnalysis
from scope.store import StateError, Store, VersionData
from scope.transcript import parse_transcript


def usage_since(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """This build's own model usage (the LLM client's counters are cumulative)."""
    steps = {k: round(v - before["by_step"].get(k, 0.0), 6) for k, v in after["by_step"].items()}
    return {
        "calls": after["calls"] - before["calls"],
        "input_tokens": after["input_tokens"] - before["input_tokens"],
        "output_tokens": after["output_tokens"] - before["output_tokens"],
        "cost_usd": round(after["cost_usd"] - before["cost_usd"], 6),
        "by_step": {k: v for k, v in steps.items() if v},
    }


class ScopeBuilder:
    def __init__(self, store: Store, llm: StructuredLLM, kb: KnowledgeClient, s: Settings) -> None:
        self.store = store
        self.llm = llm
        self.analyst = TranscriptAnalyst(llm, s.analyst_model, max_chunk_chars=s.max_chunk_chars)
        self.feasibility = FeasibilityAgent(llm, kb, s.feasibility_model)
        self.scoper = ScopeAgent(llm, s.scope_model)
        self.estimator = Estimator(
            llm, s.estimate_model, s.hourly_rate, s.currency, s.contingency_pct
        )
        self.writer = ProposalWriter(llm, s.proposal_model)
        self.changes = ChangeDetector(llm, s.changes_model)

    async def plan(self, pid: int) -> tuple[list[str], list[str], int | None]:
        """Which meetings to read: all of them, or only those since the approved baseline."""
        meetings = await self.store.meetings(pid)
        if not meetings:
            raise StateError("add a meeting transcript first")
        all_ids = [m["id"] for m in meetings]
        baseline = await self.store.latest_approved(pid)
        if baseline is None:
            return all_ids, all_ids, None
        new_ids = [m for m in all_ids if m not in baseline.meeting_ids]
        if not new_ids:
            raise StateError("no new meetings since the approved version")
        return all_ids, new_ids, baseline.number

    async def start(self, pid: int) -> int:
        all_ids, _, baseline = await self.plan(pid)
        return await self.store.start_version(pid, all_ids, baseline)

    async def build(self, pid: int, number: int) -> None:
        """Runs the agents for a version created by start(). Never raises: failures are stored."""
        before = asdict(self.llm.usage)
        try:
            await self._build(pid, number)
        except Exception as exc:  # the UI shows the error; the service stays up
            # Keep what the build spent before it failed, so the usage bar stays honest.
            spent = usage_since(before, asdict(self.llm.usage))
            await self.store.fail_version(pid, number, f"{type(exc).__name__}: {exc}", spent)

    async def _build(self, pid: int, number: int) -> None:
        before = asdict(self.llm.usage)
        project = await self.store.project(pid)
        assert project is not None
        _, read_ids, baseline_no = await self.plan(pid)
        meetings = {m["id"]: m for m in await self.store.meetings(pid)}
        transcripts = [
            parse_transcript(
                meetings[mid]["transcript"], mid, meetings[mid]["title"], meetings[mid]["date"]
            )
            for mid in read_ids
        ]
        problems: dict[str, list[str]] = {}
        diff = None
        analysis = await self.analyst.analyse(transcripts)

        if baseline_no is None:
            feasibility = await self.feasibility.check_all(analysis.requirements)
        else:
            base = await self.store.version(pid, baseline_no)
            assert base is not None and base.data is not None
            b = base.data
            notes = [n.statement for n in b.analysis.decisions + b.analysis.constraints]
            detection, problems["changes"] = await self.changes.detect(
                b.analysis.requirements, notes, analysis
            )
            revised, change_list = apply_changes(
                b.analysis.requirements, analysis, detection, notes
            )
            analysis = MeetingAnalysis(
                meeting_ids=b.analysis.meeting_ids + analysis.meeting_ids,
                requirements=revised,
                decisions=b.analysis.decisions + analysis.decisions,
                constraints=b.analysis.constraints + analysis.constraints,
                open_questions=analysis.open_questions,  # old questions were answered or carried over
                commitments=b.analysis.commitments + analysis.commitments,
            )
            touched = {c.requirement_id for c in change_list if c.change in ("added", "modified")}
            reused: list[FeasibilityResult] = [
                f
                for f in b.feasibility
                if f.requirement_id not in touched and f.requirement_id in {r.id for r in revised}
            ]
            fresh = await self.feasibility.check_all([r for r in revised if r.id in touched])
            feasibility = sorted(reused + fresh, key=lambda f: f.requirement_id)

        scope, problems["scope"] = await self.scoper.build(analysis, feasibility)
        estimate, problems["estimate"] = await self.estimator.estimate(scope)
        if baseline_no is not None:
            diff = make_diff(
                baseline_no, change_list, detection.constraint_changes, b.estimate, estimate
            )
        text, problems["proposal"] = await self.writer.write(
            project["name"], scope, feasibility, diff
        )
        markdown = render_markdown(
            text, analysis, feasibility, scope, estimate, diff, number, "draft"
        )
        issues = review(
            analysis, feasibility, scope, estimate, {k: v for k, v in problems.items() if v}
        )
        data = VersionData(
            analysis=analysis,
            feasibility=feasibility,
            scope=scope,
            estimate=estimate,
            proposal=text,
            proposal_md=markdown,
            diff=diff,
        )
        await self.store.finish_version(
            pid, number, data, issues, usage_since(before, asdict(self.llm.usage))
        )


async def export_proposal(store: Store, pid: int, number: int, actor: str) -> str:
    """The only way to get client-facing text. Refuses anything not approved."""
    v = await store.version(pid, number)
    if v is None or v.data is None:
        raise StateError("version not found")
    if v.status != "approved":
        raise StateError(
            f"v{number} is {v.status}; only an approved version can be sent to the client"
        )
    d = v.data
    md = render_markdown(
        d.proposal, d.analysis, d.feasibility, d.scope, d.estimate, d.diff, number, "approved"
    )
    await store.mark_exported(pid, number, actor)
    return md
