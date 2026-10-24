"""All settings come from environment variables (or a .env file), never from code."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="KB_", extra="ignore")

    db_path: Path = Path("data/knowledge.db")
    sitemap_url: str = "https://support.brilliantdirectories.com/support/sitemap.xml"
    # Custom sitemaps (kb/ingestion/sources.py). Every nested sitemap is followed; these are
    # safety ceilings for a small server, and the preview says so if a sitemap reaches one.
    max_custom_sitemaps: int = 5
    custom_sitemap_max_pages: int = 5000
    custom_sitemap_max_children: int = 500
    # How often the help center is re-checked by default: 1, 7, 15 or 30 days.
    helpcenter_recheck_days: int = 1
    # For the time estimate before an ingest: embedding one page on this server (measured
    # on a 2-vCPU instance, about 9 passages a page).
    embed_s_per_page: float = 0.7

    # Politeness: the help center allows ~300 requests per window; we stay far below it.
    max_concurrency: int = 2
    min_interval_s: float = 1.0
    max_attempts: int = 4
    request_timeout_s: float = 20.0
    user_agent: str = "BusinessLabs-KnowledgeBrain/0.1 (capstone project; polite crawler)"

    # Retrieval
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    model_cache_dir: Path = Path("data/models")

    # Answering. Opus 5.5 passed 88% of the answer eval vs Sonnet 81% at ~2 cents a question.
    answer_model: str = "claude-opus-5-5"  # chosen on the 32-question answer eval (see EVALS.md)
    answer_prompt_version: str = "answer_v1"
    answer_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    # Below this cosine score we do not ask the model at all (calibrated on the eval set:
    # real questions scored >= 0.68, unrelated ones <= 0.56).
    min_evidence_score: float = 0.60
    # Rephrase questions into help-center vocabulary before searching (needs the API key).
    query_rewrite: bool = True
    rewrite_model: str = "claude-haiku-5-5"

    # Screenshots. Haiku is the starting point; the vision eval compares it with Sonnet.
    vision_model: str = "claude-haiku-5-5"

    # Daily refresh, in UTC. Set refresh_enabled=false to run refreshes only on demand.
    refresh_enabled: bool = True
    refresh_hour_utc: int = 2

    # First start (see kb/bootstrap.py): snapshot file, snapshot URL, else crawl.
    seed_path: Path | None = None
    seed_url: str | None = None
    bootstrap_crawl: bool = True

    # Speech to text for the mic buttons (kb/speech.py): a local Whisper model, no external
    # service. `base` is multilingual (Hindi works) and fast enough on 2 vCPUs.
    speech_enabled: bool = True
    whisper_model: str = "base"
    # Load the model in the background at start-up, so the first dictation isn't a 20 s wait.
    speech_preload: bool = True
    max_audio_bytes: int = 10_000_000

    # Public demo guards (kb/guard.py). budget_usd caps total recorded spend on answers;
    # None = no cap. Rate limits apply per visitor (requests that came through the proxy).
    budget_usd: float | None = None
    query_limit_per_10min: int = 20
    transcribe_limit_per_10min: int = 30
    refresh_limit_per_hour: int = 2

    # Shared secret for callers (Tasks 2 and 3). None = no auth, for local development only.
    api_key: str | None = None


settings = Settings()
