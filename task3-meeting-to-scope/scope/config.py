from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SCOPE_", extra="ignore")

    db_path: Path = Path("data/scope.db")
    # A new demo account's workspace starts as a copy of this (the pre-built demo project).
    # (Default: starter.db next to the main database.)
    starter_db_path: Path | None = None
    kb_url: str = "http://localhost:8001"
    kb_api_key: str | None = None

    # Models per step. Starting points; the Task 3 evals decide the final choice.
    analyst_model: str = "claude-sonnet-4-6"
    feasibility_model: str = "claude-haiku-4-5"
    scope_model: str = "claude-sonnet-4-6"
    estimate_model: str = "claude-sonnet-4-6"
    proposal_model: str = "claude-sonnet-4-6"
    changes_model: str = "claude-sonnet-4-6"

    # Commercial settings: pricing is arithmetic on these, never model output.
    hourly_rate: float = 35.0
    currency: str = "USD"
    contingency_pct: float = 15.0

    max_chunk_chars: int = 8000

    # Public demo guards (scope/guard.py): cap on recorded build spend (None = no cap), and
    # builds per visitor per hour.
    budget_usd: float | None = None
    build_limit_per_hour: int = 3


settings = Settings()
