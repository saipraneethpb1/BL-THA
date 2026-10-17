"""Human-in-the-loop: unanswered questions become tasks; expert answers become knowledge."""

import json
import re
from dataclasses import dataclass

import aiosqlite

from kb.retrieval.embedder import Embedder


def normalize(question: str) -> str:
    return " ".join(re.findall(r"\w+", question.lower()))


@dataclass
class Escalation:
    id: int
    question: str
    status: str
    reason: str
    evidence: list[dict[str, object]]
    caller: str | None
    times_asked: int
    created_at: str
    resolved_at: str | None


def _row(r: aiosqlite.Row) -> Escalation:
    return Escalation(
        id=r["id"],
        question=r["question"],
        status=r["status"],
        reason=r["reason"],
        evidence=json.loads(r["evidence"]),
        caller=r["caller"],
        times_asked=r["times_asked"],
        created_at=r["created_at"],
        resolved_at=r["resolved_at"],
    )


async def open_escalation(
    db: aiosqlite.Connection,
    question: str,
    reason: str,
    evidence: list[dict[str, object]],
    caller: str | None,
) -> int:
    """Create a task for an expert, or bump the count on an identical open one.
    Counting repeats tells the expert which gaps hurt the most."""
    key = normalize(question)
    rows = list(
        await db.execute_fetchall(
            "SELECT id FROM escalations WHERE status='open' AND normalized=?", (key,)
        )
    )
    if rows:
        await db.execute(
            "UPDATE escalations SET times_asked = times_asked + 1 WHERE id=?", (rows[0]["id"],)
        )
        await db.commit()
        return int(rows[0]["id"])
    cur = await db.execute(
        """INSERT INTO escalations(question, normalized, reason, evidence, caller)
           VALUES (?, ?, ?, ?, ?)""",
        (question, key, reason, json.dumps(evidence), caller),
    )
    await db.commit()
    return int(cur.lastrowid or 0)


async def list_escalations(db: aiosqlite.Connection, status: str | None) -> list[Escalation]:
    if status:
        rows = await db.execute_fetchall(
            "SELECT * FROM escalations WHERE status=? ORDER BY times_asked DESC, id", (status,)
        )
    else:
        rows = await db.execute_fetchall("SELECT * FROM escalations ORDER BY id DESC")
    return [_row(r) for r in rows]


async def get_escalation(db: aiosqlite.Connection, escalation_id: int) -> Escalation | None:
    rows = list(await db.execute_fetchall("SELECT * FROM escalations WHERE id=?", (escalation_id,)))
    return _row(rows[0]) if rows else None


async def add_expert_answer(
    db: aiosqlite.Connection,
    embedder: Embedder,
    question: str,
    answer: str,
    author: str,
    escalation_id: int | None = None,
) -> int:
    """Store an approved answer as a new version and make it searchable at once.
    Earlier versions for the same question are deactivated, not deleted."""
    key = normalize(question)
    active = await db.execute_fetchall(
        "SELECT id, version, question FROM expert_answers WHERE active=1"
    )
    prev_same = [r for r in active if normalize(r["question"]) == key]
    version = max((r["version"] for r in prev_same), default=0) + 1
    for r in prev_same:
        await _deactivate(db, r["id"])

    cur = await db.execute(
        """INSERT INTO expert_answers(escalation_id, question, answer, author, version)
           VALUES (?, ?, ?, ?, ?)""",
        (escalation_id, question, answer, author, version),
    )
    answer_id = int(cur.lastrowid or 0)
    text = f"Expert answer: {question}\n\n{answer}"
    vector = embedder.embed_documents([text])[0]
    await db.execute(
        """INSERT INTO chunks(article_id, source, position, section, text, image_ids, embedding)
           VALUES (?, 'expert', 0, ?, ?, '[]', ?)""",
        (f"expert:{answer_id}", question, text, vector.tobytes()),
    )
    if escalation_id is not None:
        await db.execute(
            """UPDATE escalations SET status='resolved', resolved_at=datetime('now')
               WHERE id=?""",
            (escalation_id,),
        )
    await db.commit()
    return answer_id


async def _deactivate(db: aiosqlite.Connection, answer_id: int) -> None:
    await db.execute("UPDATE expert_answers SET active=0 WHERE id=?", (answer_id,))
    await db.execute("DELETE FROM chunks WHERE article_id=?", (f"expert:{answer_id}",))


async def retract_expert_answer(db: aiosqlite.Connection, answer_id: int) -> bool:
    rows = list(
        await db.execute_fetchall("SELECT active FROM expert_answers WHERE id=?", (answer_id,))
    )
    if not rows or not rows[0]["active"]:
        return False
    await _deactivate(db, answer_id)
    await db.commit()
    return True
