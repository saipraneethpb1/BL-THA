/**
 * Rule checks on every draft, in code. The prompt asks the model to behave; these rules
 * make sure that when it does not, the draft can never go out on its own.
 *
 * "block" violations stop auto-send and are shown to the reviewer.
 * "warn" violations are shown to the reviewer but do not block.
 */
import type { Secret } from "../email/pii.js";
import type { CategoryId } from "../email/taxonomy.js";

export interface Violation {
  code: string;
  severity: "block" | "warn";
  message: string;
}

export interface PolicyContext {
  category: CategoryId;
  secondary: readonly CategoryId[];
  secrets: readonly Secret[];
  needsKnowledge: boolean;
  evidenceCount: number;
  usedEvidence: readonly number[];
  knowledgeUnavailable: boolean;
}

const MONEY = /(?:[$€£₹]\s?\d|\b\d[\d,.]*\s?(?:usd|dollars|eur|euros|gbp|inr|rupees|rs\.?)\b)/i;
const HOURS = /\b\d+(?:\.\d+)?\s?(?:-|to)?\s?\d*\s?(?:hours?|hrs?|days? of work|man-days?)\b/i;
const REFUND_DONE =
  /\b(?:refund|money)\b[^.!?\n]{0,40}\b(?:has been|is|was|will be)\s+(?:approved|processed|issued|confirmed|sent|refunded)\b/i;
const CANCEL_DONE =
  /\b(?:(?:has|have) been|is now|was)\s+(?:cancell?ed|terminated|closed)\b/i;
const FIXED_CLAIM = /\b(?:is|has been|have been|are)\s+(?:now\s+)?(?:fixed|resolved|working again|back up)\b/i;
const DEADLINE =
  /\b(?:by|before|until)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|tonight|end of (?:the )?(?:day|week|month)|\d{1,2}(?:st|nd|rd|th)?\s+\w+)\b|\b(?:will be (?:ready|done|live|fixed|delivered))\b/i;
const ACCOUNT_CHANGED =
  /\b(?:we(?:'ve| have)? (?:changed|updated|reset|transferred)|your new password is|logged you in)\b/i;

// "Nothing has been cancelled yet" must not count as a confirmation: ignore a match
// that a negation word sits directly in front of, in the same clause. A comma ends the
// clause, so "No problem, your refund has been approved" is still caught.
const NEGATION = /(?:\b(?:no|not|nothing|never|none)\b|n't)[^.!?\n,;:]{0,15}$/i;
function affirms(re: RegExp, body: string): boolean {
  const g = new RegExp(re.source, re.flags.includes("g") ? re.flags : `${re.flags}g`);
  for (const m of body.matchAll(g)) {
    if (!NEGATION.test(body.slice(Math.max(0, m.index - 30), m.index))) return true;
  }
  return false;
}

const has = (cats: readonly CategoryId[], ...ids: CategoryId[]) => ids.some((id) => cats.includes(id));

export function checkDraft(body: string, ctx: PolicyContext): Violation[] {
  const v: Violation[] = [];
  const cats = [ctx.category, ...ctx.secondary];

  for (const s of ctx.secrets) {
    if (body.includes(s.value)) {
      v.push({ code: "secret_echoed", severity: "block", message: `Reply repeats the customer's ${s.kind.replace("_", " ")}.` });
    }
  }
  if (affirms(REFUND_DONE, body)) {
    v.push({ code: "refund_promised", severity: "block", message: "Reply promises or confirms a refund." });
  }
  if (affirms(CANCEL_DONE, body) && has(cats, "refund_cancellation")) {
    v.push({ code: "cancellation_confirmed", severity: "block", message: "Reply says a cancellation is done." });
  }
  if (has(cats, "quote_request", "project_request", "billing_question", "refund_cancellation") && MONEY.test(body)) {
    v.push({ code: "price_stated", severity: "block", message: "Reply states a price or amount." });
  }
  if (has(cats, "quote_request", "project_request") && HOURS.test(body)) {
    v.push({ code: "effort_stated", severity: "block", message: "Reply states an hours or effort estimate." });
  }
  if (DEADLINE.test(body)) {
    v.push({ code: "deadline_promised", severity: "block", message: "Reply commits to a delivery or fix date." });
  }
  if (has(cats, "troubleshooting", "complaint", "follow_up") && FIXED_CLAIM.test(body)) {
    v.push({ code: "fixed_claimed", severity: "block", message: "Reply claims the problem is fixed." });
  }
  if (has(cats, "account_access") && ACCOUNT_CHANGED.test(body)) {
    v.push({ code: "account_changed", severity: "block", message: "Reply says account details were changed." });
  }
  if (ctx.knowledgeUnavailable) {
    v.push({ code: "knowledge_unavailable", severity: "block", message: "Knowledge Brain was unreachable; product facts are unchecked." });
  } else if (ctx.needsKnowledge && ctx.evidenceCount > 0 && ctx.usedEvidence.length === 0) {
    v.push({ code: "no_evidence_used", severity: "warn", message: "Reply cites none of the knowledge-base evidence." });
  }
  const bad = ctx.usedEvidence.filter((n) => n < 1 || n > ctx.evidenceCount);
  if (bad.length > 0) {
    v.push({ code: "bad_citation", severity: "block", message: `Reply cites evidence that does not exist: ${bad.join(", ")}.` });
  }
  if (body.trim().length < 20) {
    v.push({ code: "empty_reply", severity: "block", message: "Reply is empty or too short." });
  }
  return v;
}
