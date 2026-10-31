"""Every handoff between agents is one of these models. If an agent's output does not
fit its schema, the pipeline stops there instead of passing garbage downstream.

    Transcript --(Analyst: per chunk)--> ChunkExtraction
               --(Analyst: merge)-----> MeetingAnalysis   (requirements with evidence)
               --(Feasibility)--------> FeasibilityResult per requirement
               --(Scope)--------------> ScopeOfWork
               --(Estimator)----------> Estimate
               --(Proposal/Review)----> Proposal (needs human approval)
"""

from typing import Literal

from pydantic import BaseModel, Field

# ---------- Transcript ----------


class Turn(BaseModel):
    index: int
    speaker: str
    timestamp: str | None = None
    text: str


class Transcript(BaseModel):
    meeting_id: str
    title: str
    date: str
    turns: list[Turn]


# ---------- Transcript Analyst ----------

ItemKind = Literal["requirement", "decision", "constraint", "question", "commitment"]
Priority = Literal["must", "should", "could", "unknown"]
Origin = Literal["client_stated", "inferred"]


class ExtractedItem(BaseModel):
    """What the model returns for one chunk. `source_quote` must be copied word for word;
    code checks it against the transcript before it is trusted."""

    kind: ItemKind
    statement: str = Field(description="Specific and self-contained, in plain English")
    source_quote: str = Field(description="Exact words from the transcript, 5-40 words")
    speaker: str
    priority: Priority
    origin: Origin = Field(
        description="client_stated if the client said it; inferred if you deduced it"
    )


class ChunkExtraction(BaseModel):
    items: list[ExtractedItem]


class MergedEntry(BaseModel):
    """The merge step groups extracted items. It refers to items by id, so the evidence
    attached later is the original quote, not something the model rewrote."""

    statement: str
    item_ids: list[str] = Field(description="Ids of the extracted items this entry combines")
    status: Literal["confirmed", "unclear", "disputed"] = "confirmed"
    priority: Priority = "unknown"
    origin: Origin = "client_stated"


class MergeResult(BaseModel):
    requirements: list[MergedEntry]
    decisions: list[MergedEntry]
    constraints: list[MergedEntry]
    open_questions: list[MergedEntry]
    commitments: list[MergedEntry]


class Evidence(BaseModel):
    meeting_id: str
    quote: str
    speaker: str
    turn_index: int | None
    timestamp: str | None
    verified: bool = Field(description="True if the quote was found in the transcript")


class Requirement(BaseModel):
    id: str  # REQ-001
    description: str
    status: Literal["confirmed", "unclear", "disputed"]
    priority: Priority
    origin: Origin
    evidence: list[Evidence]


class Note(BaseModel):
    """A decision, constraint, open question or client commitment, with its evidence."""

    statement: str
    evidence: list[Evidence]


class MeetingAnalysis(BaseModel):
    meeting_ids: list[str]
    requirements: list[Requirement]
    decisions: list[Note]
    constraints: list[Note]
    open_questions: list[Note]
    commitments: list[Note]


# ---------- Feasibility ----------

Verdict = Literal[
    "existing_configuration",
    "existing_with_limitation",
    "custom_development",
    "needs_clarification",
]


class FeasibilityJudgement(BaseModel):
    """What the model decides after reading the Knowledge Brain's answer."""

    verdict: Verdict
    explanation: str = Field(description="One or two sentences a client could read")
    how_to: str | None = Field(
        default=None, description="If existing: the setting or feature to use, as documented"
    )
    limitation: str | None = None
    clarifying_question: str | None = Field(
        default=None, description="If needs_clarification: the question to ask the client"
    )


class KnowledgeSource(BaseModel):
    title: str
    url: str | None


class FeasibilityResult(BaseModel):
    requirement_id: str
    verdict: Verdict
    explanation: str
    how_to: str | None
    limitation: str | None
    clarifying_question: str | None
    kb_status: Literal["answered", "partial", "insufficient_evidence", "conflict", "unavailable"]
    kb_sources: list[KnowledgeSource]


# ---------- Scope ----------


class Deliverable(BaseModel):
    id: str = Field(description="D-01, D-02, ...")
    title: str
    description: str
    requirement_ids: list[str]
    approach: Literal["configuration", "custom", "mixed"]
    acceptance_criteria: list[str] = Field(
        description="Testable statements the client can check, e.g. 'Visitors can filter results by city'"
    )


class ExcludedRequirement(BaseModel):
    requirement_id: str
    reason: str


class ScopeOfWork(BaseModel):
    deliverables: list[Deliverable]
    excluded: list[ExcludedRequirement] = Field(
        description="Requirements deliberately left out of this scope, with the reason"
    )
    exclusions: list[str] = Field(description="Other things explicitly not included")
    assumptions: list[str]
    dependencies: list[str]
    client_responsibilities: list[str]
    open_questions: list[str] = Field(
        description="Questions the client must answer before work starts"
    )


# ---------- Estimate ----------


class EstimateTask(BaseModel):
    deliverable_id: str
    name: str
    low_hours: float
    likely_hours: float
    high_hours: float
    assumptions: str
    confidence: Literal["high", "medium", "low"]


class EstimateDraft(BaseModel):
    """What the model returns. All totals and prices are computed in code from this."""

    tasks: list[EstimateTask]
    risks: list[str]


class DeliverableTotal(BaseModel):
    deliverable_id: str
    low_hours: float
    likely_hours: float
    high_hours: float
    expected_hours: float


class Estimate(BaseModel):
    tasks: list[EstimateTask]
    risks: list[str]
    per_deliverable: list[DeliverableTotal]
    low_hours: float
    likely_hours: float
    high_hours: float
    expected_hours: float = Field(description="PERT: (low + 4*likely + high) / 6")
    contingency_pct: float
    hourly_rate: float
    currency: str
    price_low: float
    price_high: float


# ---------- Proposal ----------


class ProposalText(BaseModel):
    """Prose only. Tables, hours and prices are rendered by code from the Estimate."""

    title: str
    executive_summary: str
    client_problem: str
    proposed_solution: str
    timeline: str = Field(
        description="Milestones in order; no calendar dates unless the client set them"
    )
    next_steps: str


class ReviewIssue(BaseModel):
    severity: Literal["block", "warn"]
    message: str


# ---------- Follow-up meetings ----------

ChangeType = Literal["unchanged", "modified", "added", "removed"]


class ChangeLink(BaseModel):
    """Model output: how one item from the new meeting relates to the approved baseline."""

    new_item_id: str | None = Field(description="Id of the new-meeting requirement, if any")
    baseline_id: str | None = Field(
        description="REQ-xxx (approved requirement) or DEC-xxx (approved decision/constraint)"
    )
    change: Literal["same", "modified", "added", "removed"]
    summary: str = Field(description="What changed, in one sentence")
    revised_statement: str | None = Field(
        default=None,
        description="For 'modified': the complete requirement after the change, keeping every "
        "part of the approved one that still applies",
    )
    adds_work: bool = Field(
        default=True,
        description="True if this adds or expands work compared with the approved scope; "
        "false if it only removes, reduces or swaps like-for-like",
    )


class ChangeDetection(BaseModel):
    links: list[ChangeLink]
    constraint_changes: list[str] = Field(
        description="Changes to budget, deadline, launch date or other constraints"
    )


class RequirementChange(BaseModel):
    change: ChangeType
    requirement_id: str
    before: str | None
    after: str | None
    summary: str
    out_of_scope: bool = Field(
        description="Added or expanded after the baseline was approved: quote separately"
    )
    evidence: list[Evidence]


class ScopeDiff(BaseModel):
    baseline_version: int
    changes: list[RequirementChange]
    constraint_changes: list[str]
    hours_delta_expected: float
    price_delta_low: float
    price_delta_high: float
