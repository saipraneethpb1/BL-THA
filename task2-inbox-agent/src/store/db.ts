/**
 * Persistence on Node's built-in SQLite: one file, no extra service, easy to inspect.
 * Every draft, review and autonomy change is kept, which is what lets us measure
 * whether the agent is getting better and audit why something was auto-sent.
 */
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";
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
-- Guidance learned from human edits, versioned so it can be measured and rolled back.
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category TEXT NOT NULL,
  text TEXT NOT NULL,
  version INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  source TEXT NOT NULL DEFAULT '',  -- review ids it was learned from
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
  violations: { code: string; severity: string; message: string }[];
  secrets_found: boolean;
  auto_send_blocker: string | null;
  models: unknown;
  cost_usd: number;
  latency_ms: number;
  created_at: string;
}

type Row = Record<string, unknown>;

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

  listEmails(): (EmailRow & { category: string | null; urgency: string | null; blocker: string | null })[] {
    const rows = this.db
      .prepare(
        `SELECT e.*, json_extract(d.classification, '$.category') AS category,
                json_extract(d.classification, '$.urgency') AS urgency, d.auto_send_blocker AS blocker
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
    }));
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

  autonomyEvents(limit = 50): Row[] {
    return this.db.prepare("SELECT * FROM autonomy_events ORDER BY id DESC LIMIT ?").all(limit) as Row[];
  }

  send(emailId: string, to: string, subject: string, body: string, mode: "human" | "auto"): void {
    this.db
      .prepare("INSERT INTO outbox(email_id, to_email, subject, body, mode) VALUES (?, ?, ?, ?, ?)")
      .run(emailId, to, subject, body, mode);
  }

  activeLessons(category: CategoryId): string[] {
    return (
      this.db
        .prepare("SELECT text FROM lessons WHERE category = ? AND active = 1 ORDER BY id")
        .all(category) as Row[]
    ).map((r) => String(r.text));
  }
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
