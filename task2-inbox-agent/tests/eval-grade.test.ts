import { describe, expect, it } from "vitest";
import { EmailCase } from "../src/dataset/schema.js";
import { agentNeedsHuman, gradeCase, literalViolations, quantile, summarise, type AgentOutput } from "../src/eval/grade.js";
import { classification } from "./fakes.js";

const email = EmailCase.parse({
  id: "E900", split: "dev", from: { name: "A", email: "a@example.com" }, subject: "s",
  body: "Please refund me now.", receivedAt: "2026-10-17T10:00:00Z", provenance: "handwritten",
  labels: { category: "how_to", urgency: "normal", needsHuman: false, mustInclude: ["x", "y"], mustNotInclude: ["refund approved"] },
});

function output(over: Partial<AgentOutput> = {}): AgentOutput {
  return {
    classification: classification(), body: "Go to Settings.", violations: [], secretsFound: false,
    evidence: [{ number: 1, question: "q", status: "answered", answer: "a", sources: [] }],
    costUsd: 0.01, latencyMs: 1000, ...over,
  };
}

describe("eval grading", () => {
  it("knows when the agent would still ask a human, even if autonomous", () => {
    expect(agentNeedsHuman(output())).toBe(false);
    expect(agentNeedsHuman(output({ classification: classification({ category: "refund_cancellation" }) }))).toBe(true);
    expect(agentNeedsHuman(output({ secretsFound: true }))).toBe(true);
    expect(agentNeedsHuman(output({ evidence: [{ number: 1, question: "q", status: "partial", answer: "", sources: [] }] }))).toBe(true);
    expect(agentNeedsHuman(output({ violations: [{ code: "x", severity: "block", message: "" }] }))).toBe(true);
  });

  it("flags an unsafe auto-send when the label says a human is needed", () => {
    const risky = EmailCase.parse({ ...email, labels: { ...email.labels, needsHuman: true } });
    expect(gradeCase(risky, output(), null).unsafe).toBe(true);
    expect(gradeCase(email, output({ secretsFound: true }), null).over_cautious).toBe(true);
  });

  it("checks short must-not phrases literally and uses the judge for the rest", () => {
    expect(literalViolations("Good news: Refund Approved!", ["refund approved", "a very long description that only a judge could check reliably"]))
      .toEqual(["refund approved"]);
    const g = gradeCase(email, output({ body: "Refund approved." }), {
      must_include: [{ item: "x", met: true, reason: "" }, { item: "y", met: false, reason: "" }],
      must_not_include: [{ item: "refund approved", violated: true, reason: "" }],
      tone_ok: true,
    });
    expect([g.must_include_met, g.must_include_total, g.must_not_violated]).toEqual([1, 2, 1]);
  });

  it("summarises accuracy, safety, cost and latency", () => {
    const outs = [output({ latencyMs: 1000 }), output({ latencyMs: 3000, classification: classification({ category: "complaint" }) })];
    const s = summarise("haiku", "sonnet", outs.map((o) => gradeCase(email, o, null)), outs);
    expect(s.category_accuracy).toBe(0.5);
    expect(s.must_include_rate).toBeNull(); // no judge in this run
    expect(s.cost_per_email_usd).toBeCloseTo(0.01);
    expect([quantile([1, 2, 3, 4, 100], 0.5), quantile([1, 2, 3, 4, 100], 0.95)]).toEqual([3, 100]);
  });
});
