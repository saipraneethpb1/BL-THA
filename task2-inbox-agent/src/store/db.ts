/**
 * Persistence on Node's built-in SQLite: one file, no extra service, easy to inspect.
 * Every draft, review and autonomy change is kept, which is what lets us measure
 * whether the agent is getting better and audit why something was auto-sent.
 */
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";
import type { Violation } from "../drafting/policy.js";
import type { AutonomyEvent, AutonomyState, ReviewOutcome } from "../feedback/autonomy.js";
import type { CategoryId } from "../email/taxonomy.js";

const SCHEMA = `
CREATE TABLE IF NOT EXISTS emails (
  id TEXT PRIMARY KEY,
  from_name TEXT NOT NULL,
  from_email TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  thread TEXT NOT NULL DEFAULT '[]',
  received_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new', -- new | drafted | sent | archived | escalated | failed
  error TEXT
);
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email_id TEXT NOT NULL REFERENCES emails(id),
  classification TEXT NOT NULL,
  evidence TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  note_for_reviewer TEXT NOT NULL DEFAULT '',
  follow_up_in_days INTEGER,
  violations TEXT NOT NULL DEFAULT '[]',
  secrets_found INTEGER NOT NULL DEFAULT 0,
  auto_send_blocker TEXT,
  models TEXT NOT NULL,          -- JSON: classify/draft model + prompt versions
  cost_usd REAL NOT NULL DEFAULT 0,
  latency_ms REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email_id TEXT NOT NULL REFERENCES emails(id),
  draft_id INTEGER REFERENCES drafts(id),
  outcome TEXT NOT NULL,         -- sent_unchanged | edited | rejected | escalated | auto_sent | archived
  category TEXT NOT NULL,
  final_subject TEXT,
  final_body TEXT,
  reason TEXT,
  reviewer TEXT,
  spot_check INTEGER NOT NULL DEFAULT 0,  -- auto-sent and picked for a later human check
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS autonomy (
  kind TEXT PRIMARY KEY,
  streak INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'supervised',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS autonomy_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  event TEXT NOT NULL,
  detail TEXT NOT NULL,
  email_id TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- This inbox's own settings (auto-send threshold, which labels may auto-send), as JSON values.
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Simulated outbox: what would have been emailed. Real sending is out of scope.
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email_id TEXT NOT NULL,
  to_email TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  mode TEXT NOT NULL,            -- human | auto
  sent_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Guidance learned from human edits. One row = the complete set of lessons for a
-- category at one version, so rollback is "activate an earlier row", never an edit.
CREATE TABLE IF NOT EXISTS lesson_versions (
  category TEXT NOT NULL,
  version INTEGER NOT NULL,
  status TEXT NOT NULL,          -- candidate | active | retired | rejected
  lessons TEXT NOT NULL,         -- JSON: [{text, review_ids}]
  mined_from TEXT NOT NULL,      -- JSON: review ids the miner saw
  evaluation TEXT,               -- JSON: regression check result
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  decided_by TEXT,
  decided_at TEXT,
  note TEXT,
  PRIMARY KEY (category, version)
);
CREATE TABLE IF NOT EXISTS lesson_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category TEXT NOT NULL,
  version INTEGER NOT NULL,
  event TEXT NOT NULL,           -- proposed | evaluated | promoted | rejected | rolled_back
  actor TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
`;

export interface EmailRow {
  id: string;
  from_name: string;
  from_email: string;
  subject: string;
  body: string;
  thread: { from: string; date: string; body: string }[];
  received_at: string;
  status: string;
  error: string | null;
}

export interface DraftRow {
  id: number;
  email_id: string;
  classification: unknown;
  evidence: unknown;
  subject: string;
  body: string;
  note_for_reviewer: string;
  follow_up_in_days: number | null;
  violations: Violation[];
  secrets_found: boolean;
  auto_send_blocker: string | null;
  models: unknown;
  cost_usd: number;
  latency_ms: number;
  created_at: string;
}

type Row = Record<string, unknown>;

export interface Lesson {
  text: string;
  review_ids: number[];
}

export interface LessonVersion {
  category: CategoryId;
  version: number;
  status: "candidate" | "active" | "retired" | "rejected";
  lessons: Lesson[];
  mined_from: number[];
  evaluation: unknown;
  created_at: string;
  decided_by: string | null;
  decided_at: string | null;
  note: string | null;
}

/** A human decision with a final text: ground truth for mining and for checking lessons. */
export interface ReviewedCase {
  reviewId: number;
  emailId: string;
  outcome: string;
  reason: string | null;
  draftBody: string;
  finalBody: string;
  classification: unknown;
  evidence: unknown;
  lessonsVersion: number;
}

export class InboxStore {
  readonly db: DatabaseSync;

  constructor(path: string) {
    if (path !== ":memory:") mkdirSync(dirname(path), { recursive: true });
    this.db = new DatabaseSync(path);
    this.db.exec("PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON;");
    this.db.exec(SCHEMA);
  }

  addEmail(e: Omit<EmailRow, "status" | "error">): boolean {
    const r = this.db
      .prepare(
        `INSERT OR IGNORE INTO emails(id, from_name, from_email, subject, body, thread, received_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(e.id, e.from_name, e.from_email, e.subject, e.body, JSON.stringify(e.thread), e.received_at);
    return r.changes > 0;
  }

  getEmail(id: string): EmailRow | undefined {
    const r = this.db.prepare("SELECT * FROM emails WHERE id = ?").get(id) as Row | undefined;
    return r ? toEmail(r) : undefined;
  }

  listEmails(): (EmailRow & { category: string | null; urgency: string | null; blocker: string | null; sent_mode: string | null })[] {
    const rows = this.db
      .prepare(
        `SELECT e.*, json_extract(d.classification, '$.category') AS category,
                json_extract(d.classification, '$.urgency') AS urgency, d.auto_send_blocker AS blocker,
                (SELECT mode FROM outbox o WHERE o.email_id = e.id ORDER BY o.id DESC LIMIT 1) AS sent_mode
         FROM emails e LEFT JOIN drafts d ON d.id = (SELECT max(id) FROM drafts WHERE email_id = e.id)
         ORDER BY CASE json_extract(d.classification, '$.urgency')
                    WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,
                  e.received_at DESC`,
      )
      .all() as Row[];
    return rows.map((r) => ({
      ...toEmail(r),
      category: (r.category as string | null) ?? null,
      urgency: (r.urgency as string | null) ?? null,
      blocker: (r.blocker as string | null) ?? null,
      sent_mode: (r.sent_mode as string | null) ?? null,
    }));
  }

  /** Claude spend recorded on drafts (each includes its Knowledge Brain lookups). */
  usage(): { total_usd: number; today_usd: number; calls: number; last_success_at: string | null } {
    const r = this.db.prepare(
      `SELECT count(*) AS calls, coalesce(sum(cost_usd), 0) AS usd,
              coalesce(sum(CASE WHEN date(created_at) = date('now') THEN cost_usd END), 0) AS today,
              max(CASE WHEN cost_usd > 0 THEN created_at END) AS last_ok
       FROM drafts`,
    ).get() as { calls: number; usd: number; today: number; last_ok: string | null };
    return {
      total_usd: Number(r.usd), today_usd: Number(r.today), calls: Number(r.calls),
      last_success_at: r.last_ok ? `${r.last_ok.replace(" ", "T")}+00:00` : null,
    };
  }

  setEmailStatus(id: string, status: string, error: string | null = null): void {
    this.db.prepare("UPDATE emails SET status = ?, error = ? WHERE id = ?").run(status, error, id);
  }

  saveDraft(d: Omit<DraftRow, "id" | "created_at">): number {
    const r = this.db
      .prepare(
        `INSERT INTO drafts(email_id, classification, evidence, subject, body, note_for_reviewer,
           follow_up_in_days, violations, secrets_found, auto_send_blocker, models, cost_usd, latency_ms)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        d.email_id, JSON.stringify(d.classification), JSON.stringify(d.evidence), d.subject, d.body,
        d.note_for_reviewer, d.follow_up_in_days, JSON.stringify(d.violations), d.secrets_found ? 1 : 0,
        d.auto_send_blocker, JSON.stringify(d.models), d.cost_usd, d.latency_ms,
      );
    return Number(r.lastInsertRowid);
  }

  latestDraft(emailId: string): DraftRow | undefined {
    const r = this.db
      .prepare("SELECT * FROM drafts WHERE email_id = ? ORDER BY id DESC LIMIT 1")
      .get(emailId) as Row | undefined;
    return r ? toDraft(r) : undefined;
  }

  recordReview(r: {
    emailId: string;
    draftId: number | null;
    outcome: ReviewOutcome | "auto_sent" | "archived";
    category: CategoryId;
    finalSubject?: string | null;
    finalBody?: string | null;
    reason?: string | null;
    reviewer?: string | null;
    spotCheck?: boolean;
  }): number {
    const res = this.db
      .prepare(
        `INSERT INTO reviews(email_id, draft_id, outcome, category, final_subject, final_body, reason, reviewer, spot_check)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        r.emailId, r.draftId, r.outcome, r.category, r.finalSubject ?? null, r.finalBody ?? null,
        r.reason ?? null, r.reviewer ?? null, r.spotCheck ? 1 : 0,
      );
    return Number(res.lastInsertRowid);
  }

  reviewsFor(emailId: string): Row[] {
    return this.db.prepare("SELECT * FROM reviews WHERE email_id = ? ORDER BY id").all(emailId) as Row[];
  }

  pendingSpotChecks(): Row[] {
    return this.db
      .prepare(
        `SELECT r.* FROM reviews r WHERE r.outcome = 'auto_sent' AND r.spot_check = 1
         AND NOT EXISTS (SELECT 1 FROM reviews x WHERE x.email_id = r.email_id AND x.id > r.id)`,
      )
      .all() as Row[];
  }

  autonomy(kind: CategoryId): AutonomyState {
    const r = this.db.prepare("SELECT * FROM autonomy WHERE kind = ?").get(kind) as Row | undefined;
    return r
      ? { kind, streak: Number(r.streak), status: r.status as AutonomyState["status"] }
      : { kind, streak: 0, status: "supervised" };
  }

  allAutonomy(): AutonomyState[] {
    return (this.db.prepare("SELECT * FROM autonomy ORDER BY kind").all() as Row[]).map((r) => ({
      kind: r.kind as CategoryId,
      streak: Number(r.streak),
      status: r.status as AutonomyState["status"],
    }));
  }

  saveAutonomy(state: AutonomyState, events: AutonomyEvent[], emailId: string | null): void {
    this.db
      .prepare(
        `INSERT INTO autonomy(kind, streak, status, updated_at) VALUES (?, ?, ?, datetime('now'))
         ON CONFLICT(kind) DO UPDATE SET streak = excluded.streak, status = excluded.status,
         updated_at = excluded.updated_at`,
      )
      .run(state.kind, state.streak, state.status);
    const add = this.db.prepare(
      "INSERT INTO autonomy_events(kind, event, detail, email_id) VALUES (?, ?, ?, ?)",
    );
    for (const e of events) add.run(e.kind, e.event, e.detail, emailId);
  }

  /** An event that isn't about one label's state, e.g. a settings change ("settings"). */
  logAutonomyEvent(kind: string, event: string, detail: string): void {
    this.db.prepare("INSERT INTO autonomy_events(kind, event, detail, email_id) VALUES (?, ?, ?, NULL)").run(kind, event, detail);
  }

  autonomyEvents(limit = 50): Row[] {
    return this.db.prepare("SELECT * FROM autonomy_events ORDER BY id DESC LIMIT ?").all(limit) as Row[];
  }

  getSetting(key: string): unknown {
    const r = this.db.prepare("SELECT value FROM settings WHERE key = ?").get(key) as Row | undefined;
    return r ? JSON.parse(String(r.value)) : undefined;
  }

  setSetting(key: string, value: unknown): void {
    this.db
      .prepare(
        `INSERT INTO settings(key, value, updated_at) VALUES (?, ?, datetime('now'))
         ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`,
      )
      .run(key, JSON.stringify(value));
  }

  send(emailId: string, to: string, subject: string, body: string, mode: "human" | "auto"): void {
    this.db
      .prepare("INSERT INTO outbox(email_id, to_email, subject, body, mode) VALUES (?, ?, ?, ?, ?)")
      .run(emailId, to, subject, body, mode);
  }

  // ----- learning -----

  /** The lesson set drafts use now for a category (version 0 = no lessons yet). */
  activeLessons(category: CategoryId): { version: number; lessons: Lesson[] } {
    const r = this.db
      .prepare("SELECT version, lessons FROM lesson_versions WHERE category = ? AND status = 'active'")
      .get(category) as Row | undefined;
    return r
      ? { version: Number(r.version), lessons: JSON.parse(String(r.lessons)) as Lesson[] }
      : { version: 0, lessons: [] };
  }

  lessonVersions(category?: CategoryId): LessonVersion[] {
    const rows = (
      category
        ? this.db.prepare("SELECT * FROM lesson_versions WHERE category = ? ORDER BY version DESC").all(category)
        : this.db.prepare("SELECT * FROM lesson_versions ORDER BY category, version DESC").all()
    ) as Row[];
    return rows.map(toLessonVersion);
  }

  lessonVersion(category: CategoryId, version: number): LessonVersion | undefined {
    const r = this.db
      .prepare("SELECT * FROM lesson_versions WHERE category = ? AND version = ?")
      .get(category, version) as Row | undefined;
    return r ? toLessonVersion(r) : undefined;
  }

  addLessonVersion(category: CategoryId, lessons: Lesson[], minedFrom: number[], actor: string): number {
    const row = this.db
      .prepare("SELECT coalesce(max(version), 0) AS v FROM lesson_versions WHERE category = ?")
      .get(category) as Row;
    const version = Number(row.v) + 1;
    this.db
      .prepare(
        "INSERT INTO lesson_versions(category, version, status, lessons, mined_from) VALUES (?, ?, 'candidate', ?, ?)",
      )
      .run(category, version, JSON.stringify(lessons), JSON.stringify(minedFrom));
    this.lessonEvent(category, version, "proposed", actor, `${lessons.length} lesson(s) from ${minedFrom.length} correction(s)`);
    return version;
  }

  saveLessonEvaluation(category: CategoryId, version: number, evaluation: unknown, actor: string, detail: string): void {
    this.db
      .prepare("UPDATE lesson_versions SET evaluation = ? WHERE category = ? AND version = ?")
      .run(JSON.stringify(evaluation), category, version);
    this.lessonEvent(category, version, "evaluated", actor, detail);
  }

  /** Makes `version` the only active set for its category; the previous one is retired. */
  activateLessonVersion(
    category: CategoryId,
    version: number,
    actor: string,
    event: "promoted" | "rolled_back",
    note: string | null,
  ): void {
    this.db.prepare("UPDATE lesson_versions SET status = 'retired' WHERE category = ? AND status = 'active'").run(category);
    this.db
      .prepare(
        `UPDATE lesson_versions SET status = 'active', decided_by = ?, decided_at = datetime('now'), note = ?
         WHERE category = ? AND version = ?`,
      )
      .run(actor, note, category, version);
    this.lessonEvent(category, version, event, actor, note ?? "");
  }

  /** Rollback to "no lessons at all" (version 0). */
  deactivateLessons(category: CategoryId, actor: string, note: string): void {
    const current = this.activeLessons(category).version;
    this.db.prepare("UPDATE lesson_versions SET status = 'retired' WHERE category = ? AND status = 'active'").run(category);
    this.lessonEvent(category, current, "rolled_back", actor, `to no lessons: ${note}`);
  }

  rejectLessonVersion(category: CategoryId, version: number, actor: string, note: string): void {
    this.db
      .prepare(
        `UPDATE lesson_versions SET status = 'rejected', decided_by = ?, decided_at = datetime('now'), note = ?
         WHERE category = ? AND version = ?`,
      )
      .run(actor, note, category, version);
    this.lessonEvent(category, version, "rejected", actor, note);
  }

  lessonEvent(category: CategoryId, version: number, event: string, actor: string, detail: string): void {
    this.db
      .prepare("INSERT INTO lesson_events(category, version, event, actor, detail) VALUES (?, ?, ?, ?, ?)")
      .run(category, version, event, actor, detail);
  }

  lessonEvents(limit = 50): Row[] {
    return this.db.prepare("SELECT * FROM lesson_events ORDER BY id DESC LIMIT ?").all(limit) as Row[];
  }

  reviewedCases(category: CategoryId): ReviewedCase[] {
    const rows = this.db
      .prepare(
        `SELECT r.id AS review_id, r.outcome, r.reason, r.final_body, d.body AS draft_body,
                d.classification, d.evidence, d.models, r.email_id
         FROM reviews r JOIN drafts d ON d.id = r.draft_id
         WHERE r.category = ? AND r.final_body IS NOT NULL
           AND r.outcome IN ('sent_unchanged', 'edited', 'rejected')
         ORDER BY r.id`,
      )
      .all(category) as Row[];
    return rows.map((r) => ({
      reviewId: Number(r.review_id),
      emailId: String(r.email_id),
      outcome: String(r.outcome),
      reason: (r.reason as string | null) ?? null,
      draftBody: String(r.draft_body),
      finalBody: String(r.final_body),
      classification: JSON.parse(String(r.classification)),
      evidence: JSON.parse(String(r.evidence)),
      lessonsVersion: Number((JSON.parse(String(r.models)) as { lessons_version?: string }).lessons_version ?? 0),
    }));
  }
}

function toLessonVersion(r: Row): LessonVersion {
  return {
    category: r.category as CategoryId,
    version: Number(r.version),
    status: r.status as LessonVersion["status"],
    lessons: JSON.parse(String(r.lessons)) as Lesson[],
    mined_from: JSON.parse(String(r.mined_from)) as number[],
    evaluation: r.evaluation ? JSON.parse(String(r.evaluation)) : null,
    created_at: String(r.created_at),
    decided_by: (r.decided_by as string | null) ?? null,
    decided_at: (r.decided_at as string | null) ?? null,
    note: (r.note as string | null) ?? null,
  };
}

function toEmail(r: Row): EmailRow {
  return {
    id: String(r.id),
    from_name: String(r.from_name),
    from_email: String(r.from_email),
    subject: String(r.subject),
    body: String(r.body),
    thread: JSON.parse(String(r.thread)) as EmailRow["thread"],
    received_at: String(r.received_at),
    status: String(r.status),
    error: (r.error as string | null) ?? null,
  };
}

function toDraft(r: Row): DraftRow {
  return {
    id: Number(r.id),
    email_id: String(r.email_id),
    classification: JSON.parse(String(r.classification)),
    evidence: JSON.parse(String(r.evidence)),
    subject: String(r.subject),
    body: String(r.body),
    note_for_reviewer: String(r.note_for_reviewer),
    follow_up_in_days: (r.follow_up_in_days as number | null) ?? null,
    violations: JSON.parse(String(r.violations)) as DraftRow["violations"],
    secrets_found: Boolean(r.secrets_found),
    auto_send_blocker: (r.auto_send_blocker as string | null) ?? null,
    models: JSON.parse(String(r.models)),
    cost_usd: Number(r.cost_usd),
    latency_ms: Number(r.latency_ms),
    created_at: String(r.created_at),
  };
}
