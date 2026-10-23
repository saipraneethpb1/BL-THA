import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { loadConfig } from "../src/config.js";
import { LearningService } from "../src/feedback/learning.js";
import { InboxAgent } from "../src/pipeline.js";
import { buildApp } from "../src/server.js";
import { accountFromToken, COOKIE, makeToken } from "../src/session.js";
import { Workspaces } from "../src/workspaces.js";
import type { InboxStore } from "../src/store/db.js";
import { FakeClassifier, FakeDrafter, FakeKnowledge } from "./fakes.js";

const SECRET = "test-secret";
const ROOT = join(import.meta.dirname, "..");

function app() {
  const config = { ...loadConfig({}), dbPath: join(mkdtempSync(join(tmpdir(), "inbox-")), "inbox.db") };
  const make = (store: InboxStore) => ({
    store,
    agent: new InboxAgent(store, new FakeClassifier(), new FakeDrafter(), new FakeKnowledge(), config, () => 1),
    learning: new LearningService(store, null, null),
  });
  const workspaces = new Workspaces(config.dbPath, make, ROOT);
  return buildApp({ config, ...workspaces.get("guest"), workspaces, sessionSecret: SECRET });
}
const as = (account: string) => ({ cookie: `${COOKIE}=${makeToken(SECRET, account)}` });

describe("demo accounts", () => {
  it("verifies signed sessions and rejects forged, expired or unknown ones", () => {
    const t = makeToken(SECRET, "demo2", 1000);
    expect(accountFromToken(SECRET, t, 1060)).toBe("demo2");
    expect(accountFromToken(SECRET, t, 1000 + 31 * 24 * 3600)).toBeNull();
    expect(accountFromToken("other-secret", t, 1060)).toBeNull();
    expect(accountFromToken(SECRET, t.replace("demo2", "demo3"), 1060)).toBeNull();
    expect(accountFromToken(SECRET, makeToken(SECRET, "admin", 1000), 1060)).toBeNull();
  });

  it("gives each account its own inbox, starting with the sample emails", async () => {
    const a = app();
    const list = async (headers: Record<string, string> = {}) => (await a.inject({ url: "/api/emails", headers })).json() as { id: string }[];
    expect((await list(as("demo1"))).length).toBe(40); // a new account is ready to try
    expect(await list()).toEqual([]); // guest is the existing shared inbox, not a copy
    const mail = { id: "MINE", from: { name: "Ann", email: "ann@example.com" }, subject: "Hi", body: "Just for demo1" };
    await a.inject({ method: "POST", url: "/api/emails", payload: mail, headers: as("demo1") });
    expect((await list(as("demo1"))).some((e) => e.id === "MINE")).toBe(true);
    expect((await list(as("demo2"))).some((e) => e.id === "MINE")).toBe(false);
    expect((await a.inject({ url: "/api/session", headers: as("demo2") })).json()).toEqual({ workspace: "demo2" });
    const forged = { cookie: `${COOKIE}=demo1.9999999999.${"0".repeat(64)}` };
    expect((await a.inject({ url: "/api/session", headers: forged })).json()).toEqual({ workspace: "guest" });

    await a.inject({ method: "POST", url: "/api/emails/MINE/process", headers: as("demo1") });
    await a.inject({ method: "POST", url: "/api/emails/E001/process", headers: as("demo2") });
    const usage = (await a.inject({ url: "/api/usage" })).json();
    expect(usage.calls).toBe(2); // spend counts every inbox
  });
});
