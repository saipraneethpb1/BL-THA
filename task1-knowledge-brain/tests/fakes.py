"""Test doubles. A deterministic bag-of-words embedder: texts sharing words get similar
vectors, which is enough to test the plumbing without downloading a model."""

import hashlib
import re
from collections.abc import Sequence

import numpy as np

from kb.answer.llm import AnswerDraft, LLMResult, PassageInput
from kb.retrieval.embedder import Vector


class FakeEmbedder:
    dim = 64
    model_name = "fake-bag-of-words"
    input_type = "none"

    def _one(self, text: str) -> Vector:
        v = np.zeros(self.dim, dtype=np.float32)
        for word in re.findall(r"\w+", text.lower()):
            v[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim] += 1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_documents(self, texts: Sequence[str]) -> Vector:
        return np.array([self._one(t) for t in texts], dtype=np.float32).reshape(-1, self.dim)

    def embed_query(self, text: str) -> Vector:
        return self._one(text)


class FakeAnswerer:
    """Returns a scripted AnswerDraft and records what it was asked."""

    def __init__(self, draft: AnswerDraft | None = None) -> None:
        self.draft = draft or AnswerDraft(
            status="answered", answer="Go to Finance.", cited_passages=[1]
        )
        self.calls: list[tuple[str, int]] = []

    async def answer(self, question: str, passages: list[PassageInput]) -> LLMResult:
        self.calls.append((question, len(passages)))
        return LLMResult(self.draft, "fake-model", "answer_v1", input_tokens=100, output_tokens=20)
