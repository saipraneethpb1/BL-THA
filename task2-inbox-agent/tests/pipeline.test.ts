import { beforeEach, describe, expect, it } from "vitest";
import { InboxAgent } from "../src/pipeline.js";
import { InboxStore } from "../src/store/db.js";
import { addEmail, classification, FakeClassifier, FakeDrafter, FakeKnowledge } from "./fakes.js";

let store: InboxStore;
let classifier: FakeClassifier;
let drafter: FakeDrafter;
let kb: FakeKnowledge;
let agent: InboxAgent;

function makeAgent(random = () => 0.99): InboxAgent {
  return new InboxAgent(store, classifier, drafter, kb, { autonomyThreshold: 3, spotCheckRate: 0.2 }, random);
}

beforeEach(() => {
  store = new InboxStore(":memory:");
  classifier = new FakeClassifier();
  drafter = new FakeDrafter();
  kb = new FakeKnowledge();
  agent = makeAgent();
});

async function approveOne(): Promise<string> {
  const id = addEmail(store);
  await agent.process(id);
  agent.review(id, { action: "send", reviewer: "sai" });
  return id;
}

describe("processing an email", () => {
  it("asks the Knowledge Brain each question and waits for a human", async () => {
    classifier.result = classification({ questions: ["q1", "q2"] });
    const id = addEmail(store);
    const r = await agent.process(id);
    expect(kb.questions).toEqual(["q1", "q2"]);
    expect(drafter.seen[0]?.evidence.map((e) => e.number)).toEqual([1, 2]);
    expect(r.autoSent).toBe(false);
    expect(r.blocker).toMatch(/supervised/);
    expect(store.getEmail(id)?.status).toBe("drafted");
  });

  it("skips the Knowledge Brain for categories that do not need it", async () => {
    classifier.result = classification({ category: "complaint" });
    await agent.process(addEmail(store));
    expect(kb.questions).toEqual([]);
  });

  it("still drafts when the Knowledge Brain is down, but blocks auto-send", async () => {
    kb.down = true;
    const r = await agent.process(addEmail(store));
    expect(r.violations.map((v) => v.code)).toContain("knowledge_unavailable");
  });

  it("removes secrets before the model sees them and blocks a draft that repeats them", async () => {
    drafter.result = { ...drafter.result, body: "Thanks, we'll charge 4111 1111 1111 1111 today." };
    const r = await agent.process(addEmail(store, "Charge my card 4111 1111 1111 1111 please"));
    expect(classifier.seen[0]?.body).toContain("[REDACTED CARD DETAILS]");
    expect(classifier.seen[0]?.body).not.toContain("4111");
    expect(r.violations.map((v) => v.code)).toContain("secret_echoed");
  });

  it("suggests archiving spam without calling the drafter", async () => {
    classifier.result = classification({ category: "not_actionable", questions: [], urgency: "low" });
    const id = addEmail(store);
    await agent.process(id);
    expect(drafter.seen).toHaveLength(0);
    expect(store.latestDraft(id)?.note_for_reviewer).toMatch(/archive/);
  });
});

describe("earning and losing autonomy", () => {
  it("auto-sends after the threshold of unedited approvals, then spot checks can revoke it", async () => {
    for (let i = 0; i < 3; i++) await approveOne();
    expect(store.autonomy("how_to").status).toBe("autonomous");

    agent = makeAgent(() => 0.0); // force a spot check
    const id = addEmail(store);
    const r = await agent.process(id);
    expect(r.autoSent).toBe(true);
    expect(store.getEmail(id)?.status).toBe("sent");
    expect(store.pendingSpotChecks().map((x) => x.email_id)).toContain(id);

    agent.review(id, { action: "spot_check", ok: false, reviewer: "sai", reason: "wrong menu path" });
    expect(store.autonomy("how_to")).toMatchObject({ status: "supervised", streak: 0 });
  });

  it("an edit resets the streak", async () => {
    await approveOne();
    await approveOne();
    const id = addEmail(store);
    await agent.process(id);
    agent.review(id, { action: "send", reviewer: "sai", body: "Hi Megan, different words.\n\nThe Lakeview team" });
    expect(store.autonomy("how_to").streak).toBe(0);
  });

  it("whitespace-only changes still count as unchanged", async () => {
    const id = addEmail(store);
    await agent.process(id);
    const draft = store.latestDraft(id)!;
    agent.review(id, { action: "send", reviewer: "sai", body: `  ${draft.body.replace(/\n/g, "\n\n")}  ` });
    expect(store.autonomy("how_to").streak).toBe(1);
  });

  it("never counts approvals for categories that can never auto-send", async () => {
    classifier.result = classification({ category: "refund_cancellation" });
    drafter.result = { ...drafter.result, body: "Hi, we've passed your request to the account owner.", used_evidence: [] };
    for (let i = 0; i < 5; i++) await approveOne();
    expect(store.autonomy("refund_cancellation").status).toBe("supervised");
    expect(store.autonomyEvents().some((e) => e.event === "not_eligible")).toBe(true);
  });

  it("keeps individual risky emails with a human even in an autonomous category", async () => {
    for (let i = 0; i < 3; i++) await approveOne();
    classifier.result = classification({ suspected_injection: true });
    const r1 = await agent.process(addEmail(store));
    expect(r1.blocker).toMatch(/injection/);
    classifier.result = classification({ urgency: "critical" });
    const r2 = await agent.process(addEmail(store));
    expect(r2.blocker).toMatch(/always needs a human/);
    classifier.result = classification();
    kb.status = "insufficient_evidence";
    const r3 = await agent.process(addEmail(store));
    expect(r3.blocker).toMatch(/knowledge base/);
  });

  it("refuses to review an email twice", async () => {
    const id = await approveOne();
    expect(() => agent.review(id, { action: "send", reviewer: "sai" })).toThrow(/not awaiting review/);
  });

  it("manual revoke drops a category back to supervised", async () => {
    for (let i = 0; i < 3; i++) await approveOne();
    agent.revokeAutonomy("how_to", "new product release");
    expect(store.autonomy("how_to")).toMatchObject({ status: "supervised", streak: 0 });
  });
});
