/**
 * Google OAuth and the few Gmail API calls the Inbox Agent needs: list and read inbox mail,
 * create and apply labels, send a reply in the same thread. Plain fetch, no SDK, so tests
 * can pass a fake `fetch`.
 *
 * Scope: gmail.modify (read, label, send; it cannot permanently delete mail) plus
 * openid/email, so the token response says which Google account connected.
 */
export const SCOPES = ["openid", "email", "https://www.googleapis.com/auth/gmail.modify"];
const AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth";
const TOKEN_URL = "https://oauth2.googleapis.com/token";
const REVOKE_URL = "https://oauth2.googleapis.com/revoke";
const API = "https://gmail.googleapis.com/gmail/v1/users/me";

export interface GoogleConfig {
  clientId: string;
  clientSecret: string;
}

export type Fetch = typeof fetch;

export class GoogleError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function json<T>(res: Response, what: string): Promise<T> {
  const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
  if (!res.ok) {
    const err = body.error as { message?: string } | string | undefined;
    const msg = typeof err === "string" ? `${err}${body.error_description ? `: ${String(body.error_description)}` : ""}` : err?.message;
    throw new GoogleError(`${what} failed (${res.status}): ${msg ?? "no details"}`, res.status);
  }
  return body as T;
}

export function authUrl(cfg: GoogleConfig, redirectUri: string, state: string): string {
  const q = new URLSearchParams({
    client_id: cfg.clientId, redirect_uri: redirectUri, response_type: "code", scope: SCOPES.join(" "),
    access_type: "offline", prompt: "consent", include_granted_scopes: "true", state,
  });
  return `${AUTH_URL}?${q}`;
}

export interface Tokens {
  access_token: string;
  refresh_token?: string;
  expires_in: number;
  id_token?: string;
  scope?: string;
}

export async function exchangeCode(cfg: GoogleConfig, code: string, redirectUri: string, f: Fetch = fetch): Promise<Tokens> {
  const res = await f(TOKEN_URL, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ code, client_id: cfg.clientId, client_secret: cfg.clientSecret, redirect_uri: redirectUri, grant_type: "authorization_code" }),
  });
  return json<Tokens>(res, "Google sign-in");
}

export async function refreshAccess(cfg: GoogleConfig, refreshToken: string, f: Fetch = fetch): Promise<string> {
  const res = await f(TOKEN_URL, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ refresh_token: refreshToken, client_id: cfg.clientId, client_secret: cfg.clientSecret, grant_type: "refresh_token" }),
  });
  return (await json<Tokens>(res, "Refreshing Gmail access")).access_token;
}

export async function revoke(token: string, f: Fetch = fetch): Promise<void> {
  await f(REVOKE_URL, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ token }) }).catch(() => undefined);
}

/** The Google account in an id_token. It came straight from Google's token endpoint over
 *  TLS, so (per Google's guidance) its signature needn't be checked again here. */
export function identity(idToken: string | undefined): { sub: string; email: string } {
  const payload = idToken?.split(".")[1];
  if (!payload) throw new GoogleError("Google did not say which account signed in", 400);
  const claims = JSON.parse(Buffer.from(payload, "base64url").toString("utf8")) as { sub?: string; email?: string };
  if (!claims.sub || !claims.email) throw new GoogleError("Google did not say which account signed in", 400);
  return { sub: claims.sub, email: claims.email };
}

// ----- Gmail API

export interface GmailMessage {
  id: string;
  threadId: string;
  labelIds?: string[];
  internalDate?: string;
  payload?: Part;
}
interface Part {
  mimeType?: string;
  headers?: { name: string; value: string }[];
  body?: { data?: string; size?: number };
  parts?: Part[];
}

export class Gmail {
  constructor(private readonly accessToken: string, private readonly f: Fetch = fetch) {}

  private async call<T>(path: string, init: RequestInit = {}, what = "Gmail"): Promise<T> {
    const res = await this.f(`${API}${path}`, {
      ...init,
      headers: { authorization: `Bearer ${this.accessToken}`, ...(init.body ? { "content-type": "application/json" } : {}) },
    });
    return json<T>(res, what);
  }

  async listInbox(q: string, max: number): Promise<{ id: string; threadId: string }[]> {
    const r = await this.call<{ messages?: { id: string; threadId: string }[] }>(`/messages?${new URLSearchParams({ q, maxResults: String(max) })}`, {}, "Listing Gmail");
    return r.messages ?? [];
  }

  getMessage(id: string): Promise<GmailMessage> {
    return this.call<GmailMessage>(`/messages/${encodeURIComponent(id)}?format=full`, {}, "Reading a Gmail message");
  }

  async labels(): Promise<{ id: string; name: string }[]> {
    return (await this.call<{ labels?: { id: string; name: string }[] }>("/labels", {}, "Listing Gmail labels")).labels ?? [];
  }

  createLabel(name: string): Promise<{ id: string; name: string }> {
    return this.call("/labels", { method: "POST", body: JSON.stringify({ name, labelListVisibility: "labelShow", messageListVisibility: "show" }) }, "Creating a Gmail label");
  }

  modify(id: string, add: string[], remove: string[] = []): Promise<unknown> {
    return this.call(`/messages/${encodeURIComponent(id)}/modify`, { method: "POST", body: JSON.stringify({ addLabelIds: add, removeLabelIds: remove }) }, "Labelling a Gmail message");
  }

  trash(id: string, undo = false): Promise<unknown> {
    return this.call(`/messages/${encodeURIComponent(id)}/${undo ? "untrash" : "trash"}`, { method: "POST" }, "Moving a Gmail message");
  }

  send(raw: string, threadId: string): Promise<{ id: string; threadId: string }> {
    return this.call("/messages/send", { method: "POST", body: JSON.stringify({ raw, threadId }) }, "Sending through Gmail");
  }
}

// ----- turning Gmail messages into inbox emails, and replies into MIME

const header = (m: GmailMessage, name: string) =>
  m.payload?.headers?.find((h) => h.name.toLowerCase() === name.toLowerCase())?.value ?? null;

function decode(data: string | undefined): string {
  return data ? Buffer.from(data, "base64url").toString("utf8") : "";
}

function findPart(p: Part | undefined, mime: string): Part | undefined {
  if (!p) return undefined;
  if (p.mimeType === mime && p.body?.data) return p;
  for (const c of p.parts ?? []) {
    const hit = findPart(c, mime);
    if (hit) return hit;
  }
  return undefined;
}

export function htmlToText(html: string): string {
  return html
    .replace(/<(script|style|head)[\s\S]*?<\/\1>/gi, "")
    .replace(/<br\s*\/?>/gi, "\n").replace(/<\/(p|div|li|tr|h\d)>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}

/** Drops the quoted history under "On ... wrote:" so the agent reads only the new message. */
export function stripQuoted(text: string): string {
  const cut = text.search(/\n(On .{5,200}wrote:|-{2,} ?Original Message ?-{2,}|From: .+\nSent: )/);
  return (cut > 0 ? text.slice(0, cut) : text).trim();
}

export function parseFrom(value: string | null): { name: string; email: string } {
  const v = value ?? "";
  const m = v.match(/^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$/);
  const email = (m ? m[2]! : v).trim();
  const name = (m && m[1]!.trim()) || email.split("@")[0] || "Unknown sender";
  return { name, email };
}

export interface ParsedMail {
  gmailId: string;
  threadId: string;
  from: { name: string; email: string };
  subject: string;
  body: string;
  receivedAt: string;
  messageId: string | null;
  references: string | null;
}

export function parseMessage(m: GmailMessage): ParsedMail {
  const plain = findPart(m.payload, "text/plain");
  const html = plain ? undefined : findPart(m.payload, "text/html");
  const raw = plain ? decode(plain.body?.data) : html ? htmlToText(decode(html.body?.data)) : decode(m.payload?.body?.data);
  const body = stripQuoted(raw.replace(/\r\n/g, "\n")).slice(0, 20_000) || "(empty message)";
  const when = m.internalDate ? new Date(Number(m.internalDate)) : new Date(header(m, "Date") ?? Date.now());
  return {
    gmailId: m.id, threadId: m.threadId, from: parseFrom(header(m, "From")),
    subject: (header(m, "Subject") ?? "").slice(0, 500), body,
    receivedAt: (isNaN(when.getTime()) ? new Date() : when).toISOString(),
    messageId: header(m, "Message-ID") ?? header(m, "Message-Id"), references: header(m, "References"),
  };
}

const encodeWord = (s: string) => (/^[\x20-\x7e]*$/.test(s) ? s : `=?UTF-8?B?${Buffer.from(s, "utf8").toString("base64")}?=`);

/** A plain-text reply as a base64url RFC 2822 message, threaded under the original. */
export function buildReply(r: { to: string; subject: string; body: string; inReplyTo: string | null; references: string | null }): string {
  const lines = [
    `To: ${r.to}`,
    `Subject: ${encodeWord(r.subject)}`,
    ...(r.inReplyTo ? [`In-Reply-To: ${r.inReplyTo}`, `References: ${[r.references, r.inReplyTo].filter(Boolean).join(" ")}`] : []),
    "MIME-Version: 1.0",
    "Content-Type: text/plain; charset=UTF-8",
    "Content-Transfer-Encoding: base64",
    "",
    Buffer.from(r.body, "utf8").toString("base64").replace(/.{76}/g, "$&\r\n"),
  ];
  return Buffer.from(lines.join("\r\n"), "utf8").toString("base64url");
}
