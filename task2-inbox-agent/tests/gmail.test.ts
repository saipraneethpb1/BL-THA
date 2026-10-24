import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { loadConfig } from "../src/config.js";
import { LearningService } from "../src/feedback/learning.js";
import { buildReply, parseFrom, parseMessage, type GmailMessage } from "../src/gmail/google.js";
import { decrypt, encrypt, gmailLabelName } from "../src/gmail/service.js";
import { InboxAgent } from "../src/pipeline.js";
import { buildApp } from "../src/server.js";
import { COOKIE, makeToken } from "../src/session.js";
import type { InboxStore } from "../src/store/db.js";
import { Workspaces } from "../src/workspaces.js";
import { FakeClassifier, FakeDrafter, FakeKnowledge } from "./fakes.js";

const b64 = (s: string) => Buffer.from(s, "utf8").toString("base64url");

function message(id: string, over: { from?: string; body?: string; html?: string; at?: number } = {}): GmailMessage {
  const parts = over.html
    ? [{ mimeType: "text/html", body: { data: b64(over.html) } }]
    : [{ mimeType: "text/plain", body: { data: b64(over.body ?? "How do I change the logo?\n\nOn Mon, Ann <a@x.com> wrote:\n> old stuff") } }];
  return {
    id, threadId: `t-${id}`, internalDate: String(over.at ?? Date.now() + 5000),
    payload: {
      mimeType: "multipart/alternative", parts,
      headers: [
        { name: "From", value: over.from ?? '"Megan Lee" <megan@example.com>' },
        { name: "Subject", value: "Logo" },
        { name: "Message-ID", value: `<${id}@mail.example.com>` },
      ],
    },
  };
}

describe("reading and writing Gmail messages", () => {
  it("takes the plain text, drops quoted history, and reads the sender", () => {
    const m = parseMessage(message("m1"));
    expect(m).toMatchObject({ gmailId: "m1", threadId: "t-m1", subject: "Logo", body: "How do I change the logo?", messageId: "<m1@mail.example.com>" });
    expect(m.from).toEqual({ name: "Megan Lee", email: "megan@example.com" });
    expect(parseMessage(message("m2", { html: "<p>Hi&nbsp;there</p><script>x()</script><br>Thanks" })).body).toBe("Hi there\n\nThanks");
    expect(parseFrom("solo@example.com")).toEqual({ name: "solo", email: "solo@example.com" });
  });

  it("builds a threaded UTF-8 reply", () => {
    const raw = Buffer.from(buildReply({ to: "megan@example.com", subject: "Re: Café", body: "Merci, ça marche", inReplyTo: "<m1@x>", references: "<m0@x>" }), "base64url").toString("utf8");
    expect(raw).toContain("In-Reply-To: <m1@x>");
    expect(raw).toContain("References: <m0@x> <m1@x>");
    expect(raw).toMatch(/Subject: =\?UTF-8\?B\?/);
    expect(Buffer.from(raw.split("\r\n\r\n")[1]!.replace(/\r\n/g, ""), "base64").toString("utf8")).toBe("Merci, ça marche");
  });

  it("keeps refresh tokens encrypted and tamper-evident", () => {
    const blob = encrypt("s", "refresh-123");
    expect(blob).not.toContain("refresh-123");
    expect(decrypt("s", blob)).toBe("refresh-123");
    expect(() => decrypt("other", blob)).toThrow();
  });

  it("nests Gmail labels under the category's parent setting type", () => {
    expect(gmailLabelName({ label: "Header", parent: "Design Settings" })).toBe("Inbox Agent/Design Settings/Header");
    expect(gmailLabelName({ label: "A/B tests", parent: "Toolbox" })).toBe("Inbox Agent/Toolbox/A-B tests");
  });
});

/** A fake Google: OAuth token endpoint plus the Gmail API calls the service makes. */
function fakeGoogle(inbox: GmailMessage[]) {
  const calls: { url: string; body: string }[] = [];
  const labels: { id: string; name: string }[] = [];
  const idToken = `x.${b64(JSON.stringify({ sub: "1234567890", email: "me@gmail.com" }))}.y`;
  const f = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    const body = typeof init?.body === "string" ? init.body : init?.body ? String(init.body) : "";
    calls.push({ url, body });
    const ok = (data: unknown) => new Response(JSON.stringify(data), { status: 200, headers: { "content-type": "application/json" } });
    if (url.endsWith("/token")) return ok({ access_token: "acc", refresh_token: "ref", expires_in: 3600, id_token: idToken });
    if (url.endsWith("/revoke")) return ok({});
    if (url.includes("/messages?")) return ok({ messages: inbox.map((m) => ({ id: m.id, threadId: m.threadId })) });
    if (url.endsWith("/labels") && init?.method === "POST") {
      const l = { id: `L${labels.length + 1}`, name: (JSON.parse(body) as { name: string }).name };
      labels.push(l);
      return ok(l);
    }
    if (url.endsWith("/labels")) return ok({ labels });
    if (url.endsWith("/messages/send")) return ok({ id: "sent-1", threadId: "t-m1" });
    if (url.includes("/modify")) return ok({});
    const id = decodeURIComponent(url.split("/messages/")[1]?.split("?")[0] ?? "");
    const m = inbox.find((x) => x.id === id);
    return m ? ok(m) : new Response("{}", { status: 404 });
  }) as typeof fetch;
  return { f, calls, labels };
}

function app(google: ReturnType<typeof fakeGoogle>) {
  const config = {
    ...loadConfig({}), dbPath: join(mkdtempSync(join(tmpdir(), "inbox-")), "inbox.db"),
    google: { clientId: "cid", clientSecret: "csecret" }, gmailSyncMinutes: 0,
  };
  const make = (store: InboxStore) => ({
    store,
    agent: new InboxAgent(store, new FakeClassifier(), new FakeDrafter(), new FakeKnowledge(), config, () => 1),
    learning: new LearningService(store, null, null),
  });
  const workspaces = new Workspaces(config.dbPath, make, join(import.meta.dirname, ".."));
  return buildApp({ config, ...workspaces.get("guest"), workspaces, sessionSecret: "s", googleFetch: google.f });
}

const setCookies = (h: string | string[] | undefined) => (Array.isArray(h) ? h : h ? [h] : []);
const cookieFrom = (h: string | string[] | undefined, name: string) =>
  setCookies(h).map((c) => c.split(";")[0]!).find((c) => c.startsWith(`${name}=`));

describe("Connect Gmail", () => {
  it("signs in with Google into a private workspace, then reads, labels, drafts and replies through Gmail", async () => {
    const google = fakeGoogle([message("m1"), message("old", { at: Date.parse("2020-01-01") }), message("mine", { from: "Me <me@gmail.com>" })]);
    const a = app(google);

    const start = await a.inject({ url: "/api/gmail/connect", headers: { host: "inbox.example.sslip.io" } });
    expect(start.statusCode).toBe(302);
    const to = new URL(start.headers.location as string);
    expect(to.host).toBe("accounts.google.com");
    expect(to.searchParams.get("scope")).toContain("gmail.modify");
    expect(to.searchParams.get("redirect_uri")).toBe("http://inbox.example.sslip.io/api/gmail/callback");
    const oauth = cookieFrom(start.headers["set-cookie"], "bl_oauth")!;
    const state = to.searchParams.get("state")!;

    // Google's answer must come back to the same browser.
    const forged = await a.inject({ url: `/api/gmail/callback?code=c&state=${state}` });
    expect(forged.headers.location).toMatch(/gmail-error/);

    const cb = await a.inject({ url: `/api/gmail/callback?code=c&state=${state}`, headers: { cookie: oauth, host: "inbox.example.sslip.io" } });
    expect(cb.headers.location).toBe("/#/folder/inbox");
    const session = cookieFrom(cb.headers["set-cookie"], COOKIE)!;
    expect(session).toMatch(/^bl_session=g_[0-9a-f]{16}\./);
    expect(setCookies(cb.headers["set-cookie"]).join("\n")).toContain("Domain=example.sslip.io");
    const me = { cookie: session };

    await new Promise((r) => setTimeout(r, 50)); // the first sync runs in the background
    const status = (await a.inject({ url: "/api/gmail/status", headers: me })).json();
    expect(status).toMatchObject({ configured: true, connected: true, email: "me@gmail.com" });
    expect((await a.inject({ url: "/api/gmail/status" })).json()).toMatchObject({ connected: false }); // guest

    const list = (await a.inject({ url: "/api/emails", headers: me })).json() as { id: string; status: string; from_gmail: boolean }[];
    expect(list.map((e) => e.id).sort()).toEqual(["Gm1", "Gold"]); // own mail skipped, no sample emails
    expect(list.find((e) => e.id === "Gm1")?.status).toBe("drafted"); // new mail is drafted
    expect(list.find((e) => e.id === "Gold")?.status).toBe("new"); // mail from before connecting is not
    expect(google.labels.map((l) => l.name)).toEqual(["Inbox Agent", "Inbox Agent/Design Settings", "Inbox Agent/Design Settings/Header"]);
    expect(google.calls.some((c) => c.url.includes("/messages/m1/modify") && c.body.includes("L3"))).toBe(true);

    // Nobody else can see it: not the guest inbox, not a demo account.
    expect((await a.inject({ url: "/api/emails" })).json()).toEqual([]);
    expect(((await a.inject({ url: "/api/emails", headers: { cookie: `${COOKIE}=${makeToken("s", "demo1")}` } })).json() as unknown[]).some((e) => (e as { id: string }).id === "Gm1")).toBe(false);

    const sent = await a.inject({ method: "POST", url: "/api/emails/Gm1/review", headers: me, payload: { action: "send", reviewer: "me" } });
    expect(sent.json()).not.toHaveProperty("delivery_error");
    const send = google.calls.find((c) => c.url.endsWith("/messages/send"))!;
    const { raw, threadId } = JSON.parse(send.body) as { raw: string; threadId: string };
    expect(threadId).toBe("t-m1");
    expect(Buffer.from(raw, "base64url").toString("utf8")).toContain("In-Reply-To: <m1@mail.example.com>");
    expect(((await a.inject({ url: "/api/emails", headers: me })).json() as { id: string; delivery: string }[]).find((e) => e.id === "Gm1")?.delivery).toBe("gmail:sent-1");

    // A second sync imports nothing new.
    expect((await a.inject({ method: "POST", url: "/api/gmail/sync", headers: me })).json()).toMatchObject({ imported: 0 });

    const off = await a.inject({ method: "POST", url: "/api/gmail/disconnect", headers: me });
    expect(off.json()).toEqual({ erased: true });
    expect(google.calls.some((c) => c.url.endsWith("/revoke"))).toBe(true);
    expect((await a.inject({ url: "/api/emails", headers: me })).json()).toEqual([]);
    expect((await a.inject({ url: "/api/gmail/status", headers: me })).json()).toMatchObject({ connected: false });
  });

  it("is off, and says so, without a Google client", async () => {
    const config = { ...loadConfig({}), google: null };
    const store = (await import("../src/store/db.js")).InboxStore;
    const s = new store(":memory:");
    const a = buildApp({ config, store: s, agent: null, learning: new LearningService(s, null, null) });
    expect((await a.inject({ url: "/api/gmail/status" })).json()).toMatchObject({ configured: false, connected: false });
    expect((await a.inject({ url: "/api/gmail/connect" })).statusCode).toBe(503);
  });
});
