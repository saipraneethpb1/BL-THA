"""One way to call Claude for structured output, behind a protocol so every agent can be
tested with a scripted fake and evaluated against other models."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, TypeVar

import anthropic
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
Effort = Literal["low", "medium", "high", "xhigh", "max"]
PROMPTS = Path(__file__).parent / "prompts"

PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    by_step: dict[str, float] = field(default_factory=dict)

    def add(self, step: str, model: str, input_tokens: int, output_tokens: int) -> None:
        price_in, price_out = PRICES.get(model, (0.0, 0.0))
        cost = (input_tokens * price_in + output_tokens * price_out) / 1_000_000
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd += cost
        self.by_step[step] = self.by_step.get(step, 0.0) + cost


class LLMRefusedError(RuntimeError):
    pass


class StructuredLLM(Protocol):
    async def run(
        self, *, step: str, model: str, system: str, user: str, schema: type[T], effort: Effort
    ) -> T: ...

    @property
    def usage(self) -> Usage: ...


def load_prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


class ClaudeLLM:
    def __init__(self) -> None:
        self.client = anthropic.AsyncAnthropic(max_retries=3, timeout=180.0)
        self._usage = Usage()

    @property
    def usage(self) -> Usage:
        return self._usage

    async def run(
        self, *, step: str, model: str, system: str, user: str, schema: type[T], effort: Effort
    ) -> T:
        response = await self.client.messages.parse(
            model=model,
            max_tokens=16000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort},
            output_format=schema,
        )
        u = response.usage
        self._usage.add(
            step,
            model,
            u.input_tokens
            + (u.cache_read_input_tokens or 0)
            + (u.cache_creation_input_tokens or 0),
            u.output_tokens,
        )
        if response.parsed_output is None:
            raise LLMRefusedError(
                f"{step}: no structured output (stop_reason={response.stop_reason})"
            )
        return response.parsed_output
