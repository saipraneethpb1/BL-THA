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
    kb_status: Literal["answered", "partial", "insufficient_evidence", "unavailable"]
    kb_sources: list[KnowledgeSource]
