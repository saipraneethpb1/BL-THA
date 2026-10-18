"""Shared helpers for the eval scripts: timing stats, result files, markdown tables."""

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
    ordered = sorted(values)
    return statistics.median(ordered), ordered[
        min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))
    ]


def save(name: str, payload: dict[str, Any]) -> Path:
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS / f"{name}_{stamp}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def table(headers: list[str], rows: list[list[Any]]) -> str:
    def cell(v: Any) -> str:
        if isinstance(v, float):
            return f"{v:.0%}" if 0 <= v <= 1 else f"{v:,.2f}"
        return str(v)

    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)


def load_root_env() -> None:
    """Read ANTHROPIC_API_KEY from the repo's .env when it isn't already set (never printed)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    env = Path(__file__).resolve().parents[2] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ANTHROPIC_API_KEY=") and line.split("=", 1)[1].strip():
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip()
