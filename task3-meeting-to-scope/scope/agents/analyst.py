"""Transcript Analyst: turns one or more meeting transcripts into requirements, decisions,
constraints, open questions and commitments, each tied to the exact words that support it.

Map: each chunk is read independently (in parallel) and returns extracted items.
Reduce: one call groups the items by id. Code then attaches the original, verified
quotes, numbers the requirements, and adds back any requirement the merge dropped.
"""

import asyncio

from scope.llm import Effort, StructuredLLM, load_prompt
from scope.models import (
    ChunkExtraction,
    Evidence,
    ExtractedItem,
    MeetingAnalysis,
    MergedEntry,
    MergeResult,
    Note,
    Requirement,
    Transcript,
)
from scope.transcript import Chunk, chunk_transcript, locate_quote

ItemRef = tuple[str, ExtractedItem, Evidence]  # (id, item, evidence)


class TranscriptAnalyst:
    def __init__(
        self,
        llm: StructuredLLM,
        model: str,
        merge_model: str | None = None,
        max_chunk_chars: int = 8000,
        concurrency: int = 4,
        effort: Effort = "medium",
    ) -> None:
        self.llm = llm
        self.model = model
        self.merge_model = merge_model or model
        self.max_chunk_chars = max_chunk_chars
        self.sem = asyncio.Semaphore(concurrency)
        self.effort: Effort = effort
        self.extract_prompt = load_prompt("analyst_extract_v2")
        self.merge_prompt = load_prompt("analyst_merge_v2")

    async def _extract(self, t: Transcript, chunk: Chunk, total: int) -> list[ItemRef]:
        user = (
            f"Meeting: {t.title} ({t.date}). Part {chunk.number} of {total}.\n\n"
            f"<transcript_part>\n{chunk.render()}\n</transcript_part>"
        )
        async with self.sem:
            out = await self.llm.run(
                step="analyst.extract",
                model=self.model,
                system=self.extract_prompt,
                user=user,
                schema=ChunkExtraction,
                effort=self.effort,
            )
        refs: list[ItemRef] = []
        for i, item in enumerate(out.items, start=1):
            turn = locate_quote(item.source_quote, chunk.turns)
            refs.append(
                (
                    f"{t.meeting_id}-c{chunk.number}-{i}",
                    item,
                    Evidence(
                        meeting_id=t.meeting_id,
                        quote=item.source_quote,
                        speaker=turn.speaker if turn else item.speaker,
                        turn_index=turn.index if turn else None,
                        timestamp=turn.timestamp if turn else None,
                        verified=turn is not None,
                    ),
                )
            )
        return refs

    async def analyse(self, transcripts: list[Transcript]) -> MeetingAnalysis:
        jobs = []
        for t in transcripts:
            chunks = chunk_transcript(t, self.max_chunk_chars)
            jobs += [self._extract(t, c, len(chunks)) for c in chunks]
        refs = [r for chunk_refs in await asyncio.gather(*jobs) for r in chunk_refs]
        by_id = {rid: (item, ev) for rid, item, ev in refs}

        listing = "\n".join(
            f"- id={rid} kind={item.kind} priority={item.priority} origin={item.origin} "
            f"speaker={item.speaker}: {item.statement}"
            for rid, item, _ in refs
        )
        merged = await self.llm.run(
            step="analyst.merge",
            model=self.merge_model,
            system=self.merge_prompt,
            user=f"<items>\n{listing}\n</items>",
            schema=MergeResult,
            effort=self.effort,
        )
        return build_analysis([t.meeting_id for t in transcripts], refs, by_id, merged)


def _evidence(
    entry: MergedEntry, by_id: dict[str, tuple[ExtractedItem, Evidence]]
) -> list[Evidence]:
    seen: set[tuple[str, str]] = set()
    out = []
    for rid in entry.item_ids:
        if rid in by_id:
            ev = by_id[rid][1]
            key = (ev.meeting_id, ev.quote)
            if key not in seen:
                seen.add(key)
                out.append(ev)
    return out


def build_analysis(
    meeting_ids: list[str],
    refs: list[ItemRef],
    by_id: dict[str, tuple[ExtractedItem, Evidence]],
    merged: MergeResult,
) -> MeetingAnalysis:
    requirements: list[Requirement] = []
    used: set[str] = set()

    def add_requirement(entry: MergedEntry, evidence: list[Evidence]) -> None:
        # A requirement whose quotes could not be found is never "confirmed".
        status = entry.status
        if status == "confirmed" and not any(e.verified for e in evidence):
            status = "unclear"
        requirements.append(
            Requirement(
                id=f"REQ-{len(requirements) + 1:03d}",
                description=entry.statement,
                status=status,
                priority=entry.priority,
                origin=entry.origin,
                evidence=evidence,
            )
        )

    for entry in merged.requirements:
        evidence = _evidence(entry, by_id)
        if not evidence:
            continue  # the merge invented it: no extracted item supports it
        used.update(rid for rid in entry.item_ids if rid in by_id)
        add_requirement(entry, evidence)

    # Coverage guarantee: a requirement the merge forgot comes back as "unclear".
    for rid, item, ev in refs:
        if item.kind == "requirement" and rid not in used:
            add_requirement(
                MergedEntry(
                    statement=item.statement,
                    item_ids=[rid],
                    status="unclear",
                    priority=item.priority,
                    origin=item.origin,
                ),
                [ev],
            )

    def notes(entries: list[MergedEntry]) -> list[Note]:
        return [
            Note(statement=e.statement, evidence=ev) for e in entries if (ev := _evidence(e, by_id))
        ]

    return MeetingAnalysis(
        meeting_ids=meeting_ids,
        requirements=requirements,
        decisions=notes(merged.decisions),
        constraints=notes(merged.constraints),
        open_questions=notes(merged.open_questions),
        commitments=notes(merged.commitments),
    )
