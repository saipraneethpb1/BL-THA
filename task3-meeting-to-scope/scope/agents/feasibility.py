"""Product Feasibility agent: for each requirement, ask the Knowledge Brain whether the
product already does it, then judge: existing setting, partial, custom work, or unclear.

Rule enforced in code: an "existing" verdict needs evidence. If the Knowledge Brain did
not answer, or is down, the verdict cannot be existing_configuration/with_limitation.
"""

import asyncio

from scope.knowledge import KBAnswer, KnowledgeClient, KnowledgeUnavailableError
from scope.llm import Effort, StructuredLLM, load_prompt
from scope.models import FeasibilityJudgement, FeasibilityResult, KnowledgeSource, Requirement


def kb_question(req: Requirement) -> str:
    return (
        f"Does the Brilliant Directories platform already support this, through a setting, "
        f"feature or add-on? Requirement: {req.description}"
    )


class FeasibilityAgent:
    def __init__(
        self,
        llm: StructuredLLM,
        kb: KnowledgeClient,
        model: str,
        concurrency: int = 4,
        effort: Effort = "low",
    ) -> None:
        self.llm = llm
        self.kb = kb
        self.model = model
        self.sem = asyncio.Semaphore(concurrency)
        self.effort: Effort = effort
        self.prompt = load_prompt("feasibility_v1")

    async def check(self, req: Requirement) -> FeasibilityResult:
        async with self.sem:
            try:
                answer: KBAnswer | None = await self.kb.query(kb_question(req))
            except KnowledgeUnavailableError:
                answer = None
            if answer is None:
                return FeasibilityResult(
                    requirement_id=req.id,
                    verdict="needs_clarification",
                    explanation="Could not check the product documentation (knowledge service unavailable).",
                    how_to=None,
                    limitation=None,
                    clarifying_question=None,
                    kb_status="unavailable",
                    kb_sources=[],
                )
            sources = "\n".join(f"- {s.title} ({s.url or 'expert answer'})" for s in answer.sources)
            user = (
                f"<requirement id='{req.id}' status='{req.status}'>{req.description}</requirement>\n\n"
                f"<knowledge_base status='{answer.status}'>\n{answer.answer}\n"
                f"Sources:\n{sources or 'none'}\n</knowledge_base>"
            )
            j = await self.llm.run(
                step="feasibility",
                model=self.model,
                system=self.prompt,
                user=user,
                schema=FeasibilityJudgement,
                effort=self.effort,
            )

        verdict = j.verdict
        explanation = j.explanation
        no_evidence = answer.status in ("insufficient_evidence", "conflict") or not answer.sources
        invalid_configuration = verdict == "existing_configuration" and answer.status != "answered"
        invalid_limitation = verdict == "existing_with_limitation" and (not j.limitation or j.limitation.casefold() not in answer.answer.casefold())
        if no_evidence or invalid_configuration or invalid_limitation:
            verdict = "needs_clarification"
            explanation = (
                "The product documentation does not confirm this feature, so it is not "
                f"offered as an existing setting. {explanation}"
            )
        return FeasibilityResult(
            requirement_id=req.id,
            verdict=verdict,
            explanation=explanation,
            how_to=j.how_to if verdict.startswith("existing") else None,
            limitation=j.limitation,
            clarifying_question=j.clarifying_question,
            kb_status=answer.status,
            kb_sources=[KnowledgeSource(title=s.title, url=s.url) for s in answer.sources],
        )

    async def check_all(self, reqs: list[Requirement]) -> list[FeasibilityResult]:
        return list(await asyncio.gather(*(self.check(r) for r in reqs)))
