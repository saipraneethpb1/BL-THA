"""Prompts as versioned data, so the agent can maintain its own.

Each prompt (today: "rewrite", the query rewriter) has numbered versions. Exactly one is
active. The agent writes candidates; the robustness check decides; a person can promote,
reject or roll back. Nothing is overwritten: every version, who made it, why, and how it
scored stays in the table. v1 is seeded from the file the prompt started as.
"""

import json
from pathlib import Path
from typing import Any

import aiosqlite

PROMPT_FILES = Path(__file__).resolve().parent.parent / "prompts"


class PromptError(ValueError):
    pass


async def ensure_seeded(db: aiosqlite.Connection, name: str, file_stem: str) -> None:
    rows = list(await db.execute_fetchall("SELECT 1 FROM prompt_versions WHERE name=?", (name,)))
    if not rows:
        text = (PROMPT_FILES / f"{file_stem}.md").read_text(encoding="utf-8")
        await db.execute(
            """INSERT INTO prompt_versions(name, version, text, status, author, rationale, decided_at)
               VALUES (?, 1, ?, 'active', 'written by hand', ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))""",
            (name, text, f"the starting prompt (kb/prompts/{file_stem}.md)"),
        )
        await db.commit()


def _row(r: aiosqlite.Row) -> dict[str, Any]:
    d = dict(r)
    d["evaluation"] = json.loads(d["evaluation"]) if d.get("evaluation") else None
    return d


async def active(db: aiosqlite.Connection, name: str) -> dict[str, Any]:
    rows = list(
        await db.execute_fetchall(
            "SELECT * FROM prompt_versions WHERE name=? AND status='active'", (name,)
        )
    )
    if not rows:
        raise PromptError(f"no active {name} prompt")
    return _row(rows[0])


async def get(db: aiosqlite.Connection, name: str, version: int) -> dict[str, Any]:
    rows = list(
        await db.execute_fetchall(
            "SELECT * FROM prompt_versions WHERE name=? AND version=?", (name, version)
        )
    )
    if not rows:
        raise PromptError(f"{name} v{version} doesn't exist")
    return _row(rows[0])


async def versions(db: aiosqlite.Connection, name: str) -> list[dict[str, Any]]:
    rows = await db.execute_fetchall(
        "SELECT * FROM prompt_versions WHERE name=? ORDER BY version DESC", (name,)
    )
    return [_row(r) for r in rows]


async def add_candidate(
    db: aiosqlite.Connection, name: str, text: str, author: str, based_on: int, rationale: str
) -> int:
    if any(v["status"] == "candidate" for v in await versions(db, name)):
        raise PromptError("a candidate is already waiting: promote or reject it first")
    rows = list(
        await db.execute_fetchall(
            "SELECT coalesce(max(version), 0) + 1 AS v FROM prompt_versions WHERE name=?", (name,)
        )
    )
    version = int(rows[0]["v"])
    await db.execute(
        """INSERT INTO prompt_versions(name, version, text, status, author, based_on, rationale)
           VALUES (?, ?, ?, 'candidate', ?, ?, ?)""",
        (name, version, text, author, based_on, rationale),
    )
    await db.commit()
    return version


async def save_evaluation(
    db: aiosqlite.Connection, name: str, version: int, evaluation: dict[str, Any]
) -> None:
    await db.execute(
        "UPDATE prompt_versions SET evaluation=? WHERE name=? AND version=?",
        (json.dumps(evaluation), name, version),
    )
    await db.commit()


async def activate(
    db: aiosqlite.Connection,
    name: str,
    version: int,
    by: str,
    note: str | None,
    override: bool = False,
) -> None:
    """`override`: a person makes live a version the robustness check turned down."""
    v = await get(db, name, version)
    if v["status"] == "active":
        return
    if v["status"] == "rejected" and not (override and v["decided_by"] == "robustness check"):
        raise PromptError(f"v{version} was rejected")
    await db.execute(
        "UPDATE prompt_versions SET status='retired' WHERE name=? AND status='active'", (name,)
    )
    await db.execute(
        """UPDATE prompt_versions SET status='active', decided_by=?, note=coalesce(?, note),
           decided_at=strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE name=? AND version=?""",
        (by, note, name, version),
    )
    await db.commit()


async def reject(db: aiosqlite.Connection, name: str, version: int, by: str, note: str) -> None:
    v = await get(db, name, version)
    if v["status"] != "candidate":
        raise PromptError(f"v{version} isn't a candidate")
    await db.execute(
        """UPDATE prompt_versions SET status='rejected', decided_by=?, note=?,
           decided_at=strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE name=? AND version=?""",
        (by, note, name, version),
    )
    await db.commit()


async def rollback(db: aiosqlite.Connection, name: str, by: str, note: str) -> int:
    """Back to the version that was active before the current one."""
    current = await active(db, name)
    rows = list(
        await db.execute_fetchall(
            """SELECT version FROM prompt_versions WHERE name=? AND status='retired'
               AND version < ? ORDER BY version DESC LIMIT 1""",
            (name, current["version"]),
        )
    )
    if not rows:
        raise PromptError("there's no earlier version to roll back to")
    await activate(db, name, int(rows[0]["version"]), by, f"rolled back: {note}")
    return int(rows[0]["version"])
