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

    # Answering. The model is a starting point; the answer eval decides the final choice.
    answer_model: str = "claude-sonnet-5-5"
    answer_prompt_version: str = "answer_v1"
    answer_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    # Below this cosine score we do not ask the model at all (calibrated on the eval set:
    # real questions scored >= 0.68, unrelated ones <= 0.56).
    min_evidence_score: float = 0.60

    # Shared secret for callers (Tasks 2 and 3). None = no auth, for local development only.
    api_key: str | None = None


settings = Settings()
