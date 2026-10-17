from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SCOPE_", extra="ignore")

    db_path: Path = Path("data/scope.db")
    kb_url: str = "http://localhost:8001"
    kb_api_key: str | None = None

    # Models per step. Starting points; the Task 3 evals decide the final choice.
    analyst_model: str = "claude-sonnet-5-5"
    feasibility_model: str = "claude-haiku-5-5"
    scope_model: str = "claude-sonnet-5-5"
    estimate_model: str = "claude-sonnet-5-5"
    proposal_model: str = "claude-sonnet-5-5"
    changes_model: str = "claude-sonnet-5-5"

    # Commercial settings: pricing is arithmetic on these, never model output.
    hourly_rate: float = 35.0
    currency: str = "USD"
    contingency_pct: float = 15.0

    max_chunk_chars: int = 8000


settings = Settings()
