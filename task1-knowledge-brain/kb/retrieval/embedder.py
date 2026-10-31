"""Text embeddings, computed locally.

Claude has no embedding model, so we run a small open model (bge-small, 384 dims) on
the CPU: no second API key, no per-call cost, and the same vectors on every machine.
Larger open models (e5-large-v2, bge-m3) are registered below for the embedding bake-off;
they also run locally through ONNX, at no cost.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import numpy as np
import numpy.typing as npt

Vector = npt.NDArray[np.float32]


class Embedder(Protocol):
    dim: int
    model_name: str
    input_type: str  # how inputs are prepared, stored with the index (see index_meta)

    def embed_documents(self, texts: Sequence[str]) -> Vector: ...
    def embed_query(self, text: str) -> Vector: ...


@dataclass(frozen=True)
class CustomModel:
    """An open ONNX model that fastembed does not ship, loaded from Hugging Face."""

    hf_repo: str
    dim: int
    pooling: Literal["mean", "cls"]
    model_file: str = "onnx/model.onnx"
    additional_files: tuple[str, ...] = ()
    query_prefix: str = ""
    passage_prefix: str = ""


CUSTOM_MODELS: dict[str, CustomModel] = {
    # English only, 512 tokens; trained with these prefixes and much worse without them.
    "intfloat/e5-large-v2": CustomModel(
        "intfloat/e5-large-v2", 1024, "mean", query_prefix="query: ", passage_prefix="passage: "
    ),
    # int8 builds of the same weights: 3-4x faster on a CPU, what a small server can run.
    "intfloat/e5-large-v2-int8": CustomModel(
        "Xenova/e5-large-v2",
        1024,
        "mean",
        model_file="onnx/model_int8.onnx",
        query_prefix="query: ",
        passage_prefix="passage: ",
    ),
    "BAAI/bge-m3-int8": CustomModel(
        "Xenova/bge-m3", 1024, "cls", model_file="onnx/model_int8.onnx"
    ),
    # Multilingual, 8,192 tokens; the dense vector is the normalised CLS token.
    "BAAI/bge-m3": CustomModel(
        "Xenova/bge-m3", 1024, "cls", additional_files=("onnx/model.onnx_data",)
    ),
}


class FastEmbedder:
    dim = 384

    def __init__(self, model_name: str, cache_dir: Path) -> None:
        from fastembed import TextEmbedding  # imported lazily: loading takes a few seconds

        self.model_name = model_name
        custom = CUSTOM_MODELS.get(model_name)
        self._query_prefix = custom.query_prefix if custom else ""
        self._passage_prefix = custom.passage_prefix if custom else ""
        if custom and model_name not in {m["model"] for m in TextEmbedding.list_supported_models()}:
            from fastembed.common.model_description import ModelSource, PoolingType

            TextEmbedding.add_custom_model(
                model=model_name,
                pooling=PoolingType.MEAN if custom.pooling == "mean" else PoolingType.CLS,
                normalization=True,
                sources=ModelSource(hf=custom.hf_repo),
                dim=custom.dim,
                model_file=custom.model_file,
                additional_files=list(custom.additional_files),
            )
        self._custom = custom is not None
        self._model = TextEmbedding(model_name, cache_dir=str(cache_dir))
        self.dim = self._model.embedding_size
        self.input_type = (
            f"prefixes query={self._query_prefix!r} passage={self._passage_prefix!r}"
            if self._custom
            else "fastembed default"
        )

    def embed_documents(self, texts: Sequence[str]) -> Vector:
        inputs = [self._passage_prefix + t for t in texts]
        vecs = np.array(
            list(self._model.embed(inputs, batch_size=16 if self._custom else 64)), dtype=np.float32
        )
        return _normalise(vecs)

    def embed_query(self, text: str) -> Vector:
        if self._custom:
            vecs = np.array(list(self._model.embed([self._query_prefix + text])), dtype=np.float32)
        else:
            # fastembed adds the model's own query instruction where the model has one.
            vecs = np.array(list(self._model.query_embed(text)), dtype=np.float32)
        first: Vector = _normalise(vecs)[0]
        return first


def make_embedder(model_name: str, cache_dir: Path) -> Embedder:
    """The embedder for a model name. Every model here runs locally, free."""
    return FastEmbedder(model_name, cache_dir)


def _normalise(vecs: Vector) -> Vector:
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return (vecs / np.maximum(norms, 1e-12)).astype(np.float32)
