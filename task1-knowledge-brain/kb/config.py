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
    # Optional local cross-encoder that re-orders the best 20 passages (kb/retrieval/rerank.py),
    # scoring the first `rerank_chars` characters of each. None = off. See EVALS.md.
    reranker_model: str | None = None
    rerank_chars: int | None = 1000

    # Answering. Models are the ones the Amazon Bedrock account can use (kb/claude.py): a bare
    # name or a full Bedrock id such as "au.anthropic.claude-opus-4-6-v1". The answer eval
    # chose Opus 5.5 (88% vs Sonnet 5.5 81%); Opus 4.6 is the Opus on this account and has not
    # been through that eval yet. "ollama:<model>" answers with a local open-weight model.
    answer_model: str = "claude-opus-4-6"
    answer_prompt_version: str = "answer_v1"
    # Opus/Sonnet 4.6 take low, medium, high or max (xhigh needs Opus 4.7 or later).
    answer_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    # Below this cosine score we do not ask the model at all. Recalibrated for bge-small on
    # the 147-case retrieval eval (EVALS.md): 0.60 let 59% of the must-refuse dev questions
    # through; 0.66 lets 36% through and refuses 1% of answerable ones (held-out: 56%, 0%).
    # Recalibrate whenever the embedding model changes: cosine scales differ per model.
    min_evidence_score: float = 0.66
    # Rephrase questions into help-center vocabulary before searching (needs Claude: kb/claude.py).
    query_rewrite: bool = True
    rewrite_model: str = "claude-haiku-4-5"  # or "ollama:<model>" (local, free)
    # Skip the rewrite when the question as asked already finds a passage this similar
    # (dev: 20% of rewrites skipped, no recall lost). None = always rewrite.
    rewrite_skip_above: float | None = 0.80
    # Self-directed prompt engineering (kb/tuning): the model that rewrites the rewrite
    # prompt, and how often the agent checks and improves it on its own (0 = only on demand).
    prompt_writer_model: str = "claude-sonnet-4-6"
    prompt_tune_days: int = 7

    # Screenshots. Haiku is the starting point; the vision eval compares it with Sonnet.
    vision_model: str = "claude-haiku-4-5"

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
