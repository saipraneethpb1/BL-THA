/**
 * The Inbox Agent: one email in, one reviewed (or, when earned, auto-sent) reply out.
 *
 *   redact secrets -> classify -> ask the Knowledge Brain -> draft -> policy check
 *     -> auto-send if every gate passes, otherwise wait for a human
 *   human review -> sent / edited / rejected / escalated -> autonomy counter updated
 */
import type { Config } from "./config.js";
import { checkDraft, type Violation } from "./drafting/policy.js";
import { findSecrets, redact } from "./email/pii.js";
import { CATEGORIES, canEverAutoSend, type CategoryId } from "./email/taxonomy.js";
import { readSettings, type InboxSettings } from "./settings.js";
import {
  applyReview,
  autoSendBlocker,
  isUnchanged,
  revoke,
  type ReviewOutcome,
} from "./feedback/autonomy.js";
import { KnowledgeUnavailableError, type KnowledgeClient } from "./knowledge-client/client.js";
import type { Classification, Classifier, Drafter, EvidenceItem } from "./llm/models.js";
import type { InboxStore } from "./store/db.js";

export interface ProcessResult {
  draftId: number;
  category: CategoryId;
  autoSent: boolean;
  blocker: string | null;
  violations: Violation[];
}

export type ReviewAction =
  | { action: "send"; subject?: string; body?: string; reviewer: string; reason?: string }
  | { action: "reject"; subject?: string; body: string; reviewer: string; reason: string }
  | { action: "escalate"; reviewer: string; reason: string }
  | { action: "archive"; reviewer: string; reason?: string }
  | { action: "spot_check"; ok: boolean; reviewer: string; reason?: string };

export class ReviewError extends Error {
  override readonly name = "ReviewError";
}

export class InboxAgent {
  constructor(
    private readonly store: InboxStore,
    private readonly classifier: Classifier,
    private readonly drafter: Drafter,
    private readonly kb: KnowledgeClient,
    private readonly config: Pick<Config, "autonomyThreshold" | "spotCheckRate">,
    private readonly random: () => number = Math.random,
  ) {}

  async process(emailId: string): Promise<ProcessResult> {
    const email = this.store.getEmail(emailId);
    if (!email) throw new ReviewError(`email ${emailId} not found`);
    const started = performance.now();

    const allText = [email.body, ...email.thread.map((t) => t.body)].join("\n");
    const secrets = findSecrets(allText);
    const forModel = {
      fromName: email.from_name,
      subject: email.subject,
      body: redact(email.body, secrets),
      thread: email.thread.map((t) => ({ ...t, body: redact(t.body, secrets) })),
    };

    let classification: Classification;
    let cost = 0;
    const models: Record<string, string> = {};
    try {
      const c = await this.classifier.classify(forModel);
      classification = c.result;
      cost += c.usage.costUsd;
      models.classify = `${c.usage.model}@${c.usage.promptVersion}`;
    } catch (err) {
      this.store.setEmailStatus(emailId, "failed", `classification failed: ${String(err)}`);
      throw err;
    }

    const category = classification.category;
    const cats = [category, ...classification.secondary];
    const needsKnowledge = cats.some((c) => CATEGORIES[c].needsKnowledge);

    // Ask the Knowledge Brain each product question, in parallel.
    const evidence: EvidenceItem[] = [];
    let knowledgeUnavailable = false;
    if (needsKnowledge && classification.questions.length > 0) {
      const answers = await Promise.allSettled(classification.questions.map((q) => this.kb.query(q)));
      answers.forEach((a, i) => {
        const question = classification.questions[i] ?? "";
        if (a.status === "fulfilled") {
          cost += a.value.cost_usd; // the email's true cost includes its knowledge lookups
          evidence.push({
            number: evidence.length + 1,
            question,
            status: a.value.status,
            answer: a.value.answer,
            sources: a.value.sources.map((s) => ({ title: s.title, url: s.url })),
          });
        } else if (a.reason instanceof KnowledgeUnavailableError) {
          knowledgeUnavailable = true;
        } else {
          throw a.reason;
        }
      });
    }
    const knowledgeGap = knowledgeUnavailable || evidence.some((e) => e.status !== "answered");

    let subject = `Re: ${email.subject}`;
    let body = "";
    let note = "Suggested action: archive, no reply needed.";
    let followUp: number | null = null;
    let usedEvidence: number[] = [];
    if (category !== "not_actionable") {
      const active = this.store.activeLessons(category);
      models.lessons_version = String(active.version); // lets us compare edit rates per version
      const d = await this.drafter.draft({
        email: forModel,
        classification,
        evidence,
        lessons: active.lessons.map((l) => l.text),
      });
      ({ subject, body } = d.result);
      note = d.result.note_for_reviewer;
      followUp = d.result.follow_up_in_days;
      usedEvidence = d.result.used_evidence;
      cost += d.usage.costUsd;
      models.draft = `${d.usage.model}@${d.usage.promptVersion}`;
    }

    const violations =
      category === "not_actionable"
        ? []
        : checkDraft(body, {
            category,
            secondary: classification.secondary,
            secrets,
            needsKnowledge,
            evidenceCount: evidence.length,
            usedEvidence,
            knowledgeUnavailable,
          });
    const state = this.store.autonomy(category);
    const blocker = autoSendBlocker(state, {
      category,
      eligible: this.settings().autoSend[category],
      urgency: classification.urgency,
      sentiment: classification.sentiment,
      hasBlockingViolation: violations.some((v) => v.severity === "block"),
      secretsFound: secrets.length > 0,
      suspectedInjection: classification.suspected_injection,
      knowledgeGap: needsKnowledge && knowledgeGap,
    });

    const draftId = this.store.saveDraft({
      email_id: emailId,
      classification,
      evidence,
      subject,
      body,
      note_for_reviewer: note,
      follow_up_in_days: followUp,
      violations,
      secrets_found: secrets.length > 0,
      auto_send_blocker: blocker,
      models,
      cost_usd: cost,
      latency_ms: performance.now() - started,
    });

    const autoSent = blocker === null;
    if (autoSent) {
      if (category === "not_actionable") {
        this.store.setEmailStatus(emailId, "archived");
      } else {
        this.store.send(emailId, email.from_email, subject, body, "auto");
        this.store.setEmailStatus(emailId, "sent");
      }
      this.store.recordReview({
        emailId, draftId, outcome: category === "not_actionable" ? "archived" : "auto_sent",
        category, finalSubject: subject, finalBody: body, reviewer: "agent",
        spotCheck: this.random() < this.config.spotCheckRate,
      });
    } else {
      this.store.setEmailStatus(emailId, "drafted");
    }
    return { draftId, category, autoSent, blocker, violations };
  }

  review(emailId: string, a: ReviewAction): { outcome: string; autonomy: unknown } {
    const email = this.store.getEmail(emailId);
    const draft = this.store.latestDraft(emailId);
    if (!email || !draft) throw new ReviewError(`no draft to review for ${emailId}`);
    const c = draft.classification as Classification;
    const category = c.category;
    const hadBlock = draft.violations.some((v) => v.severity === "block");
    const settings = this.settings();
    const eligible = canEverAutoSend(category, c.urgency, settings.autoSend[category]);
    let state = this.store.autonomy(category);

    if (a.action === "spot_check") {
      const pending = this.store.pendingSpotChecks().some((r) => r.email_id === emailId);
      if (!pending) throw new ReviewError(`${emailId} has no pending spot check`);
      this.store.recordReview({
        emailId, draftId: draft.id, outcome: a.ok ? "sent_unchanged" : "edited", category,
        reason: a.reason ?? (a.ok ? "spot check passed" : "spot check failed"), reviewer: a.reviewer,
      });
      if (!a.ok) {
        const r = revoke(state, `spot check failed on ${emailId}: ${a.reason ?? "no reason given"}`);
        this.store.saveAutonomy(r.state, r.events, emailId);
        state = r.state;
      }
      return { outcome: a.ok ? "spot_check_ok" : "spot_check_failed", autonomy: state };
    }

    if (email.status !== "drafted") throw new ReviewError(`${emailId} is ${email.status}, not awaiting review`);

    let outcome: ReviewOutcome;
    switch (a.action) {
      case "send": {
        const finalBody = a.body ?? draft.body;
        const finalSubject = a.subject ?? draft.subject;
        outcome = isUnchanged(draft.body, finalBody) && isUnchanged(draft.subject, finalSubject) ? "sent_unchanged" : "edited";
        this.store.send(emailId, email.from_email, finalSubject, finalBody, "human");
        this.store.setEmailStatus(emailId, "sent");
        this.store.recordReview({ emailId, draftId: draft.id, outcome, category, finalSubject, finalBody, reason: a.reason ?? null, reviewer: a.reviewer });
        break;
      }
      case "reject": {
        outcome = "rejected";
        const finalSubject = a.subject ?? draft.subject;
        this.store.send(emailId, email.from_email, finalSubject, a.body, "human");
        this.store.setEmailStatus(emailId, "sent");
        this.store.recordReview({ emailId, draftId: draft.id, outcome, category, finalSubject, finalBody: a.body, reason: a.reason, reviewer: a.reviewer });
        break;
      }
      case "escalate": {
        outcome = "escalated";
        this.store.setEmailStatus(emailId, "escalated");
        this.store.recordReview({ emailId, draftId: draft.id, outcome, category, reason: a.reason, reviewer: a.reviewer });
        break;
      }
      case "archive": {
        // Archiving is "accepting the draft" only when the agent suggested it.
        outcome = category === "not_actionable" ? "sent_unchanged" : "rejected";
        this.store.setEmailStatus(emailId, "archived");
        this.store.recordReview({ emailId, draftId: draft.id, outcome, category, reason: a.reason ?? "archived", reviewer: a.reviewer });
        break;
      }
    }
    const next = applyReview(state, outcome, { eligible, hadBlockingViolation: hadBlock, threshold: settings.autonomyThreshold });
    this.store.saveAutonomy(next.state, next.events, emailId);
    return { outcome, autonomy: next.state };
  }

  /** This inbox's X and auto-send switches (settings.ts); the env value is the default X. */
  settings(): InboxSettings {
    return readSettings(this.store, this.config.autonomyThreshold);
  }

  revokeAutonomy(kind: CategoryId, reason: string): void {
    const r = revoke(this.store.autonomy(kind), `manual: ${reason}`);
    this.store.saveAutonomy(r.state, r.events, null);
  }
}
