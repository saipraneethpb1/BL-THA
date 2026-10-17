"""All settings come from environment variables (or a .env file), never from code."""

from pathlib import Path

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


settings = Settings()
