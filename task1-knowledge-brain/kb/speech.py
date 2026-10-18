"""Speech to text for the mic buttons, on our own server.

Browsers' built-in dictation sends audio to Google or Microsoft and fails behind many VPNs,
in Brave and similar browsers, and doesn't exist in Firefox. This runs an open-source Whisper
model locally instead: no third-party service, no per-use cost, the same in every browser.
"""

import io
import threading
from pathlib import Path
from typing import Protocol

# Nudges Whisper towards this product's words ("site", not "sight"). Used only for English speech.
VOCABULARY = (
    "Brilliant Directories website: members, listings, admin dashboard, the site, "
    "search results, categories, refund, invoice, email."
)


class Transcriber(Protocol):
    def transcribe(self, audio: bytes, language: str | None) -> tuple[str, str]:
        """Returns (text, detected language code)."""
        ...


class WhisperTranscriber:
    """faster-whisper on CPU with 8-bit weights. The model (~145 MB for `base`) downloads on
    first use into the model cache, so the service starts as fast as before."""

    def __init__(self, model: str, cache_dir: Path, threads: int = 2) -> None:
        self.model_name = model
        self.cache_dir = cache_dir
        self.threads = threads
        self._model: object | None = None
        self._lock = threading.Lock()  # one transcription at a time on a small VM

    def _load(self) -> object:
        if self._model is None:
            from faster_whisper import WhisperModel  # heavy import, only when first used

            self._model = WhisperModel(
                self.model_name,
                device="cpu",
                compute_type="int8",
                cpu_threads=self.threads,
                download_root=str(self.cache_dir / "whisper"),
            )
        return self._model

    def warm(self) -> None:
        """Download and load the model now (called in the background at start-up)."""
        with self._lock:
            self._load()

    def transcribe(self, audio: bytes, language: str | None) -> tuple[str, str]:
        with self._lock:
            model = self._load()
            lang = language
            if lang is None:
                # Detect first (segments are lazy, so this costs little), so the English
                # vocabulary hint can't pull Hindi or Spanish speech towards English.
                _, detected = model.transcribe(io.BytesIO(audio), beam_size=1, vad_filter=True)  # type: ignore[attr-defined]
                lang = detected.language
            segments, info = model.transcribe(  # type: ignore[attr-defined]
                io.BytesIO(audio),
                language=lang,
                beam_size=1,
                vad_filter=True,
                initial_prompt=VOCABULARY if lang == "en" else None,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        return text, str(info.language)
