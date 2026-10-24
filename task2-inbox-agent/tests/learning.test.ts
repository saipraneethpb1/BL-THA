import { beforeEach, describe, expect, it } from "vitest";
import { loadConfig } from "../src/config.js";
import { LearningError, LearningService, type LessonMiner, type MinedLessons } from "../src/feedback/learning.js";
import { similarity } from "../src/feedback/similarity.js";
import type { DraftRequest, Drafter } from "../src/llm/models.js";
import { InboxAgent } from "../src/pipeline.js";
import { buildApp } from "../src/server.js";
import { InboxStore } from "../src/store/db.js";
import { addEmail, FakeClassifier, FakeKnowledge } from "./fakes.js";

const LONG =
  "Hi Megan,\n\nThank you so much for reaching out to us today, we really appreciate it. " +
  "To change the logo, go to Design Settings and upload the new file there. " +
  "Please do not hesitate to contact us if you have any other questions at all.\n\nThe Lakeview team";
const SHORT = "Hi Megan,\n\nGo to Design Settings and upload the new logo.\n\nThe Lakeview team";
const LESSON = "Keep replies short: answer first, no filler thanks or closing offers.";

/** Behaves like a model that follows its lessons. */
class LessonAwareDrafter implements Drafter {
  calls: string[][] = [];
  constructor(private readonly unsafeWithLessons = false) {}
  async draft(req: DraftRequest) {
    this.calls.push(req.lessons);
    const shortened = req.lessons.includes(LESSON);
    const body = shortened ? (this.unsafeWithLessons ? `${SHORT}\nIt will be done by Friday.` : SHORT) : LONG;
    return {
      result: { subject: "Re: Logo", body, used_evidence: [1], follow_up_in_days: null, note_for_reviewer: "" },
      usage: { model: "fake", promptVersion: "t", inputTokens: 1, outputTokens: 1, costUsd: 0 },
    };
  }
}

class FakeMiner implements LessonMiner {
  seen: number[] = [];
  async mine(_c: string, _cur: unknown, corrections: { review_id: number }[]): Promise<MinedLessons> {
    this.seen = corrections.map((c) => c.review_id);
    return {
      lessons: [
        { text: LESSON, review_ids: [...this.seen, 999] }, // 999 does not exist: dropped
        { text: "An unsupported idea", review_ids: [] }, // no support: dropped
      ],
    };
  }
}

let store: InboxStore;
let drafter: LessonAwareDrafter;
let agent: InboxAgent;
let learning: LearningService;
let miner: FakeMiner;

beforeEach(() => {
  store = new InboxStore(":memory:");
  drafter = new LessonAwareDrafter();
  agent = new InboxAgent(store, new FakeClassifier(), drafter, new FakeKnowledge(), { autonomyThreshold: 2, spotCheckRate: 0 });
  miner = new FakeMiner();
  learning = new LearningService(store, miner, drafter);
});

/** A reviewer who always rewrites the reply to `body` (the short version by default). */
async function editedEmails(n: number, body = SHORT): Promise<void> {
  for (let i = 0; i < n; i++) {
    const id = addEmail(store);
    await agent.process(id);
    agent.review(id, { action: "send", reviewer: "sai", body, reason: "too long" });
  }
}

describe("similarity", () => {
  it("scores identical, different and partly overlapping texts", () => {
    expect(similarity("Go to Design Settings", "go to design settings!")).toBe(1);
    expect(similarity("alpha beta", "gamma delta")).toBe(0);
    expect(similarity(LONG, SHORT)).toBeGreaterThan(0.3);
    expect(similarity(LONG, SHORT)).toBeLessThan(0.7);
  });
});

describe("learning from edits", () => {
  it("needs enough corrections before proposing", async () => {
    await editedEmails(2);
    await expect(learning.propose("how_to", "sai")).rejects.toThrow(/need at least 3 corrections/);
  });

  it("proposes, measures on unseen emails, promotes, and resets autonomy", async () => {
    await editedEmails(3);
    const v = await learning.propose("how_to", "sai");
    const candidate = store.lessonVersion("how_to", v)!;
    expect(candidate.lessons).toEqual([{ text: LESSON, review_ids: miner.seen }]);
    expect(candidate.status).toBe("candidate");
    await expect(learning.propose("how_to", "sai")).rejects.toThrow(/already waiting/);

    await editedEmails(2); // happen after mining: the "unseen" emails
    expect(() => learning.promote("how_to", v, "sai", null)).toThrow(/regression check/);
    const ev = await learning.evaluate("how_to", v, "sai");
    expect(ev.unseen_n).toBe(2);
    expect(ev.unseen_after).toBe(1); // drafts now match what the reviewer sent
    expect(ev.unseen_before).toBeLessThan(0.7);
    expect(ev.recommended).toBe(true);

    store.saveAutonomy({ kind: "header", streak: 7, status: "supervised" }, [], null);
    learning.promote("how_to", v, "sai", null);
    expect(store.activeLessons("how_to").version).toBe(v);
    // Trust is re-earned in the categories these how-to emails belong to ("header").
    expect(store.autonomy("header").streak).toBe(0);

    const id = addEmail(store);
    await agent.process(id);
    expect(drafter.calls.at(-1)).toEqual([LESSON]); // new drafts use the lessons
    agent.review(id, { action: "send", reviewer: "sai" });
    const m = learning.metrics("how_to");
    expect(m.map((x) => [x.version, x.unchanged_rate])).toEqual([[0, 0], [1, 1]]);
  });

  it("refuses lessons that make drafts break the rules", async () => {
    drafter = new LessonAwareDrafter(true);
    learning = new LearningService(store, miner, drafter);
    agent = new InboxAgent(store, new FakeClassifier(), drafter, new FakeKnowledge(), { autonomyThreshold: 2, spotCheckRate: 0 });
    await editedEmails(5);
    const v = await learning.propose("how_to", "sai");
    const ev = await learning.evaluate("how_to", v, "sai");
    expect(ev.violations_after).toBeGreaterThan(ev.violations_before);
    expect(ev.recommended).toBe(false);
    expect(() => learning.promote("how_to", v, "sai", null)).toThrow(/needs a reason/);
    learning.reject("how_to", v, "sai", "promises dates");
    expect(store.lessonVersion("how_to", v)?.status).toBe("rejected");
  });

  it("does not judge without unseen emails, but allows a reasoned override", async () => {
    await editedEmails(3);
    const v = await learning.propose("how_to", "sai");
    const ev = await learning.evaluate("how_to", v, "sai");
    expect(ev.unseen_n).toBe(0);
    expect(ev.verdict).toMatch(/Not enough unseen/);
    learning.promote("how_to", v, "sai", "reviewers asked for this explicitly");
    expect(store.lessonVersion("how_to", v)?.note).toBe("reviewers asked for this explicitly");
  });

  it("rolls back one step at a time, then to no lessons", async () => {
    await editedEmails(3);
    const v1 = await learning.propose("how_to", "sai");
    await learning.evaluate("how_to", v1, "sai");
    learning.promote("how_to", v1, "sai", "first");
    expect(() => learning.promote("how_to", v1, "sai", "again")).toThrow(/not a candidate/);
    await editedEmails(3, `${SHORT}
Cheers!`); // the reviewer still edits under v1
    const v2 = await learning.propose("how_to", "sai");
    await learning.evaluate("how_to", v2, "sai");
    learning.promote("how_to", v2, "sai", "second");

    expect(learning.rollback("how_to", "sai", "complaints")).toBe(v1);
    expect(store.activeLessons("how_to").version).toBe(v1);
    expect(learning.rollback("how_to", "sai", "still bad")).toBe(0);
    expect(store.activeLessons("how_to")).toEqual({ version: 0, lessons: [] });
    expect(() => learning.rollback("how_to", "sai", "x")).toThrow(LearningError);
    const events = store.lessonEvents().map((e) => e.event);
    expect(events.filter((e) => e === "rolled_back")).toHaveLength(2);
  });
});

describe("learning API", () => {
  it("reports state without a key and refuses model steps with 503", async () => {
    const app = buildApp({ config: loadConfig({}), store, agent: null, learning: new LearningService(store, null, null) });
    await editedEmails(3);
    const state = (await app.inject({ url: "/api/learning" })).json();
    const howTo = state.categories.find((c: { category: string }) => c.category === "how_to");
    expect(howTo.corrections_since_active).toBe(3);
    const r = await app.inject({ method: "POST", url: "/api/learning/how_to/propose", payload: { actor: "sai" } });
    expect(r.statusCode).toBe(503);
    const bad = await app.inject({ method: "POST", url: "/api/learning/how_to/rollback", payload: { actor: "sai", note: "x" } });
    expect(bad.statusCode).toBe(409); // nothing active to roll back
  });
});
