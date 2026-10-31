"""Answering and query rewriting with an open-weight model on this machine (Ollama).

The free stack: no API key, no per-call cost, and no question or passage leaves the
server. Both classes fit the same protocols as the Claude ones (`Answerer`,
`QueryRewriter`), so search, the evidence gate and the citation check are unchanged.

Small local models break the JSON format more often than Claude does, so every reply is
validated against the same pydantic schema and retried; if it still fails, the answer is
a refusal (and so an escalation), never unchecked text.
"""

import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from kb.answer.llm import AnswerDraft, LLMResult, PassageInput, render_user_message
from kb.retrieval.rewrite import Rewrites

log = logging.getLogger(__name__)
PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
OLLAMA_URL = "http://localhost:11434"


class OllamaClient:
    def __init__(self, model: str, base_url: str = OLLAMA_URL, timeout_s: float = 300.0) -> None:
        self.model = model
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def chat_json(
        self, system: str, user: str, schema: type[BaseModel], attempts: int = 3
    ) -> tuple[BaseModel | None, int, int, str | None]:
        """(parsed reply or None, input tokens, output tokens, last error)."""
        tokens_in = tokens_out = 0
        error: str | None = None
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        for _ in range(attempts):
            try:
                r = await self._http.post(
                    "/api/chat",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "format": schema.model_json_schema(),  # constrained JSON output
                        "stream": False,
                        "options": {"temperature": 0},
                    },
                )
                r.raise_for_status()
            except httpx.HTTPError as exc:
                error = f"ollama: {exc}"
                continue
            body: dict[str, Any] = r.json()
            tokens_in += body.get("prompt_eval_count", 0)
            tokens_out += body.get("eval_count", 0)
            content = body.get("message", {}).get("content", "")
            try:
                return schema.model_validate_json(content), tokens_in, tokens_out, None
            except ValidationError as exc:  # validate and retry, telling the model why
                error = f"invalid JSON from {self.model}: {exc.errors()[:2]}"
                messages += [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": f"That reply did not match the schema: {error}. Reply again with valid JSON only.",
                    },
                ]
        return None, tokens_in, tokens_out, error


class OllamaAnswerer:
    def __init__(
        self, model: str, prompt_version: str = "answer_v1", base_url: str = OLLAMA_URL
    ) -> None:
        self.model = f"ollama:{model}"
        self.prompt_version = prompt_version
        self.system = (PROMPTS / f"{prompt_version}.md").read_text(encoding="utf-8")
        self._client = OllamaClient(model, base_url)

    async def answer(self, question: str, passages: list[PassageInput]) -> LLMResult:
        parsed, tokens_in, tokens_out, error = await self._client.chat_json(
            self.system, render_user_message(question, passages), AnswerDraft
        )
        draft = parsed if isinstance(parsed, AnswerDraft) else None
        if draft is None:
            log.warning("local answer failed: %s", error)
            draft = AnswerDraft(
                status="insufficient_evidence",
                answer="I can't answer this automatically; it has been passed to a human expert.",
                cited_passages=[],
                missing_info=error or "no valid reply from the local model",
            )
        return LLMResult(draft, self.model, self.prompt_version, tokens_in, tokens_out)


class OllamaQueryRewriter:
    """The query rewriter on a local model. Same prompt and cache as ClaudeQueryRewriter."""

    def __init__(
        self,
        model: str,
        prompt_version: str = "rewrite_v1",
        cache_size: int = 512,
        base_url: str = OLLAMA_URL,
    ) -> None:
        self.model = f"ollama:{model}"
        self.prompt_version = prompt_version
        self.system = (PROMPTS / f"{prompt_version}.md").read_text(encoding="utf-8")
        self._client = OllamaClient(model, base_url, timeout_s=60.0)
        self._cache: OrderedDict[str, list[str]] = OrderedDict()
        self._cache_size = cache_size
        self.input_tokens = 0
        self.output_tokens = 0

    def use_prompt(self, text: str, version: str) -> None:
        self.system, self.prompt_version = text, version
        self._cache.clear()

    async def rewrite(self, question: str) -> list[str]:
        key = " ".join(question.lower().split())
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        parsed, tokens_in, tokens_out, error = await self._client.chat_json(
            self.system, f"<question>{question}</question>", Rewrites, attempts=2
        )
        self.input_tokens += tokens_in
        self.output_tokens += tokens_out
        if not isinstance(parsed, Rewrites):  # search still works on the original question
            log.warning("local query rewrite failed, using the original question: %s", error)
            return []
        queries = [q.strip() for q in parsed.queries if q.strip()][:3]
        self._cache[key] = queries
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return queries
