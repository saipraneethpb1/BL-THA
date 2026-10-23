/**
 * One inbox per demo account; "guest" for everyone else.
 *
 * Each account's emails, drafts, reviews, autonomy counters and lessons live in their own
 * SQLite file under data/accounts/, so they persist across visits and nobody else sees them.
 * A new account's inbox starts with the 40 sample emails, ready to try.
 */
import { existsSync, mkdirSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { EmailCase } from "./dataset/schema.js";
import type { LearningService } from "./feedback/learning.js";
import type { InboxAgent } from "./pipeline.js";
import { DEMO_ACCOUNTS, GUEST } from "./session.js";
import { InboxStore } from "./store/db.js";

export interface Workspace {
  store: InboxStore;
  /** null when no ANTHROPIC_API_KEY: the inbox and reviews still work, drafting does not. */
  agent: InboxAgent | null;
  learning: LearningService;
}

export type MakeWorkspace = (store: InboxStore) => Workspace;

/** Loads a labelled split into an inbox (the labels are never shown to the agent). */
export function seedEmails(store: InboxStore, root: string, split: "dev" | "test"): { added: number; total: number } {
  const lines = readFileSync(join(root, `data/emails/${split}.jsonl`), "utf8").split(/\r?\n/).filter(Boolean);
  let added = 0;
  for (const line of lines) {
    const e = EmailCase.parse(JSON.parse(line));
    if (store.addEmail({ id: e.id, from_name: e.from.name, from_email: e.from.email, subject: e.subject, body: e.body, thread: e.thread, received_at: e.receivedAt })) added++;
  }
  return { added, total: lines.length };
}

export class Workspaces {
  private readonly open = new Map<string, Workspace>();
  private readonly accountsDir: string;

  constructor(
    private readonly guestDb: string,
    private readonly make: MakeWorkspace,
    private readonly root: string,
  ) {
    this.accountsDir = join(dirname(guestDb), "accounts");
  }

  pathFor(account: string): string {
    if (account === GUEST) return this.guestDb;
    if (!(DEMO_ACCOUNTS as readonly string[]).includes(account)) throw new Error(`unknown account ${account}`);
    return join(this.accountsDir, `${account}.db`);
  }

  get(account: string): Workspace {
    let ws = this.open.get(account);
    if (!ws) {
      const path = this.pathFor(account);
      const fresh = account !== GUEST && path !== ":memory:" && !existsSync(path);
      if (fresh) mkdirSync(dirname(path), { recursive: true });
      ws = this.make(new InboxStore(path));
      if (fresh) seedEmails(ws.store, this.root, "dev");
      this.open.set(account, ws);
    }
    return ws;
  }

  /** Every inbox that exists, for site-wide usage and budget. */
  all(): Workspace[] {
    const names = existsSync(this.accountsDir)
      ? readdirSync(this.accountsDir).filter((f) => f.endsWith(".db")).map((f) => f.slice(0, -3))
        .filter((n) => (DEMO_ACCOUNTS as readonly string[]).includes(n)).sort()
      : [];
    return [GUEST, ...names].map((n) => this.get(n));
  }
}
