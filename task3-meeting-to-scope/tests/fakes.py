from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

from scope.knowledge import KBAnswer, KBSource, KnowledgeUnavailableError
from scope.llm import Effort, Usage

T = TypeVar("T", bound=BaseModel)

# Short synthetic transcript for unit tests only. Real meetings live in transcripts/.
TRANSCRIPT = """Kickoff call - Green Valley Vets
[00:00:05] Sam (Agency): Thanks for joining. What's not working with the current site?
[00:00:12] Priya (Client): Honestly the search is bad. Pet owners can't find a vet near them.
We get complaints every week.
[00:01:02] Priya (Client): We definitely need a map on the search results so people see clinics nearby.
[00:01:40] Sam (Agency): Got it. Anything about payments?
[00:01:55] Priya (Client): Yes, clinics must pay a yearly listing fee by card.
[00:02:30] Priya (Client): Our budget is around five thousand dollars and we want to launch by March.
[00:03:10] Sam (Agency): I'll send you a summary and questions by Friday.
"""


class FakeLLM:
    """Answers each call with a handler chosen by output schema."""

    def __init__(self, handlers: dict[type[BaseModel], Callable[[str], Any]]) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, str]] = []
        self._usage = Usage()

    @property
    def usage(self) -> Usage:
        return self._usage

    async def run(
        self, *, step: str, model: str, system: str, user: str, schema: type[T], effort: Effort
    ) -> T:
        self.calls.append((step, user))
        self._usage.add(step, model, 100, 20)
        result = self.handlers[schema](user)
        return schema.model_validate(result) if isinstance(result, dict) else result


class FakeKB:
    def __init__(self, answers: dict[str, KBAnswer] | None = None, down: bool = False) -> None:
        self.answers = answers or {}
        self.down = down
        self.questions: list[str] = []

    async def query(self, question: str) -> KBAnswer:
        self.questions.append(question)
        if self.down:
            raise KnowledgeUnavailableError("down")
        for key, ans in self.answers.items():
            if key.lower() in question.lower():
                return ans
        return KBAnswer(
            status="insufficient_evidence", answer="Not covered.", sources=[], needs_human=True
        )


def kb_answer(text: str, title: str = "Google Map Search Results") -> KBAnswer:
    return KBAnswer(
        status="answered",
        answer=text,
        needs_human=False,
        sources=[
            KBSource(title=title, url="https://help.test/map", source_type="article", section=None)
        ],
    )
