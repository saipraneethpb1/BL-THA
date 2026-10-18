"""Query rewriting: translate customer wording into help-center wording before searching.

Why: "hide my site while we finish building it" never mentions "Staging Mode", so neither
keyword nor vector search found the right article. A small model rewrites the question
into 2-3 searches in product vocabulary; search runs on the original plus the rewrites
and merges the results. If the model fails, search falls back to the original question.
"""

import logging
from collections import OrderedDict
from pathlib import Path
from typing import Protocol

import anthropic
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


class Rewrites(BaseModel):
    queries: list[str] = Field(description="2-3 short search queries in help-center vocabulary")


class QueryRewriter(Protocol):
    async def rewrite(self, question: str) -> list[str]: ...


class ClaudeQueryRewriter:
    def __init__(
        self, model: str, prompt_version: str = "rewrite_v1", cache_size: int = 512
    ) -> None:
        self.client = anthropic.AsyncAnthropic(max_retries=2, timeout=20.0)
        self.model = model
        self.system = (PROMPTS / f"{prompt_version}.md").read_text(encoding="utf-8")
        self._cache: OrderedDict[str, list[str]] = OrderedDict()
        self._cache_size = cache_size
        self.input_tokens = 0
        self.output_tokens = 0

    async def rewrite(self, question: str) -> list[str]:
        key = " ".join(question.lower().split())
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        try:
            r = await self.client.messages.parse(
                model=self.model,
                max_tokens=300,
                system=[
                    {"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": f"<question>{question}</question>"}],
                output_config={"effort": "low"},
                output_format=Rewrites,
            )
        except anthropic.APIError as exc:  # search still works on the original question
            log.warning("query rewrite failed, using the original question: %s", exc)
            return []
        self.input_tokens += r.usage.input_tokens
        self.output_tokens += r.usage.output_tokens
        queries = [
            q.strip() for q in (r.parsed_output.queries if r.parsed_output else []) if q.strip()
        ][:3]
        self._cache[key] = queries
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return queries
