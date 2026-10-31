"""Second-stage ranking with a local cross-encoder (free, runs on the CPU).

The first stage (hybrid search) is fast but judges the question and each passage
separately. A cross-encoder reads the question and the passage together, so it ranks the
~20 candidates more precisely, and its score says how well a passage answers the question,
which is a better signal for the evidence gate than cosine similarity.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol


class Reranker(Protocol):
    model_name: str

    def score(self, query: str, passages: Sequence[str]) -> list[float]: ...


class CrossEncoderReranker:
    def __init__(self, model_name: str, cache_dir: Path) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # lazy: slow import

        self.model_name = model_name
        self._model = TextCrossEncoder(model_name, cache_dir=str(cache_dir))

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        return [float(s) for s in self._model.rerank(query, list(passages), batch_size=32)]
