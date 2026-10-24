import { describe, expect, it } from "vitest";
import { loadConfig } from "../src/config.js";
import { LearningService, type LessonMiner } from "../src/feedback/learning.js";
import { DEFAULT_TOPICS, OTHER, readTopics, saveTopics, TopicError } from "../src/email/topics.js";
import { normalizeClassification, type DraftRequest, type Drafter } from "../src/llm/models.js";
import { InboxAgent } from "../src/pipeline.js";
import { buildApp } from "../src/server.js";
import { updateSettings } from "../src/settings.js";
import { InboxStore } from "../src/store/db.js";
import { addEmail, classification, FakeClassifier, FakeDrafter, FakeKnowledge } from "./fakes.js";

describe("email categories (website areas under their parent setting type)", () => {
  it("start from the help center's structure and always keep Other", () => {
    const store = new InboxStore(":memory:");
    const topics = readTopics(store);
    expect(topics.map((t) => t.label)).toEqual(expect.arrayContaining(["Home Page", "Header", "Footer", "Search & Filters"]));
    expect(topics.find((t) => t.id === "footer")?.parent).toBe("Design Settings");
    const saved = saveTopics(store, [{ label: "Header", parent: "Design", description: "" }]);
    expect(saved.map((t) => t.id)).toEqual(["header", OTHER]);
  });

  it("keep their id when renamed, so streaks and emails keep their category", () => {
    const store = new InboxStore(":memory:");
    const edited = readTopics(store).map((t) => (t.id === "search_filters" ? { ...t, label: "Filters", parent: "Search" } : t));
    saveTopics(store, [...edited, { label: "Pop-ups", parent: "Design Settings", description: "Pop-up windows" }]);
    const topics = readTopics(store);
    expect(topics.find((t) => t.id === "search_filters")).toMatchObject({ label: "Filters", parent: "Search" });
    expect(topics.find((t) => t.label === "Pop-ups")).toMatchObject({ id: "pop_ups", auto_send: true });
    expect(() => saveTopics(store, [{ label: "", parent: "x", description: "" }])).toThrow(TopicError);
  });

  it("an id the model makes up, or one removed meanwhile, falls back to Other", () => {
    const raw = { ...classification(), secondary: [], questions: [] };
    expect(normalizeClassification({ ...raw, topic: "header" }).topic).toBe("header");
    expect(normalizeClassification({ ...raw, topic: "headers_and_stuff" }).topic).toBe(OTHER);
    expect(normalizeClassification({ ...raw, topic: "header" }, [DEFAULT_TOPICS.at(-1)!]).topic).toBe(OTHER);
  });

  it("each category earns auto-send on its own, and its switch can stop it", async () => {
    const store = new InboxStore(":memory:");
    const classifier = new FakeClassifier(classification({ topic: "footer" }));
    const agent = new InboxAgent(store, classifier, new FakeDrafter(), new FakeKnowledge(), { autonomyThreshold: 2, spotCheckRate: 0 });
    for (let i = 0; i < 2; i++) {
      const id = addEmail(store);
      await agent.process(id);
      agent.review(id, { action: "send", reviewer: "sai" });
    }
    expect(store.autonomy("footer").status).toBe("autonomous");
    expect(store.autonomy("header").streak).toBe(0); // other categories are separate
    expect((await agent.process(addEmail(store))).autoSent).toBe(true);

    const topics = readTopics(store).map((t) => (t.id === "footer" ? { ...t, auto_send: false } : t));
    updateSettings(store, 2, { topics }, "sai");
    expect(store.autonomy("footer").status).toBe("supervised");
    expect((await agent.process(addEmail(store))).autoSent).toBe(false);
  });
});

describe("Spam and Trash", () => {
  it("spam is filed without a reply; Not spam and Restore put mail back where it was", async () => {
    const store = new InboxStore(":memory:");
    const drafter = new FakeDrafter();
    const kb = new FakeKnowledge();
    const agent = new InboxAgent(store, new FakeClassifier(classification({ spam: true })), drafter, kb, { autonomyThreshold: 2, spotCheckRate: 0 });
    const id = addEmail(store, "Buy 10,000 backlinks today!!!");
    expect(await agent.process(id)).toMatchObject({ autoSent: false, blocker: "spam" });
    expect(store.getEmail(id)?.status).toBe("spam");
    expect(drafter.seen).toHaveLength(0);
    expect(kb.questions).toHaveLength(0);
    expect(store.moveEmail(id, "back")).toBe("new");

    store.setEmailStatus(id, "drafted");
    expect(store.moveEmail(id, "trash")).toBe("trash");
    expect(store.moveEmail(id, "spam")).toBe("spam"); // moved on from Trash: still remembers "drafted"
    expect(store.moveEmail(id, "back")).toBe("drafted");
  });

  it("is exposed over the API", async () => {
    const config = { ...loadConfig({}), autonomyThreshold: 2 };
    const store = new InboxStore(":memory:");
    const app = buildApp({ config, store, agent: null, learning: new LearningService(store, null, null) });
    await app.inject({ method: "POST", url: "/api/emails", payload: { id: "S1", from: { name: "Seo", email: "seo@spam.test" }, subject: "Rank #1", body: "Guaranteed!" } });
    expect((await app.inject({ method: "POST", url: "/api/emails/S1/move", payload: { to: "spam" } })).json()).toEqual({ id: "S1", status: "spam" });
    expect((await app.inject({ method: "POST", url: "/api/emails/S1/move", payload: { to: "back" } })).json()).toEqual({ id: "S1", status: "new" });
    expect((await app.inject({ method: "POST", url: "/api/emails/S1/move", payload: { to: "delete" } })).statusCode).toBe(422);
  });
});

describe("the learning loop runs by itself", () => {
  const SHORT = "Hi Megan,\n\nGo to Design Settings and upload the new logo.\n\nThe Lakeview team";
  const LESSON = "Keep replies short.";
  class Drafts implements Drafter {
    async draft(req: DraftRequest) {
      const body = req.lessons.includes(LESSON) ? SHORT : `${SHORT}\n\nThank you so much, we truly appreciate your message and are always happy to help!`;
      return { result: { subject: "Re: Logo", body, used_evidence: [1], follow_up_in_days: null, note_for_reviewer: "" }, usage: { model: "f", promptVersion: "t", inputTokens: 1, outputTokens: 1, costUsd: 0 } };
    }
  }
  const miner: LessonMiner = { mine: async (_c, _cur, cs) => ({ lessons: [{ text: LESSON, review_ids: cs.map((c) => c.review_id) }] }) };

  it("after enough corrections it proposes, checks and makes lessons live", async () => {
    const store = new InboxStore(":memory:");
    const drafter = new Drafts();
    const agent = new InboxAgent(store, new FakeClassifier(), drafter, new FakeKnowledge(), { autonomyThreshold: 5, spotCheckRate: 0 });
    const learning = new LearningService(store, miner, drafter, { evalLimit: 6, minUnseen: 1 });
    let spent = 0;
    for (let i = 0; i < 2; i++) {
      const id = addEmail(store);
      await agent.process(id);
      agent.review(id, { action: "send", reviewer: "sai", body: SHORT, reason: "too long" });
      expect(await learning.autoLearn("how_to", () => spent++)).toBeNull(); // not enough yet
    }
    const id = addEmail(store);
    await agent.process(id);
    agent.review(id, { action: "send", reviewer: "sai", body: SHORT, reason: "too long" });
    // Unseen emails: reviewed unchanged after the corrections, so the check has something to judge.
    const what = await learning.autoLearn("how_to", () => spent++);
    expect(what).toMatch(/^lessons v1 /);
    expect(spent).toBe(2); // asked before proposing and before checking
    expect(await learning.autoLearn("how_to")).toBeNull(); // nothing new to learn from
  });

  it("stops when the budget or the daily limit says so", async () => {
    const store = new InboxStore(":memory:");
    const drafter = new Drafts();
    const agent = new InboxAgent(store, new FakeClassifier(), drafter, new FakeKnowledge(), { autonomyThreshold: 5, spotCheckRate: 0 });
    for (let i = 0; i < 3; i++) {
      const id = addEmail(store);
      await agent.process(id);
      agent.review(id, { action: "send", reviewer: "sai", body: SHORT, reason: "too long" });
    }
    const learning = new LearningService(store, miner, drafter);
    await expect(learning.autoLearn("how_to", () => { throw new Error("budget reached"); })).rejects.toThrow(/budget/);
    expect(store.lessonVersions("how_to")).toHaveLength(0);
  });
});
