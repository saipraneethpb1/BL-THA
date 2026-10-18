"""All settings come from environment variables (or a .env file), never from code."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="KB_", extra="ignore")

    db_path: Path = Path("data/knowledge.db")
    sitemap_url: str = "https://support.brilliantdirectories.com/support/sitemap.xml"

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

    # Shared secret for callers (Tasks 2 and 3). None = no auth, for local development only.
    api_key: str | None = None


settings = Settings()
