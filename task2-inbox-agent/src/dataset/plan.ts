/**
 * Coverage targets for the email dataset. `npm run dataset:check` fails until the
 * dataset meets them, so "covers every kind of request" is checked, not claimed.
 *
 * 60 emails: 40 "dev" (used while building and tuning prompts) and 20 "test"
 * (held out; only run for the numbers reported in EVALS.md).
 */
import type { CategoryId } from "../email/taxonomy.js";
import type { TRAITS } from "./schema.js";

export const CATEGORY_TARGETS: Readonly<Record<CategoryId, { dev: number; test: number }>> = {
  how_to: { dev: 8, test: 4 },
  troubleshooting: { dev: 6, test: 3 },
  billing_question: { dev: 3, test: 2 },
  refund_cancellation: { dev: 4, test: 2 },
  complaint: { dev: 3, test: 2 },
  project_request: { dev: 4, test: 2 },
  quote_request: { dev: 3, test: 1 },
  follow_up: { dev: 3, test: 1 },
  account_access: { dev: 2, test: 1 },
  feedback: { dev: 2, test: 1 },
  not_actionable: { dev: 2, test: 1 },
};

/** Minimum number of emails (across both splits) showing each hard trait. */
export const TRAIT_MINIMUMS: Readonly<Record<(typeof TRAITS)[number], number>> = {
  multi_intent: 5,
  angry_tone: 4,
  vague: 4,
  long_thread: 3,
  non_english: 2,
  typos_informal: 4,
  prompt_injection: 3,
  contains_pii: 3,
  not_in_kb: 5,
  image_only_answer: 2,
  needs_history: 4,
};

/** At least this many emails must be marked critical (site down, payments failing). */
export const MIN_CRITICAL = 3;
