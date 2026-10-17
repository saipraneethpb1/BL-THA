"""Estimation agent: the model breaks deliverables into tasks with low/likely/high hours.
Everything numeric after that (totals, PERT, contingency, price) is plain arithmetic in
code, because models are unreliable at adding up and must never invent a price."""

import json

from scope.agents.base import run_checked
from scope.llm import Effort, StructuredLLM, load_prompt
from scope.models import DeliverableTotal, Estimate, EstimateDraft, ScopeOfWork


def check_estimate(draft: EstimateDraft, deliverable_ids: set[str]) -> list[str]:
    problems: list[str] = []
    for t in draft.tasks:
        if t.deliverable_id not in deliverable_ids:
            problems.append(f"task '{t.name}' refers to unknown deliverable {t.deliverable_id}")
        if not (0 < t.low_hours <= t.likely_hours <= t.high_hours):
            problems.append(f"task '{t.name}': hours must satisfy 0 < low <= likely <= high")
        if t.likely_hours > 16:
            problems.append(f"task '{t.name}' is {t.likely_hours}h likely; split tasks above 16h")
    missing = deliverable_ids - {t.deliverable_id for t in draft.tasks}
    problems += [f"{d} has no tasks" for d in sorted(missing)]
    return problems


def pert(low: float, likely: float, high: float) -> float:
    return (low + 4 * likely + high) / 6


def compute_estimate(
    draft: EstimateDraft, hourly_rate: float, currency: str, contingency_pct: float
) -> Estimate:
    per: dict[str, DeliverableTotal] = {}
    for t in draft.tasks:
        d = per.setdefault(
            t.deliverable_id,
            DeliverableTotal(
                deliverable_id=t.deliverable_id,
                low_hours=0,
                likely_hours=0,
                high_hours=0,
                expected_hours=0,
            ),
        )
        d.low_hours += t.low_hours
        d.likely_hours += t.likely_hours
        d.high_hours += t.high_hours
        d.expected_hours += pert(t.low_hours, t.likely_hours, t.high_hours)
    low = sum(t.low_hours for t in draft.tasks)
    likely = sum(t.likely_hours for t in draft.tasks)
    high = sum(t.high_hours for t in draft.tasks)
    expected = sum(pert(t.low_hours, t.likely_hours, t.high_hours) for t in draft.tasks)
    # Price range: from the expected effort to the high effort, plus contingency on both.
    factor = 1 + contingency_pct / 100
    return Estimate(
        tasks=draft.tasks,
        risks=draft.risks,
        per_deliverable=[per[k] for k in sorted(per)],
        low_hours=round(low, 1),
        likely_hours=round(likely, 1),
        high_hours=round(high, 1),
        expected_hours=round(expected, 1),
        contingency_pct=contingency_pct,
        hourly_rate=hourly_rate,
        currency=currency,
        price_low=round(expected * hourly_rate * factor, -1),
        price_high=round(high * hourly_rate * factor, -1),
    )


class Estimator:
    def __init__(
        self,
        llm: StructuredLLM,
        model: str,
        hourly_rate: float,
        currency: str,
        contingency_pct: float,
        effort: Effort = "medium",
    ) -> None:
        self.llm = llm
        self.model = model
        self.hourly_rate = hourly_rate
        self.currency = currency
        self.contingency_pct = contingency_pct
        self.effort: Effort = effort
        self.prompt = load_prompt("estimate_v1")

    async def estimate(self, scope: ScopeOfWork) -> tuple[Estimate, list[str]]:
        body = {
            "deliverables": [d.model_dump() for d in scope.deliverables],
            "assumptions": scope.assumptions,
            "dependencies": scope.dependencies,
            "open_questions": scope.open_questions,
        }
        ids = {d.id for d in scope.deliverables}
        draft, problems = await run_checked(
            self.llm,
            step="estimate",
            model=self.model,
            system=self.prompt,
            user=f"<scope>\n{json.dumps(body, indent=1)}\n</scope>",
            schema=EstimateDraft,
            effort=self.effort,
            check=lambda d: check_estimate(d, ids),
        )
        # Drop tasks that still violate the rules rather than let them skew the totals.
        valid = EstimateDraft(
            tasks=[
                t
                for t in draft.tasks
                if t.deliverable_id in ids and 0 < t.low_hours <= t.likely_hours <= t.high_hours
            ],
            risks=draft.risks,
        )
        return compute_estimate(
            valid, self.hourly_rate, self.currency, self.contingency_pct
        ), problems
