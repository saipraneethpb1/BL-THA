/**
 * What kinds of email the inbox receives, and the rules attached to each kind.
 *
 * The business: a small agency that builds and runs Brilliant Directories websites
 * for clients (the Knowledge Brain holds the product's help center). Another business
 * plugs in by editing this file: the categories, their risk, and whether they can
 * ever be sent without a human.
 *
 * Two dimensions are kept separate on purpose:
 *  - category: what the customer wants (decides the reply's job and its rules)
 *  - urgency:  how fast it matters (decides queue order and alerts)
 * "My site is down" is troubleshooting + critical, not its own category.
 */

export const CATEGORY_IDS = [
  "how_to",
  "troubleshooting",
  "billing_question",
  "refund_cancellation",
  "complaint",
  "project_request",
  "quote_request",
  "follow_up",
  "account_access",
  "feedback",
  "not_actionable",
] as const;

export type CategoryId = (typeof CATEGORY_IDS)[number];

export const URGENCY_LEVELS = ["low", "normal", "high", "critical"] as const;
export type Urgency = (typeof URGENCY_LEVELS)[number];

export type Risk = "low" | "medium" | "high";

/**
 * Parent types: the settings group each label sits under (the inbox sidebar and the
 * settings page group labels by these). A label is the precise kind of email; its parent
 * says which part of the business handles it.
 */
export const PARENT_IDS = ["support", "account", "sales", "care", "other"] as const;
export type ParentId = (typeof PARENT_IDS)[number];
export const PARENTS: Readonly<Record<ParentId, { label: string; description: string }>> = {
  support: { label: "Product support", description: "Questions and problems with the product itself" },
  account: { label: "Account & billing", description: "Logins, owners, invoices, refunds and cancellations" },
  sales: { label: "Sales & projects", description: "New work, changes to a site, and estimates" },
  care: { label: "Customer care", description: "Complaints, chasers and ideas: the relationship" },
  other: { label: "Other", description: "Mail that needs no reply" },
};

export interface CategoryRule {
  readonly id: CategoryId;
  readonly label: string;
  readonly parent: ParentId;
  /** Shown to the classifier. Says what belongs here and what does not. */
  readonly definition: string;
  readonly risk: Risk;
  /**
   * The default for whether this label may earn auto-send (after X unedited approvals in
   * a row, X set in settings). "never": a human always sends it unless an account's
   * settings deliberately switch auto-send on for it.
   */
  readonly autonomy: "eligible" | "never";
  /** Whether the draft should be grounded in the Knowledge Brain. */
  readonly needsKnowledge: boolean;
  /** Things a draft in this category must never do. Checked in code, not just prompted. */
  readonly mustNot: readonly string[];
  /** What should happen after the reply is sent. */
  readonly nextAction: string;
}

export const CATEGORIES: Readonly<Record<CategoryId, CategoryRule>> = {
  how_to: {
    id: "how_to",
    label: "How-to question",
    parent: "support",
    definition:
      "Asks how to do something in the product (settings, features, setup). Nothing is broken.",
    risk: "low",
    autonomy: "eligible",
    needsKnowledge: true,
    mustNot: ["state product steps that are not in the cited sources"],
    nextAction: "none",
  },
  troubleshooting: {
    id: "troubleshooting",
    label: "Something isn't working",
    parent: "support",
    definition:
      "Reports a problem: an error, a broken page, emails not arriving, payments failing, site down.",
    risk: "medium",
    autonomy: "eligible",
    needsKnowledge: true,
    mustNot: [
      "claim the issue is fixed",
      "blame the customer",
      "promise a fix time",
    ],
    nextAction:
      "if urgency is critical (site down, payments or signups failing): a team member has been alerted and will reply personally today, without promising when it will be fixed; otherwise follow up in 2 business days if the customer has not replied",
  },
  billing_question: {
    id: "billing_question",
    label: "Billing question",
    parent: "account",
    definition:
      "Asks about an invoice, a charge, plan prices or payment methods, without asking for money back or to cancel.",
    risk: "medium",
    autonomy: "eligible",
    needsKnowledge: true,
    mustNot: ["quote a price not in the sources", "change or waive a charge"],
    nextAction: "none",
  },
  refund_cancellation: {
    id: "refund_cancellation",
    label: "Refund or cancellation",
    parent: "account",
    definition: "Asks for money back, to cancel a plan or service, or to stop a subscription.",
    risk: "high",
    autonomy: "never",
    needsKnowledge: true,
    mustNot: [
      "promise or confirm a refund",
      "confirm a cancellation has been done",
      "state a refund amount or date",
    ],
    nextAction: "account owner reviews the request within 1 business day",
  },
  complaint: {
    id: "complaint",
    label: "Complaint",
    parent: "care",
    definition:
      "Expresses dissatisfaction with our work, service or communication. Can contain a problem report too.",
    risk: "high",
    autonomy: "never",
    needsKnowledge: false,
    mustNot: ["argue with the customer", "admit legal liability", "offer compensation"],
    nextAction: "senior team member calls or replies personally within 1 business day",
  },
  project_request: {
    id: "project_request",
    label: "New project or change request",
    parent: "sales",
    definition:
      "Asks us to build, change or add something on their site (new feature, redesign, integration).",
    risk: "high",
    autonomy: "never",
    needsKnowledge: true,
    mustNot: ["commit to a price", "commit to a delivery date", "agree to scope"],
    nextAction: "offer a scoping call; hand over to the Scope Builder after the meeting",
  },
  quote_request: {
    id: "quote_request",
    label: "Quote request",
    parent: "sales",
    definition: "Asks how much something would cost or for a formal estimate.",
    risk: "high",
    autonomy: "never",
    needsKnowledge: true,
    mustNot: ["state a price or hours estimate", "promise a discount"],
    nextAction: "prepare an estimate; reply with a range only after human approval",
  },
  follow_up: {
    id: "follow_up",
    label: "Follow-up on earlier request",
    parent: "care",
    definition:
      "Chases a previous email, ticket or job ('any update?', 'did you get my email?').",
    risk: "medium",
    autonomy: "never",
    needsKnowledge: false,
    mustNot: ["invent a status or progress update", "promise a completion date"],
    nextAction: "owner of the original request updates the customer the same day",
  },
  account_access: {
    id: "account_access",
    label: "Login or account access",
    parent: "account",
    definition:
      "Cannot log in, wants a password reset, a change of account owner or email, or access for someone else.",
    risk: "high",
    autonomy: "never",
    needsKnowledge: true,
    mustNot: [
      "change account details by email",
      "share credentials",
      "grant access without identity verification",
    ],
    nextAction: "verify identity before any account change",
  },
  feedback: {
    id: "feedback",
    label: "Feedback or idea",
    parent: "care",
    definition: "Praise, a suggestion, or a feature idea, with no request that needs action now.",
    risk: "low",
    autonomy: "eligible",
    needsKnowledge: false,
    mustNot: ["promise that the idea will be built"],
    nextAction: "log the idea",
  },
  not_actionable: {
    id: "not_actionable",
    label: "No reply needed",
    parent: "other",
    definition:
      "Spam, vendor sales pitches, auto-replies, newsletters, or 'thanks!' messages that need no answer.",
    risk: "low",
    autonomy: "eligible",
    needsKnowledge: false,
    mustNot: ["reply to spam or phishing"],
    nextAction: "archive",
  },
};

/** Whether a label may earn auto-send by default (settings can override per account). */
export function eligibleByDefault(category: CategoryId): boolean {
  return CATEGORIES[category].autonomy === "eligible";
}

export function canEverAutoSend(
  category: CategoryId,
  urgency: Urgency,
  eligible: boolean = eligibleByDefault(category),
): boolean {
  // Critical issues always reach a human, whatever their label or settings.
  return eligible && urgency !== "critical";
}
