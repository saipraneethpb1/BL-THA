import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { loadConfig } from "../src/config.js";
import { reconsider } from "../src/feedback/autonomy.js";
import { LearningService } from "../src/feedback/learning.js";
import { CATEGORIES, CATEGORY_IDS, PARENTS } from "../src/email/taxonomy.js";
import { InboxAgent } from "../src/pipeline.js";
import { buildApp } from "../src/server.js";
import { COOKIE, makeToken } from "../src/session.js";
import { readSettings, updateSettings } from "../src/settings.js";
import { InboxStore } from "../src/store/db.js";
import { Workspaces } from "../src/workspaces.js";
import { classification, FakeClassifier, FakeDrafter, FakeKnowledge } from "./fakes.js";

const mail = (id: string) => ({ id, from: { name: "Megan", email: "megan@example.com" }, subject: "Logo", body: "How do I change the logo?" });

describe("labels and parent types", () => {
  it("puts every label under a parent type", () => {
    for (const id of CATEGORY_IDS) expect(PARENTS[CATEGORIES[id].parent]).toBeDefined();
    expect(CATEGORIES.refund_cancellation.parent).toBe("account");
    expect(CATEGORIES.how_to.parent).toBe("support");
  });
});

describe("changing X or a label's switch applies at once", () => {
  it("lowering X grants a label already past it; raising it takes autonomy back but keeps the streak", () => {
    const s = { kind: "how_to" as const, streak: 4, status: "supervised" as const };
    const granted = reconsider(s, { eligible: true, threshold: 3 });
    expect(granted.state.status).toBe("autonomous");
    const back = reconsider(granted.state, { eligible: true, threshold: 6 });
    expect(back.state).toMatchObject({ status: "supervised", streak: 4 });
    expect(back.events[0]?.detail).toMatch(/2 more/);
    expect(reconsider(s, { eligible: true, threshold: 5 }).events).toEqual([]);
  });

  it("switching a label off revokes it", () => {
    const r = reconsider({ kind: "how_to", streak: 9, status: "autonomous" }, { eligible: false, threshold: 3 });
    expect(r.state).toMatchObject({ status: "supervised", streak: 0 });
  });

  it("keeps settings per inbox, storing only what differs from the defaults", () => {
    const store = new InboxStore(":memory:");
    expect(readSettings(store, 20)).toMatchObject({ autonomyThreshold: 20, autoSend: { how_to: true, refund_cancellation: false } });
    updateSettings(store, 20, { autonomyThreshold: 3, autoSend: { refund_cancellation: true } }, "sai");
    expect(readSettings(store, 20)).toMatchObject({ autonomyThreshold: 3, autoSend: { refund_cancellation: true, how_to: true } });
    expect(store.getSetting("auto_send")).toEqual({ refund_cancellation: true });
    expect(store.autonomyEvents(1)[0]).toMatchObject({ kind: "settings", event: "settings_changed" });
    expect(() => updateSettings(store, 20, { autonomyThreshold: 0 }, "sai")).toThrow(RangeError);
  });

  it("auto-sends a label switched on in settings once it reaches the new X", async () => {
    const store = new InboxStore(":memory:");
    const classifier = new FakeClassifier(classification({ category: "refund_cancellation" }));
    const agent = new InboxAgent(store, classifier, new FakeDrafter(), new FakeKnowledge(), { autonomyThreshold: 20, spotCheckRate: 0 });
    updateSettings(store, 20, { autonomyThreshold: 2, autoSend: { refund_cancellation: true } }, "sai");
    for (const id of ["A", "B"]) {
      store.addEmail({ id, from_name: "M", from_email: "m@example.com", subject: "Idea", body: "Dark mode?", thread: [], received_at: "2026-01-01T00:00:00Z" });
      await agent.process(id);
      agent.review(id, { action: "send", reviewer: "sai" });
    }
    expect(store.autonomy("header").status).toBe("autonomous");
    store.addEmail({ id: "C", from_name: "M", from_email: "m@example.com", subject: "Idea", body: "Dark mode?", thread: [], received_at: "2026-01-01T00:00:00Z" });
    expect((await agent.process("C")).autoSent).toBe(true);

    updateSettings(store, 20, { autoSend: { refund_cancellation: false } }, "sai");
    store.addEmail({ id: "D", from_name: "M", from_email: "m@example.com", subject: "Idea", body: "Dark mode?", thread: [], received_at: "2026-01-01T00:00:00Z" });
    expect((await agent.process("D")).autoSent).toBe(false);
  });
});

describe("settings API", () => {
  function app() {
    const config = { ...loadConfig({}), dbPath: join(mkdtempSync(join(tmpdir(), "inbox-")), "inbox.db") };
    const make = (store: InboxStore) => ({
      store,
      agent: new InboxAgent(store, new FakeClassifier(), new FakeDrafter(), new FakeKnowledge(), config, () => 1),
      learning: new LearningService(store, null, null),
    });
    const workspaces = new Workspaces(config.dbPath, make, join(import.meta.dirname, ".."));
    return buildApp({ config, ...workspaces.get("guest"), workspaces, sessionSecret: "s" });
  }
  const demo1 = { cookie: `${COOKIE}=${makeToken("s", "demo1")}` };
  const proxied = { "x-forwarded-for": "203.0.113.9" };

  it("lists labels with their parents and saves X per account", async () => {
    const a = app();
    const s = (await a.inject({ url: "/api/settings", headers: demo1 })).json();
    expect(s.autonomy_threshold).toBe(20);
    expect(s.parents).toEqual(expect.arrayContaining(["Design Settings", "Members", "Finance"]));
    expect(s.topics.find((t: { id: string }) => t.id === "search_filters")).toMatchObject({ label: "Search & Filters", parent: "Members" });
    expect(s.request_types.find((l: { id: string }) => l.id === "refund_cancellation")).toMatchObject({ auto_send: false });

    const put = await a.inject({ method: "PUT", url: "/api/settings", headers: { ...demo1, ...proxied }, payload: { autonomy_threshold: 5, auto_send: { complaint: true } } });
    expect(put.statusCode).toBe(200);
    expect((await a.inject({ url: "/api/autonomy", headers: demo1 })).json().threshold).toBe(5);
    expect((await a.inject({ url: "/api/autonomy" })).json().threshold).toBe(20); // guest unaffected
    expect((await a.inject({ method: "PUT", url: "/api/settings", headers: demo1, payload: { autonomy_threshold: 500 } })).statusCode).toBe(422);
  });

  it("asks visitors to sign in before changing the shared guest inbox's settings", async () => {
    const a = app();
    const r = await a.inject({ method: "PUT", url: "/api/settings", headers: proxied, payload: { autonomy_threshold: 1 } });
    expect(r.statusCode).toBe(401);
  });
});
