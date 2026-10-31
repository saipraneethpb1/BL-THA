"""Local, lazy ONNX entailment validation of draft sentences."""

import re
import threading
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from kb.config import settings


class Entailment(Protocol):
    def supports(self, premise: str, claim: str) -> bool: ...


class LocalEntailment:
    def __init__(self, cache_dir: Path = Path("data/models")) -> None:
        self.cache_dir = cache_dir
        self.session: Any = None
        self.lock = threading.Lock()

    def supports(self, premise: str, claim: str) -> bool:
        with self.lock:
            return self._supports(premise, claim)

    def _supports(self, premise: str, claim: str) -> bool:
        if self.session is None:
            import json

            import onnxruntime as ort  # type: ignore[import-untyped]
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer

            def download(name: str) -> str:
                return hf_hub_download(
                    "Xenova/nli-deberta-v3-small", name, cache_dir=self.cache_dir
                )

            self.tokenizer = Tokenizer.from_file(download("tokenizer.json"))
            self.tokenizer.enable_truncation(max_length=512, strategy="only_first")
            config = json.loads(Path(download("config.json")).read_text())
            self.entailment_id = config["label2id"]["entailment"]
            self.session = ort.InferenceSession(
                download("onnx/model_quantized.onnx"), providers=["CPUExecutionProvider"]
            )
        # Never truncate the claim itself: that could validate only half an assertion.
        if len(self.tokenizer.encode(claim).ids) >= 500:
            return False
        encoded = self.tokenizer.encode(premise, claim)
        values = {
            "input_ids": encoded.ids,
            "attention_mask": encoded.attention_mask,
            "token_type_ids": encoded.type_ids,
        }
        logits = self.session.run(
            None,
            {i.name: np.array([values[i.name]], dtype=np.int64) for i in self.session.get_inputs()},
        )[0][0]
        probabilities = np.exp(logits - np.max(logits))
        probabilities /= probabilities.sum()
        return bool(probabilities[self.entailment_id] >= 0.8)


def check_claims(
    answer: str, passages: list[str], model: Entailment
) -> tuple[list[str], list[str]]:
    claims = [c.strip() for c in re.split(r"(?<=[.!?])\s+|\n+", answer) if c.strip()]
    supported: list[str] = []
    missing: list[str] = []
    for claim in claims:
        target = supported if any(model.supports(p, claim) for p in passages) else missing
        target.append(claim)
    return supported, missing


local_entailment = LocalEntailment(settings.model_cache_dir)
