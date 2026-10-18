import { describe, expect, it } from "vitest";
import { loadConfig } from "../src/config.js";
import { LearningService } from "../src/feedback/learning.js";
import { InboxAgent } from "../src/pipeline.js";
import { buildApp } from "../src/server.js";
import { InboxStore } from "../src/store/db.js";
import { FakeClassifier, FakeDrafter, FakeKnowledge } from "./fakes.js";

function app(withAgent = true) {
  const config = { ...loadConfig({}), autonomyThreshold: 2 };
  const store = new InboxStore(":memory:");
  const agent = withAgent
    ? new InboxAgent(store, new FakeClassifier(), new FakeDrafter(), new FakeKnowledge(), config, () => 1)
    : null;
  const learning = new LearningService(store, null, null);
  return { app: buildApp({ config, store, agent, learning }), store };
}

const email = {
  id: "X1", from: { name: "Megan", email: "megan@example.com" }, subject: "Logo", body: "How do I change the logo?",
};

describe("HTTP API", () => {
  it("ingests, drafts and reviews an email end to end", async () => {
    const { app: a } = app();
    expect((await a.inject({ method: "POST", url: "/api/emails", payload: email })).statusCode).toBe(201);
    expect((await a.inject({ method: "POST", url: "/api/emails", payload: email })).json()).toMatchObject({ added: false });
    const p = await a.inject({ method: "POST", url: "/api/emails/X1/process" });
    expect(p.json()).toMatchObject({ category: "how_to", autoSent: false });
    const r = await a.inject({ method: "POST", url: "/api/emails/X1/review", payload: { action: "send", reviewer: "sai" } });
    expect(r.json()).toMatchObject({ outcome: "sent_unchanged", autonomy: { streak: 1 } });
    const again = await a.inject({ method: "POST", url: "/api/emails/X1/review", payload: { action: "send", reviewer: "sai" } });
    expect(again.statusCode).toBe(409);
  });

  it("validates input", async () => {
    const { app: a } = app();
    expect((await a.inject({ method: "POST", url: "/api/emails", payload: { ...email, from: { name: "x", email: "nope" } } })).statusCode).toBe(422);
    await a.inject({ method: "POST", url: "/api/emails", payload: email });
    await a.inject({ method: "POST", url: "/api/emails/X1/process" });
    const bad = await a.inject({ method: "POST", url: "/api/emails/X1/review", payload: { action: "reject", reviewer: "sai", body: "x" } });
    expect(bad.statusCode).toBe(422); // reject needs a reason
  });

  it("reports drafting as unavailable without an API key, but still serves the inbox", async () => {
    const { app: a } = app(false);
    expect((await a.inject({ url: "/health" })).json()).toEqual({ status: "ok", drafting_enabled: false });
    await a.inject({ method: "POST", url: "/api/emails", payload: email });
    expect((await a.inject({ method: "POST", url: "/api/emails/X1/process" })).statusCode).toBe(503);
    expect((await a.inject({ url: "/api/emails" })).json()).toHaveLength(1);
  });

  it("loads the labelled dev set without exposing labels", async () => {
    const { app: a, store } = app(false);
    // Sent the way browsers and curl often do: JSON content type, empty body.
    const r = await a.inject({ method: "POST", url: "/api/seed/dev", headers: { "content-type": "application/json" } });
    expect(r.json()).toMatchObject({ added: 40, total: 40 });
    expect(Object.keys(store.getEmail("E001") ?? {})).not.toContain("labels");
  });

  it("serves the review screen and the autonomy overview", async () => {
    const { app: a } = app();
    expect((await a.inject({ url: "/" })).body).toContain("<title>Inbox Agent</title>");
    const auto = (await a.inject({ url: "/api/autonomy" })).json();
    expect(auto.categories).toHaveLength(11);
    expect(auto.categories.filter((c: { eligible: boolean }) => !c.eligible)).toHaveLength(6);
    expect(auto.threshold).toBe(2);
  });
});

describe("when the Claude API fails", () => {
  it("answers 502 with Anthropic's one-line reason, not a 500", async () => {
    const Anthropic = (await import("@anthropic-ai/sdk")).default;
    const msg = "Your credit balance is too low to access the Anthropic API.";
    class BrokeDrafter extends FakeDrafter {
      override async draft(): Promise<never> {
        throw new Anthropic.BadRequestError(400, { type: "error", error: { type: "invalid_request_error", message: msg } }, msg, new Headers());
      }
    }
    const config = { ...loadConfig({}), autonomyThreshold: 2 };
    const store = new InboxStore(":memory:");
    const agent = new InboxAgent(store, new FakeClassifier(), new BrokeDrafter(), new FakeKnowledge(), config, () => 1);
    const a = buildApp({ config, store, agent, learning: new LearningService(store, null, null) });
    await a.inject({ method: "POST", url: "/api/emails", payload: email });
    const r = await a.inject({ method: "POST", url: "/api/emails/X1/process" });
    expect(r.statusCode).toBe(502);
    expect(r.json()).toEqual({ error: `Claude API error: ${msg}` });
  });
});

describe("usage", () => {
  it("sums draft spend and remembers the last Claude failure", async () => {
    const Anthropic = (await import("@anthropic-ai/sdk")).default;
    const msg = "Your credit balance is too low to access the Anthropic API.";
    let broke = false;
    class MaybeBroke extends FakeDrafter {
      override async draft(...args: Parameters<FakeDrafter["draft"]>) {
        if (broke) throw new Anthropic.BadRequestError(400, { type: "error", error: { type: "invalid_request_error", message: msg } }, msg, new Headers());
        return super.draft(...args);
      }
    }
    const config = { ...loadConfig({}), autonomyThreshold: 2 };
    const store = new InboxStore(":memory:");
    const agent = new InboxAgent(store, new FakeClassifier(), new MaybeBroke(), new FakeKnowledge(), config, () => 1);
    const a = buildApp({ config, store, agent, learning: new LearningService(store, null, null) });
    expect((await a.inject({ url: "/api/usage" })).json()).toMatchObject({ service: "inbox", calls: 0, total_usd: 0, last_error: null });
    await a.inject({ method: "POST", url: "/api/emails", payload: email });
    await a.inject({ method: "POST", url: "/api/emails/X1/process" });
    const after = (await a.inject({ url: "/api/usage" })).json();
    expect(after.calls).toBe(1);
    expect(after.last_success_at).not.toBeNull();
    broke = true;
    await a.inject({ method: "POST", url: "/api/emails", payload: { ...email, id: "X2" } });
    await a.inject({ method: "POST", url: "/api/emails/X2/process" });
    expect((await a.inject({ url: "/api/usage" })).json().last_error.message).toBe(msg);
  });
});
