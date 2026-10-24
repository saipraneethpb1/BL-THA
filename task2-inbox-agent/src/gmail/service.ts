/**
 * One workspace's connection to a real Gmail account, plugged into the normal flow:
 * new inbox mail is imported, labelled (in Gmail too, under "Inbox Agent/<parent>/<label>"),
 * drafted, and the reply a person approves (or that has earned auto-send) is sent from that
 * Gmail account in the same thread.
 *
 * Only mail that arrives after connecting is drafted automatically, so connecting a busy
 * inbox can't run up the bill; older mail is imported and can be drafted by hand.
 * The refresh token is stored encrypted (AES-256-GCM, key derived from SESSION_SECRET).
 */
import { createCipheriv, createDecipheriv, createHash, randomBytes } from "node:crypto";
import { readTopics, topicOf, type Topic } from "../email/topics.js";
import type { InboxAgent } from "../pipeline.js";
import type { InboxStore } from "../store/db.js";
import { buildReply, Gmail, GoogleError, parseMessage, refreshAccess, revoke, type Fetch, type GoogleConfig, type Tokens } from "./google.js";

export const INBOX_QUERY = "in:inbox -from:me newer_than:14d -category:promotions -category:social";
const IMPORT_MAX = 20;

export interface Connection {
  email: string;
  sub: string;
  refresh: string; // encrypted
  connected_at: string;
  last_sync_at: string | null;
  last_error: string | null;
  labels: Record<string, string>; // Gmail label name -> id
}

const key = (secret: string) => createHash("sha256").update(`gmail-token:${secret}`).digest();

export function encrypt(secret: string, text: string): string {
  const iv = randomBytes(12);
  const c = createCipheriv("aes-256-gcm", key(secret), iv);
  const ct = Buffer.concat([c.update(text, "utf8"), c.final()]);
  return [iv, c.getAuthTag(), ct].map((b) => b.toString("base64url")).join(".");
}

export function decrypt(secret: string, blob: string): string {
  const [iv, tag, ct] = blob.split(".").map((p) => Buffer.from(p, "base64url"));
  const d = createDecipheriv("aes-256-gcm", key(secret), iv!);
  d.setAuthTag(tag!);
  return Buffer.concat([d.update(ct!), d.final()]).toString("utf8");
}

/** The Gmail label for an email category, nested under its parent setting type. */
export function gmailLabelName(topic: Pick<Topic, "label" | "parent">): string {
  const clean = (x: string) => x.replace(/\//g, "-");
  return `Inbox Agent/${clean(topic.parent)}/${clean(topic.label)}`;
}

export interface SyncResult {
  imported: number;
  drafted: number;
  auto_sent: number;
}

export class GmailService {
  private access: { token: string; until: number } | null = null;

  constructor(
    private readonly store: InboxStore,
    private readonly agent: InboxAgent | null,
    private readonly cfg: GoogleConfig,
    private readonly secret: string,
    private readonly f: Fetch = fetch,
  ) {}

  connection(): Connection | undefined {
    return this.store.getSetting("gmail") as Connection | undefined;
  }

  private save(c: Connection): void {
    this.store.setSetting("gmail", c);
  }

  status() {
    const c = this.connection();
    return c
      ? { connected: true, email: c.email, connected_at: c.connected_at, last_sync_at: c.last_sync_at, last_error: c.last_error }
      : { connected: false };
  }

  connect(tokens: Tokens, who: { sub: string; email: string }): void {
    if (!tokens.refresh_token) throw new GoogleError("Google did not grant offline access; please connect again", 400);
    const old = this.connection();
    this.save({
      email: who.email, sub: who.sub, refresh: encrypt(this.secret, tokens.refresh_token),
      // Reconnecting keeps the original start, so mail from before isn't auto-drafted.
      connected_at: old?.connected_at ?? new Date().toISOString(),
      last_sync_at: old?.last_sync_at ?? null, last_error: null, labels: old?.labels ?? {},
    });
    this.access = { token: tokens.access_token, until: Date.now() + (tokens.expires_in - 60) * 1000 };
  }

  async disconnect(): Promise<void> {
    const c = this.connection();
    if (c) await revoke(decrypt(this.secret, c.refresh), this.f);
    this.access = null;
  }

  private async client(): Promise<Gmail> {
    const c = this.connection();
    if (!c) throw new GoogleError("Gmail is not connected", 409);
    if (!this.access || this.access.until < Date.now()) {
      try {
        this.access = { token: await refreshAccess(this.cfg, decrypt(this.secret, c.refresh), this.f), until: Date.now() + 50 * 60 * 1000 };
      } catch (e) {
        if (e instanceof GoogleError && e.status === 400) {
          throw new GoogleError("Google access expired or was revoked (Testing-mode sign-ins last 7 days): connect Gmail again", 401);
        }
        throw e;
      }
    }
    return new Gmail(this.access.token, this.f);
  }

  private note(error: string | null, synced = false): void {
    const c = this.connection();
    if (c) this.save({ ...c, last_error: error, ...(synced ? { last_sync_at: new Date().toISOString() } : {}) });
  }

  /** Imports new inbox mail; drafts (up to `draftLimit`) what arrived after connecting. */
  async sync(opts: { draftLimit: number; beforeDraft?: () => void }): Promise<SyncResult> {
    const result: SyncResult = { imported: 0, drafted: 0, auto_sent: 0 };
    try {
      const c = this.connection();
      if (!c) throw new GoogleError("Gmail is not connected", 409);
      const g = await this.client();
      const fresh: { id: string; at: string }[] = [];
      for (const { id } of await g.listInbox(INBOX_QUERY, IMPORT_MAX)) {
        if (this.store.hasGmailMessage(id)) continue;
        const m = parseMessage(await g.getMessage(id));
        if (m.from.email.toLowerCase() === c.email.toLowerCase()) continue;
        const emailId = `G${m.gmailId}`;
        this.store.addEmail({ id: emailId, from_name: m.from.name, from_email: m.from.email, subject: m.subject, body: m.body, thread: [], received_at: m.receivedAt });
        this.store.linkGmail({ emailId, gmailId: m.gmailId, threadId: m.threadId, messageId: m.messageId, references: m.references });
        fresh.push({ id: emailId, at: m.receivedAt });
        result.imported++;
      }
      if (this.agent) {
        for (const { id } of fresh.filter((x) => x.at >= c.connected_at).slice(0, opts.draftLimit)) {
          opts.beforeDraft?.();
          const r = await this.agent.process(id);
          result.drafted++;
          if (r.autoSent) result.auto_sent++;
          if (r.blocker === "spam") await this.mirrorMove(id, "spam", "new").catch(() => undefined);
          await this.afterProcess(id, r.autoSent);
        }
      }
      this.note(null, true);
      return result;
    } catch (e) {
      this.note(e instanceof Error ? e.message : String(e));
      throw e;
    }
  }

  /** After drafting a Gmail email: label it in Gmail, and send it if it was auto-sent. */
  async afterProcess(emailId: string, autoSent: boolean): Promise<void> {
    if (!this.store.gmailLink(emailId)) return;
    await this.label(emailId).catch((e) => this.note(e instanceof Error ? e.message : String(e)));
    if (autoSent) await this.deliver(emailId);
  }

  async label(emailId: string): Promise<void> {
    const link = this.store.gmailLink(emailId);
    const c = this.store.latestDraft(emailId)?.classification as { topic?: string; spam?: boolean } | undefined;
    if (!link || link.labelled || !c || c.spam) return;
    const g = await this.client();
    const labelId = await this.ensureLabel(g, gmailLabelName(topicOf(readTopics(this.store), c.topic)));
    await g.modify(link.gmail_id, [labelId]);
    this.store.markGmailLabelled(emailId);
  }

  private async ensureLabel(g: Gmail, path: string): Promise<string> {
    const c = this.connection()!;
    const cached = c.labels[path];
    if (cached) return cached;
    const existing = new Map((await g.labels()).map((l) => [l.name, l.id]));
    const parts = path.split("/");
    let id = "";
    // Create each level so Gmail shows them nested: Inbox Agent > Product support > How-to.
    for (let i = 1; i <= parts.length; i++) {
      const name = parts.slice(0, i).join("/");
      id = existing.get(name) ?? (await g.createLabel(name)).id;
      existing.set(name, id);
    }
    this.save({ ...this.connection()!, labels: { ...c.labels, [path]: id } });
    return id;
  }

  /** Mirrors Spam / Trash / back in the Gmail mailbox, so both stay the same. */
  async mirrorMove(emailId: string, to: "spam" | "trash" | "back", from: string): Promise<void> {
    const link = this.store.gmailLink(emailId);
    if (!link) return;
    const g = await this.client();
    if (to === "spam") await g.modify(link.gmail_id, ["SPAM"], ["INBOX"]);
    else if (to === "trash") await g.trash(link.gmail_id);
    else if (from === "spam") await g.modify(link.gmail_id, ["INBOX"], ["SPAM"]);
    else if (from === "trash") await g.trash(link.gmail_id, true);
  }

  /** Sends the recorded reply for a Gmail email through Gmail, once, in the same thread. */
  async deliver(emailId: string): Promise<void> {
    const link = this.store.gmailLink(emailId);
    const out = this.store.latestOutbox(emailId);
    if (!link || !out || out.delivery?.startsWith("gmail:")) return;
    try {
      const g = await this.client();
      const sent = await g.send(buildReply({ to: out.to_email, subject: out.subject, body: out.body, inReplyTo: link.message_id, references: link.references_hdr }), link.thread_id);
      this.store.setDelivery(out.id, `gmail:${sent.id}`);
      await g.modify(link.gmail_id, [], ["UNREAD"]).catch(() => undefined);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      this.store.setDelivery(out.id, `error: ${msg}`);
      this.note(msg);
      throw e;
    }
  }
}
