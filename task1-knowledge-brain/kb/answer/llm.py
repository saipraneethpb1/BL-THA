"""The only place that talks to Claude for answering. Everything else depends on the
`Answerer` protocol, so tests and offline runs can swap in a fake."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from kb import claude

Effort = Literal["low", "medium", "high", "xhigh", "max"]

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

# USD per million tokens (input, output), Anthropic's list prices: used for recorded spend,
# budget caps and eval costs. Bedrock bills separately and its prices can differ.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


def price(model: str) -> tuple[float, float]:
    """Prices for a model setting, bare name or full Bedrock id."""
    return PRICES.get(claude.base_name(model), (0.0, 0.0))


class AnswerDraft(BaseModel):
    """What the model must return. Validated by the SDK against this schema."""

    status: Literal["answered", "partial", "insufficient_evidence"]
    answer: str
    cited_passages: list[int] = Field(description="Numbers of the passages the answer relies on")
    missing_info: str | None = Field(
        default=None, description="What the passages do not cover, if anything"
    )


@dataclass(frozen=True)
class PassageInput:
    number: int
    title: str
    section: str | None
    text: str


@dataclass
class LLMResult:
    draft: AnswerDraft
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int

    @property
    def cost_usd(self) -> float:
        price_in, price_out = price(self.model)
        return (self.input_tokens * price_in + self.output_tokens * price_out) / 1_000_000


class Answerer(Protocol):
    async def answer(self, question: str, passages: list[PassageInput]) -> LLMResult: ...


def render_user_message(question: str, passages: list[PassageInput]) -> str:
    blocks = [
        f'<passage number="{p.number}" title="{p.title}" section="{p.section or ""}">\n'
        f"{p.text}\n</passage>"
        for p in passages
    ]
    return "<passages>\n" + "\n".join(blocks) + f"\n</passages>\n\n<question>{question}</question>"


class ClaudeAnswerer:
    def __init__(
        self, model: str, prompt_version: str = "answer_v1", effort: Effort = "low"
    ) -> None:
        self.client = claude.client(max_retries=3, timeout=60.0)
        self.model = model
        self.prompt_version = prompt_version
        self.effort = effort
        self.system = (PROMPTS / f"{prompt_version}.md").read_text(encoding="utf-8")

    async def answer(self, question: str, passages: list[PassageInput]) -> LLMResult:
        response = await self.client.messages.parse(
            model=claude.model_id(self.model),
            max_tokens=4000,
            # The system prompt is identical on every call, so cache it.
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": render_user_message(question, passages)}],
            output_config=claude.output_config(self.model, self.effort),
            output_format=AnswerDraft,
        )
        draft = response.parsed_output
        if response.stop_reason == "refusal" or draft is None:
            draft = AnswerDraft(
                status="insufficient_evidence",
                answer="I can't answer this automatically; it has been passed to a human expert.",
                cited_passages=[],
                missing_info=f"model stopped with {response.stop_reason}",
            )
        return LLMResult(
            draft=draft,
            model=self.model,
            prompt_version=self.prompt_version,
            input_tokens=response.usage.input_tokens
            + (response.usage.cache_read_input_tokens or 0)
            + (response.usage.cache_creation_input_tokens or 0),
            output_tokens=response.usage.output_tokens,
        )
