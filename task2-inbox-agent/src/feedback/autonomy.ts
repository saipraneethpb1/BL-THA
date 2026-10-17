/**
 * When may a kind of email be sent without a human? Pure functions, no I/O, so every
 * rule is unit-tested and easy to explain.
 *
 * Definitions (our answers to the brief's open questions):
 * - "Same kind" = same category (e.g. how_to). One counter per category.
 * - "Without changing" = the sent text equals the draft after trimming and collapsing
 *   whitespace. Any word change counts as an edit. Strict on purpose: easy to audit.
 * - Earning freedom: `threshold` (20) unedited approvals in a row, in a category that
 *   taxonomy.ts marks eligible. Drafts with a blocking policy violation never count.
 * - Losing freedom: any edit, rejection or escalation of that category; any human
 *   correction of a spot-checked auto-sent email; or a manual revoke. The counter goes
 *   back to 0 and must be earned again from scratch.
 * - Even when autonomous, an individual email still goes to a human if it is critical,
 *   angry, had secrets removed, looks like a prompt injection, or the knowledge base
 *   could not answer its questions.
 */
import type { CategoryId, Urgency } from "../email/taxonomy.js";
import { canEverAutoSend } from "../email/taxonomy.js";

export type AutonomyStatus = "supervised" | "autonomous";
export type ReviewOutcome = "sent_unchanged" | "edited" | "rejected" | "escalated";

export interface AutonomyState {
  kind: CategoryId;
  streak: number;
  status: AutonomyStatus;
}

export interface AutonomyEvent {
  kind: CategoryId;
  event: "streak_up" | "streak_reset" | "granted" | "revoked" | "not_eligible";
  detail: string;
}

export function normalizeText(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

export function isUnchanged(draft: string, sent: string): boolean {
  return normalizeText(draft) === normalizeText(sent);
}

export function applyReview(
  state: AutonomyState,
  outcome: ReviewOutcome,
  opts: { eligible: boolean; hadBlockingViolation: boolean; threshold: number },
): { state: AutonomyState; events: AutonomyEvent[] } {
  const events: AutonomyEvent[] = [];
  if (!opts.eligible) {
    return { state, events: [{ kind: state.kind, event: "not_eligible", detail: "category can never auto-send" }] };
  }
  if (outcome === "sent_unchanged" && !opts.hadBlockingViolation) {
    const streak = state.streak + 1;
    events.push({ kind: state.kind, event: "streak_up", detail: `${streak}/${opts.threshold}` });
    if (state.status === "supervised" && streak >= opts.threshold) {
      events.push({ kind: state.kind, event: "granted", detail: `${streak} unedited approvals in a row` });
      return { state: { ...state, streak, status: "autonomous" }, events };
    }
    return { state: { ...state, streak }, events };
  }
  const why = opts.hadBlockingViolation && outcome === "sent_unchanged" ? "sent despite a policy violation" : outcome;
  events.push({ kind: state.kind, event: "streak_reset", detail: why });
  if (state.status === "autonomous") {
    events.push({ kind: state.kind, event: "revoked", detail: `human ${why}` });
  }
  return { state: { ...state, streak: 0, status: "supervised" }, events };
}

export function revoke(state: AutonomyState, reason: string): { state: AutonomyState; events: AutonomyEvent[] } {
  return {
    state: { ...state, streak: 0, status: "supervised" },
    events: [{ kind: state.kind, event: "revoked", detail: reason }],
  };
}

export interface SendCheck {
  category: CategoryId;
  urgency: Urgency;
  sentiment: string;
  hasBlockingViolation: boolean;
  secretsFound: boolean;
  suspectedInjection: boolean;
  knowledgeGap: boolean;
}

/** Returns null if this email may be auto-sent, otherwise the reason it may not. */
export function autoSendBlocker(state: AutonomyState, c: SendCheck): string | null {
  if (state.status !== "autonomous") return `category is supervised (${state.streak} clean approvals so far)`;
  if (!canEverAutoSend(c.category, c.urgency)) return "category or urgency always needs a human";
  if (c.hasBlockingViolation) return "draft failed a policy check";
  if (c.secretsFound) return "email contained secrets";
  if (c.suspectedInjection) return "email looks like a prompt injection";
  if (c.knowledgeGap) return "knowledge base could not answer every question";
  if (c.sentiment === "angry") return "customer is angry";
  return null;
}
