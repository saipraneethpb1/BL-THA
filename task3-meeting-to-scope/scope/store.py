"""Projects, meetings and scope versions in SQLite, with an audit trail.

A version moves: building -> draft -> approved (by a named person) -> superseded,
or building -> failed. Only an approved version can be exported for the client.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import BaseModel

from scope.models import (
    Estimate,
    FeasibilityResult,
    MeetingAnalysis,
    ProposalText,
    ReviewIssue,
    ScopeDiff,
    ScopeOfWork,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    client TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    date TEXT NOT NULL,
    transcript TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    number INTEGER NOT NULL,
    status TEXT NOT NULL,           -- building | draft | approved | superseded | failed
    baseline_version INTEGER,       -- the approved version this one revises
    meeting_ids TEXT NOT NULL,      -- JSON: meetings included so far
    data TEXT,                      -- JSON VersionData
    issues TEXT NOT NULL DEFAULT '[]',
    usage TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    approved_by TEXT,
    approved_at TEXT,
    approval_note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, number)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    version_number INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class VersionData(BaseModel):
    analysis: MeetingAnalysis
    feasibility: list[FeasibilityResult]
    scope: ScopeOfWork
    estimate: Estimate
    proposal: ProposalText
    proposal_md: str
    diff: ScopeDiff | None = None


class Version(BaseModel):
    project_id: int
    number: int
    status: str
    baseline_version: int | None
    meeting_ids: list[str]
    data: VersionData | None
    issues: list[ReviewIssue]
    usage: dict[str, Any]
    error: str | None
    approved_by: str | None
    approved_at: str | None
    approval_note: str | None
    created_at: str


class StateError(RuntimeError):
    """An action that the version's current state does not allow."""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def init(self) -> None:
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.db() as db:
            await db.executescript(SCHEMA)
            await db.commit()

    @asynccontextmanager
    async def db(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            yield conn

    async def audit(
        self,
        db: aiosqlite.Connection,
        project_id: int,
        number: int | None,
        action: str,
        actor: str,
        detail: str = "",
    ) -> None:
        await db.execute(
            "INSERT INTO audit(project_id, version_number, action, actor, detail) VALUES (?, ?, ?, ?, ?)",
            (project_id, number, action, actor, detail),
        )

    # ----- projects and meetings -----

    async def create_project(self, name: str, client: str) -> int:
        async with self.db() as db:
            cur = await db.execute(
                "INSERT INTO projects(name, client) VALUES (?, ?)", (name, client)
            )
            pid = int(cur.lastrowid or 0)
            await self.audit(db, pid, None, "project_created", "user", name)
            await db.commit()
            return pid

    async def projects(self) -> list[dict[str, Any]]:
        async with self.db() as db:
            rows = await db.execute_fetchall(
                """SELECT p.*, (SELECT count(*) FROM meetings m WHERE m.project_id = p.id) AS meetings,
                          (SELECT max(number) FROM versions v WHERE v.project_id = p.id) AS latest_version
                   FROM projects p ORDER BY p.id DESC"""
            )
        return [dict(r) for r in rows]

    async def project(self, pid: int) -> dict[str, Any] | None:
        async with self.db() as db:
            rows = list(await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (pid,)))
        return dict(rows[0]) if rows else None

    async def add_meeting(self, pid: int, title: str, date: str, transcript: str) -> str:
        async with self.db() as db:
            rows = list(
                await db.execute_fetchall(
                    "SELECT count(*) AS n FROM meetings WHERE project_id = ?", (pid,)
                )
            )
            mid = f"p{pid}-m{rows[0]['n'] + 1}"
            await db.execute(
                "INSERT INTO meetings(id, project_id, title, date, transcript) VALUES (?, ?, ?, ?, ?)",
                (mid, pid, title, date, transcript),
            )
            await self.audit(db, pid, None, "meeting_added", "user", f"{mid} {title}")
            await db.commit()
            return mid

    async def meetings(self, pid: int) -> list[dict[str, Any]]:
        async with self.db() as db:
            rows = await db.execute_fetchall(
                "SELECT * FROM meetings WHERE project_id = ? ORDER BY date, created_at", (pid,)
            )
        return [dict(r) for r in rows]

    # ----- versions -----

    async def start_version(self, pid: int, meeting_ids: list[str], baseline: int | None) -> int:
        async with self.db() as db:
            rows = list(
                await db.execute_fetchall(
                    "SELECT status FROM versions WHERE project_id = ? AND status = 'building'",
                    (pid,),
                )
            )
            if rows:
                raise StateError("a version is already being built for this project")
            rows = list(
                await db.execute_fetchall(
                    "SELECT coalesce(max(number), 0) AS n FROM versions WHERE project_id = ?",
                    (pid,),
                )
            )
            number = int(rows[0]["n"]) + 1
            await db.execute(
                """INSERT INTO versions(project_id, number, status, baseline_version, meeting_ids)
                   VALUES (?, ?, 'building', ?, ?)""",
                (pid, number, baseline, json.dumps(meeting_ids)),
            )
            await self.audit(
                db,
                pid,
                number,
                "build_started",
                "system",
                f"baseline v{baseline}" if baseline else "initial",
            )
            await db.commit()
            return number

    async def finish_version(
        self,
        pid: int,
        number: int,
        data: VersionData,
        issues: list[ReviewIssue],
        usage: dict[str, Any],
    ) -> None:
        async with self.db() as db:
            # Older unapproved drafts are superseded; approved versions stay as history.
            await db.execute(
                "UPDATE versions SET status = 'superseded' WHERE project_id = ? AND status = 'draft'",
                (pid,),
            )
            await db.execute(
                "UPDATE versions SET status = 'draft', data = ?, issues = ?, usage = ? WHERE project_id = ? AND number = ?",
                (
                    data.model_dump_json(),
                    json.dumps([i.model_dump() for i in issues]),
                    json.dumps(usage),
                    pid,
                    number,
                ),
            )
            await self.audit(
                db,
                pid,
                number,
                "draft_ready",
                "system",
                f"{sum(i.severity == 'block' for i in issues)} blocking issue(s)",
            )
            await db.commit()

    async def usage(self) -> dict[str, Any]:
        """Claude spend recorded on built versions, and the latest build failure if it happened
        after the latest successful build (for the usage bar). Times come from the audit log,
        i.e. when a build finished or failed, not when it started."""

        def iso(t: str | None) -> str | None:
            return f"{t.replace(' ', 'T')}+00:00" if t else None

        async with self.db() as db:
            [r] = await db.execute_fetchall(
                """SELECT count(*) AS calls,
                          coalesce(sum(json_extract(usage, '$.cost_usd')), 0) AS usd,
                          coalesce(sum(CASE WHEN date(created_at) = date('now')
                                       THEN json_extract(usage, '$.cost_usd') END), 0) AS today
                   FROM versions WHERE status != 'building'"""
            )
            [ok] = await db.execute_fetchall(
                "SELECT max(created_at) AS at FROM audit WHERE action = 'draft_ready'"
            )
            failed = list(
                await db.execute_fetchall(
                    """SELECT a.created_at AS at, v.error AS error FROM audit a
                       JOIN versions v ON v.project_id = a.project_id AND v.number = a.version_number
                       WHERE a.action = 'build_failed' ORDER BY a.id DESC LIMIT 1"""
                )
            )
        last_error = None
        if failed and (ok["at"] is None or failed[0]["at"] >= ok["at"]):
            last_error = {"message": failed[0]["error"], "at": iso(failed[0]["at"])}
        return {
            "service": "scope",
            "total_usd": round(float(r["usd"]), 6),
            "today_usd": round(float(r["today"]), 6),
            "calls": r["calls"],
            "last_success_at": iso(ok["at"]),
            "last_error": last_error,
        }

    async def fail_version(
        self, pid: int, number: int, error: str, usage: dict[str, Any] | None = None
    ) -> None:
        async with self.db() as db:
            await db.execute(
                "UPDATE versions SET status = 'failed', error = ?, usage = ? "
                "WHERE project_id = ? AND number = ?",
                (error, json.dumps(usage or {}), pid, number),
            )
            await self.audit(db, pid, number, "build_failed", "system", error[:500])
            await db.commit()

    async def version(self, pid: int, number: int) -> Version | None:
        async with self.db() as db:
            rows = list(
                await db.execute_fetchall(
                    "SELECT * FROM versions WHERE project_id = ? AND number = ?", (pid, number)
                )
            )
        return _version(rows[0]) if rows else None

    async def versions(self, pid: int) -> list[Version]:
        async with self.db() as db:
            rows = await db.execute_fetchall(
                "SELECT * FROM versions WHERE project_id = ? ORDER BY number DESC", (pid,)
            )
        return [_version(r) for r in rows]

    async def latest_approved(self, pid: int) -> Version | None:
        async with self.db() as db:
            rows = list(
                await db.execute_fetchall(
                    "SELECT * FROM versions WHERE project_id = ? AND status = 'approved' ORDER BY number DESC LIMIT 1",
                    (pid,),
                )
            )
        return _version(rows[0]) if rows else None

    async def approve(self, pid: int, number: int, actor: str, note: str | None) -> None:
        v = await self.version(pid, number)
        if v is None:
            raise StateError("version not found")
        if v.status != "draft":
            raise StateError(f"only a draft can be approved; v{number} is {v.status}")
        blocking = [i.message for i in v.issues if i.severity == "block"]
        if blocking and not note:
            raise StateError(
                "this draft has blocking issues; approving it needs a note explaining why"
            )
        async with self.db() as db:
            await db.execute(
                "UPDATE versions SET status = 'superseded' WHERE project_id = ? AND status = 'approved'",
                (pid,),
            )
            await db.execute(
                """UPDATE versions SET status = 'approved', approved_by = ?, approved_at = datetime('now'),
                   approval_note = ? WHERE project_id = ? AND number = ?""",
                (actor, note, pid, number),
            )
            await self.audit(db, pid, number, "approved", actor, note or "")
            await db.commit()

    async def mark_exported(self, pid: int, number: int, actor: str) -> None:
        async with self.db() as db:
            await self.audit(db, pid, number, "exported", actor)
            await db.commit()

    async def audit_log(self, pid: int) -> list[dict[str, Any]]:
        async with self.db() as db:
            rows = await db.execute_fetchall(
                "SELECT * FROM audit WHERE project_id = ? ORDER BY id DESC LIMIT 100", (pid,)
            )
        return [dict(r) for r in rows]


def _version(r: aiosqlite.Row) -> Version:
    return Version(
        project_id=r["project_id"],
        number=r["number"],
        status=r["status"],
        baseline_version=r["baseline_version"],
        meeting_ids=json.loads(r["meeting_ids"]),
        data=VersionData.model_validate_json(r["data"]) if r["data"] else None,
        issues=[ReviewIssue.model_validate(i) for i in json.loads(r["issues"])],
        usage=json.loads(r["usage"]),
        error=r["error"],
        approved_by=r["approved_by"],
        approved_at=r["approved_at"],
        approval_note=r["approval_note"],
        created_at=r["created_at"],
    )
