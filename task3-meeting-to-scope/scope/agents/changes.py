"""Follow-up meetings: what changed compared with the APPROVED baseline.

Comparing with the approved version, not the last draft, matters: otherwise an AI
suggestion that nobody agreed to could quietly become part of the baseline.

The model links new-meeting requirements to baseline ids. Code applies the links,
re-numbers, and guarantees coverage: a new-meeting requirement the model did not link
is treated as "added", so new work can never slip in unquoted.
"""

import json

from scope.agents.base import run_checked
from scope.llm import Effort, StructuredLLM, load_prompt
from scope.models import (
    ChangeDetection,
    Estimate,
    MeetingAnalysis,
    Requirement,
    RequirementChange,
    ScopeDiff,
)


def check_links(d: ChangeDetection, baseline_ids: set[str], new_ids: set[str]) -> list[str]:
    problems = []
    for link in d.links:
        if link.baseline_id and link.baseline_id not in baseline_ids:
            problems.append(f"{link.baseline_id} is not an approved requirement id")
        if link.new_item_id and link.new_item_id not in new_ids:
            problems.append(f"{link.new_item_id} is not a new-meeting item id")
        if link.change in ("same", "modified") and not (link.baseline_id and link.new_item_id):
            problems.append(f"'{link.change}' links need both ids ({link.summary})")
        if link.change == "added" and not link.new_item_id:
            problems.append(f"'added' needs new_item_id ({link.summary})")
        if link.change == "removed" and not link.baseline_id:
            problems.append(f"'removed' needs baseline_id ({link.summary})")
    return problems


def apply_changes(
    baseline: list[Requirement], followup: MeetingAnalysis, detection: ChangeDetection
) -> tuple[list[Requirement], list[RequirementChange]]:
    """Returns the revised requirement list and one change record per baseline/new item."""
    base = {r.id: r for r in baseline}
    new = {f"NEW-{i:02d}": r for i, r in enumerate(followup.requirements, start=1)}
    revised = {r.id: r.model_copy(deep=True) for r in baseline}
    changes: dict[str, RequirementChange] = {}
    linked_new: set[str] = set()
    next_num = max((int(r.id.split("-")[1]) for r in baseline), default=0) + 1

    for link in detection.links:
        b = base.get(link.baseline_id or "")
        n = new.get(link.new_item_id or "")
        if link.new_item_id in new:
            linked_new.add(link.new_item_id or "")
        if link.change == "same" and b and n:
            revised[b.id].evidence += n.evidence
        elif link.change == "modified" and b and n:
            # Clients often state only the delta ("also show opening hours"): use the full
            # revised statement, falling back to old + delta, never the delta alone.
            after = link.revised_statement or f"{b.description}; {n.description}"
            revised[b.id] = b.model_copy(
                update={
                    "description": after,
                    "status": n.status,
                    "priority": n.priority,
                    "evidence": b.evidence + n.evidence,
                }
            )
            changes[b.id] = RequirementChange(
                change="modified",
                requirement_id=b.id,
                before=b.description,
                after=after,
                summary=link.summary,
                out_of_scope=True,
                evidence=n.evidence,
            )
        elif link.change == "removed" and b:
            revised.pop(b.id, None)
            changes[b.id] = RequirementChange(
                change="removed",
                requirement_id=b.id,
                before=b.description,
                after=None,
                summary=link.summary,
                out_of_scope=False,
                evidence=n.evidence if n else [],
            )
        elif link.change == "added" and n:
            pass  # handled below together with unlinked items
    for nid, n in new.items():
        is_added = any(
            link.change == "added" and link.new_item_id == nid for link in detection.links
        )
        if is_added or nid not in linked_new:
            rid = f"REQ-{next_num:03d}"
            next_num += 1
            revised[rid] = n.model_copy(update={"id": rid})
            summary = next(
                (link.summary for link in detection.links if link.new_item_id == nid), n.description
            )
            changes[rid] = RequirementChange(
                change="added",
                requirement_id=rid,
                before=None,
                after=n.description,
                summary=summary,
                out_of_scope=True,
                evidence=n.evidence,
            )
    for rid, b in base.items():
        changes.setdefault(
            rid,
            RequirementChange(
                change="unchanged",
                requirement_id=rid,
                before=b.description,
                after=b.description,
                summary="No change",
                out_of_scope=False,
                evidence=[],
            ),
        )
    order = sorted(changes.values(), key=lambda c: c.requirement_id)
    return sorted(revised.values(), key=lambda r: r.id), order


def make_diff(
    baseline_version: int,
    changes: list[RequirementChange],
    constraint_changes: list[str],
    before: Estimate,
    after: Estimate,
) -> ScopeDiff:
    return ScopeDiff(
        baseline_version=baseline_version,
        changes=changes,
        constraint_changes=constraint_changes,
        hours_delta_expected=round(after.expected_hours - before.expected_hours, 1),
        price_delta_low=after.price_low - before.price_low,
        price_delta_high=after.price_high - before.price_high,
    )


class ChangeDetector:
    def __init__(self, llm: StructuredLLM, model: str, effort: Effort = "medium") -> None:
        self.llm = llm
        self.model = model
        self.effort: Effort = effort
        self.prompt = load_prompt("changes_v1")

    async def detect(
        self,
        baseline: list[Requirement],
        baseline_constraints: list[str],
        followup: MeetingAnalysis,
    ) -> tuple[ChangeDetection, list[str]]:
        def view(rid: str, r: Requirement) -> dict[str, object]:
            return {
                "id": rid,
                "description": r.description,
                "status": r.status,
                "client_words": [e.quote for e in r.evidence][:2],
            }

        body = {
            "approved_requirements": [view(r.id, r) for r in baseline],
            "approved_constraints": baseline_constraints,
            "new_meeting_requirements": [
                view(f"NEW-{i:02d}", r) for i, r in enumerate(followup.requirements, 1)
            ],
            "new_meeting_constraints": [n.statement for n in followup.constraints],
            "new_meeting_decisions": [n.statement for n in followup.decisions],
        }
        new_ids = {f"NEW-{i:02d}" for i in range(1, len(followup.requirements) + 1)}
        return await run_checked(
            self.llm,
            step="changes",
            model=self.model,
            system=self.prompt,
            user=f"<input>\n{json.dumps(body, indent=1)}\n</input>",
            schema=ChangeDetection,
            effort=self.effort,
            check=lambda d: check_links(d, {r.id for r in baseline}, new_ids),
        )
