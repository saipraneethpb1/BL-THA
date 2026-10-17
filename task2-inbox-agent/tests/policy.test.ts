import { describe, expect, it } from "vitest";
import { checkDraft, type PolicyContext } from "../src/drafting/policy.js";
import { findSecrets, redact } from "../src/email/pii.js";
import { HttpKnowledgeClient, KnowledgeUnavailableError } from "../src/knowledge-client/client.js";

const ctx = (over: Partial<PolicyContext> = {}): PolicyContext => ({
  category: "how_to", secondary: [], secrets: [], needsKnowledge: true,
  evidenceCount: 1, usedEvidence: [1], knowledgeUnavailable: false, ...over,
});
const codes = (body: string, over: Partial<PolicyContext> = {}) => checkDraft(body, ctx(over)).map((v) => v.code);
const OK = "Hi Sam, here is how to do it: open Settings and press Save. The Lakeview team";

describe("policy rules", () => {
  it("passes a normal grounded reply", () => {
    expect(codes(OK)).toEqual([]);
  });

  it.each([
    ["Your refund has been approved and will arrive soon.", "refund_promised"],
    ["Good news: the money was refunded today.", "refund_promised"],
    ["We will have it ready by Friday.", "deadline_promised"],
    ["It will be live tomorrow.", "deadline_promised"],
  ])("blocks %s", (body, code) => {
    expect(codes(`${body} ${OK}`)).toContain(code);
  });

  it("blocks prices and hours only where they are commitments", () => {
    expect(codes(`A redesign would be $1,200. ${OK}`, { category: "quote_request" })).toContain("price_stated");
    expect(codes(`It should take about 12 hours. ${OK}`, { category: "project_request" })).toContain("effort_stated");
    expect(codes(`Coupons can take $10 off. ${OK}`)).not.toContain("price_stated"); // how-to fact
  });

  it("blocks claims that a problem is fixed, and cancellations confirmed", () => {
    expect(codes(`The issue has been fixed. ${OK}`, { category: "troubleshooting" })).toContain("fixed_claimed");
    expect(codes(`Your plan has been cancelled. ${OK}`, { category: "refund_cancellation", usedEvidence: [] })).toContain("cancellation_confirmed");
  });

  it("blocks account changes by email", () => {
    expect(codes(`We have changed the owner to you. ${OK}`, { category: "account_access" })).toContain("account_changed");
  });

  it("checks citations", () => {
    expect(codes(OK, { usedEvidence: [] })).toEqual(["no_evidence_used"]);
    expect(codes(OK, { usedEvidence: [3] })).toContain("bad_citation");
    expect(codes(OK, { knowledgeUnavailable: true })).toContain("knowledge_unavailable");
  });
});

describe("secrets", () => {
  it("finds real card numbers and ignores numbers that fail the Luhn check", () => {
    expect(findSecrets("card 4111 1111 1111 1111 exp 09/28 CVV: 123").map((s) => s.kind)).toEqual(["card_number", "card_cvv"]);
    expect(findSecrets("order 1234 5678 9012 3456")).toEqual([]);
  });

  it("finds pasted passwords but not talk about passwords", () => {
    expect(findSecrets("My password is Sunrise@2024 and")).toEqual([{ kind: "password", value: "Sunrise@2024" }]);
    expect(findSecrets("login pass Tailor#2021 if u need")).toEqual([{ kind: "password", value: "Tailor#2021" }]);
    expect(findSecrets("Please send me a password reset link")).toEqual([]);
  });

  it("redacts every occurrence", () => {
    const s = findSecrets("pass: hunter22 ... again hunter22");
    expect(redact("pass: hunter22 ... again hunter22", s)).toBe("pass: [REDACTED PASSWORD] ... again [REDACTED PASSWORD]");
  });
});

describe("Knowledge Brain client", () => {
  const good = {
    status: "answered", answer: "Do X.", sources: [], needs_human: false, escalation_id: null,
    missing_info: null, cost_usd: 0, latency_ms: 1,
  };
  const respond = (...replies: (number | object)[]): typeof fetch => {
    let i = 0;
    return (async () => {
      const r = replies[i++];
      if (typeof r === "number") return new Response("{}", { status: r });
      return new Response(JSON.stringify(r), { status: 200 });
    }) as typeof fetch;
  };

  it("retries server errors, then succeeds", async () => {
    const c = new HttpKnowledgeClient("http://kb", "k", 1000, 2, respond(503, good));
    await expect(c.query("q")).resolves.toMatchObject({ status: "answered" });
  });

  it("does not retry client errors", async () => {
    const c = new HttpKnowledgeClient("http://kb", "k", 1000, 2, respond(401, good));
    await expect(c.query("q")).rejects.toBeInstanceOf(KnowledgeUnavailableError);
  });

  it("rejects responses that break the contract", async () => {
    const c = new HttpKnowledgeClient("http://kb", "k", 1000, 0, respond({ answer: 42 }));
    await expect(c.query("q")).rejects.toThrow(/unexpected response shape/);
  });
});
