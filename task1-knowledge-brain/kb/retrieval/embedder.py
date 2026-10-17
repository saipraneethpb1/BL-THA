"""Text embeddings, computed locally.

Claude has no embedding model, so we run a small open model (bge-small, 384 dims) on
the CPU: no second API key, no per-call cost, and the same vectors on every machine.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np
import numpy.typing as npt

Vector = npt.NDArray[np.float32]


class Embedder(Protocol):
    dim: int

    def embed_documents(self, texts: Sequence[str]) -> Vector: ...
    def embed_query(self, text: str) -> Vector: ...


class FastEmbedder:
    dim = 384

    def __init__(self, model_name: str, cache_dir: Path) -> None:
        from fastembed import TextEmbedding  # imported lazily: loading takes a few seconds

        self._model = TextEmbedding(model_name, cache_dir=str(cache_dir))

    def embed_documents(self, texts: Sequence[str]) -> Vector:
        vecs = np.array(list(self._model.embed(list(texts), batch_size=64)), dtype=np.float32)
        return _normalise(vecs)

    def embed_query(self, text: str) -> Vector:
        # bge models retrieve better when queries carry this instruction prefix.
        vecs = np.array(list(self._model.query_embed(text)), dtype=np.float32)
        first: Vector = _normalise(vecs)[0]
        return first


def _normalise(vecs: Vector) -> Vector:
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return (vecs / np.maximum(norms, 1e-12)).astype(np.float32)
