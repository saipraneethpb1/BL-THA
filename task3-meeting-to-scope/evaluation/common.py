"""Shared helpers for the Task 3 eval scripts."""

import json
import os
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RESULTS = Path(__file__).parent / "results"


def p50_p95(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    s = sorted(values)
    return statistics.median(s), s[min(len(s) - 1, round(0.95 * (len(s) - 1)))]


def save(name: str, payload: dict[str, Any]) -> Path:
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS / f"{name}_{stamp}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def load_root_env() -> None:
    """Read ANTHROPIC_API_KEY and KB_API_KEY from the repo's .env if unset (never printed)."""
    env = Path(__file__).resolve().parents[2] / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        for name in ("ANTHROPIC_API_KEY", "KB_API_KEY"):
            if line.startswith(f"{name}=") and not os.environ.get(name):
                value = line.split("=", 1)[1].strip()
                if value:
                    os.environ[name] = value


def kb_key(configured: str | None) -> str | None:
    return configured or os.environ.get("KB_API_KEY")
