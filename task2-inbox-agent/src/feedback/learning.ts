/**
 * Learning from human edits, in four auditable steps:
 *
 *  1. propose:  a model reads recent corrections and proposes a new lesson set (candidate)
 *  2. evaluate: re-draft past emails with the current lessons and with the candidate,
 *               and score both against what the human actually sent
 *  3. promote:  only if it measurably helps on emails the miner did not see (or a person
 *               overrides with a written reason); the category's autonomy is reset
 *  4. rollback: re-activate the previous set in one step; autonomy is reset again
 *
 * Nothing changes the prompt silently: every step is a row in lesson_events.
 */
import Anthropic from "@anthropic-ai/sdk";
import { zodOutputFormat } from "@anthropic-ai/sdk/helpers/zod";
import { z } from "zod";
import { COMPANY } from "../company.js";
import { checkDraft } from "../drafting/policy.js";
import { findSecrets, redact } from "../email/pii.js";
import { CATEGORIES, type CategoryId } from "../email/taxonomy.js";
import { loadPrompt, type Classification, type Drafter, type EvidenceItem } from "../llm/models.js";
import type { InboxStore, Lesson, ReviewedCase } from "../store/db.js";
import { revoke } from "./autonomy.js";
import { mean, similarity } from "./similarity.js";

export class LearningError extends Error {
  override readonly name = "LearningError";
}

/** Raised when a step needs a model but no API key is configured. */
export class LearningUnavailableError extends LearningError {}

export const MinedLessons = z.object({
  lessons: z.array(z.object({ text: z.string(), review_ids: z.array(z.number().int()) })),
});
export type MinedLessons = z.infer<typeof MinedLessons>;

export interface Correction {
  review_id: number;
  outcome: string;
  reason: string | null;
  draft: string;
  sent: string;
}

export interface LessonMiner {
  mine(category: CategoryId, current: Lesson[], corrections: Correction[]): Promise<MinedLessons>;
}

export class ClaudeLessonMiner implements LessonMiner {
  private readonly client = new Anthropic({ maxRetries: 3, timeout: 120_000 });
  constructor(private readonly model: string) {}

  async mine(category: CategoryId, current: Lesson[], corrections: Correction[]): Promise<MinedLessons> {
    const system = loadPrompt("mine_lessons_v1", {
      company_name: COMPANY.name,
      company_what: COMPANY.what,
      category_label: CATEGORIES[category].label,
    });
    const user = JSON.stringify({ current_lessons: current.map((l) => l.text), corrections }, null, 1);
    const r = await this.client.messages.parse({
      model: this.model,
      max_tokens: 4000,
      system,
      messages: [{ role: "user", content: user }],
      output_config: { effort: "medium", format: zodOutputFormat(MinedLessons) },
    });
    if (!r.parsed_output) throw new LearningError(`lesson miner returned nothing (${r.stop_reason})`);
    return r.parsed_output;
  }
}

export interface CaseScore {
  review_id: number;
  email_id: string;
  seen_by_miner: boolean;
  before: number;
  after: number;
  violations_before: number;
  violations_after: number;
}

export interface Evaluation {
  baseline_version: number;
  cases: CaseScore[];
  n: number;
  unseen_n: number;
  mean_before: number;
  mean_after: number;
  unseen_before: number;
  unseen_after: number;
  violations_before: number;
  violations_after: number;
  recommended: boolean;
  verdict: string;
}

export interface LearningOptions {
  minCorrections: number;
  evalLimit: number;
  minUnseen: number;
  minGain: number;
}

const DEFAULTS: LearningOptions = { minCorrections: 3, evalLimit: 12, minUnseen: 2, minGain: 0.02 };
const r3 = (x: number) => Math.round(x * 1000) / 1000;

export class LearningService {
  private readonly opts: LearningOptions;

  constructor(
    private readonly store: InboxStore,
    private readonly miner: LessonMiner | null,
    private readonly drafter: Drafter | null,
    opts: Partial<LearningOptions> = {},
  ) {
    this.opts = { ...DEFAULTS, ...opts };
  }

  /** Corrections made while the current lesson set was in use: what it still gets wrong. */
  corrections(category: CategoryId): ReviewedCase[] {
    const active = this.store.activeLessons(category).version;
    return this.store
      .reviewedCases(category)
      .filter((c) => c.lessonsVersion === active && (c.outcome === "edited" || c.outcome === "rejected"));
  }

  async propose(category: CategoryId, actor: string): Promise<number> {
    if (this.store.lessonVersions(category).some((v) => v.status === "candidate")) {
      throw new LearningError("a candidate is already waiting: evaluate, promote or reject it first");
    }
    const cases = this.corrections(category).slice(-15);
    if (cases.length < this.opts.minCorrections) {
      throw new LearningError(
        `need at least ${this.opts.minCorrections} corrections in ${category} since the current lessons; have ${cases.length}`,
      );
    }
    if (!this.miner) throw new LearningUnavailableError("proposing lessons needs ANTHROPIC_API_KEY");
    const current = this.store.activeLessons(category).lessons;
    const mined = await this.miner.mine(
      category,
      current,
      cases.map((c) => ({ review_id: c.reviewId, outcome: c.outcome, reason: c.reason, draft: c.draftBody, sent: c.finalBody })),
    );
    // Keep only support the miner could have seen; cap the set size.
    const valid = new Set(cases.map((c) => c.reviewId));
    const currentTexts = new Set(current.map((l) => l.text));
    const lessons = mined.lessons
      .map((l) => ({ text: l.text.trim(), review_ids: l.review_ids.filter((id) => valid.has(id)) }))
      .filter((l) => l.text.length > 0 && (l.review_ids.length > 0 || currentTexts.has(l.text)))
      .slice(0, 8);
    if (lessons.length === 0) throw new LearningError("the miner found no lesson supported by the corrections");
    return this.store.addLessonVersion(category, lessons, [...valid], actor);
  }

  async evaluate(category: CategoryId, version: number, actor: string): Promise<Evaluation> {
    const candidate = this.store.lessonVersion(category, version);
    if (!candidate || candidate.status !== "candidate") throw new LearningError(`v${version} is not a candidate`);
    const drafter = this.drafter;
    if (!drafter) throw new LearningUnavailableError("the regression check needs ANTHROPIC_API_KEY");
    const baseline = this.store.activeLessons(category);
    const seen = new Set(candidate.mined_from);
    const cases = this.store.reviewedCases(category).slice(-this.opts.evalLimit);
    if (cases.length === 0) throw new LearningError("no reviewed emails to check against");

    const scores: CaseScore[] = [];
    for (const c of cases) {
      const email = this.store.getEmail(c.emailId);
      if (!email) continue;
      const secrets = findSecrets([email.body, ...email.thread.map((t) => t.body)].join("\n"));
      const classification = c.classification as Classification;
      const evidence = c.evidence as EvidenceItem[];
      const req = {
        email: {
          fromName: email.from_name,
          subject: email.subject,
          body: redact(email.body, secrets),
          thread: email.thread.map((t) => ({ ...t, body: redact(t.body, secrets) })),
        },
        classification,
        evidence,
      };
      const [before, after] = await Promise.all([
        drafter.draft({ ...req, lessons: baseline.lessons.map((l) => l.text) }),
        drafter.draft({ ...req, lessons: candidate.lessons.map((l) => l.text) }),
      ]);
      const blocking = (body: string, used: number[]) =>
        checkDraft(body, {
          category,
          secondary: classification.secondary,
          secrets,
          needsKnowledge: CATEGORIES[category].needsKnowledge,
          evidenceCount: evidence.length,
          usedEvidence: used,
          knowledgeUnavailable: false,
        }).filter((v) => v.severity === "block").length;
      scores.push({
        review_id: c.reviewId,
        email_id: c.emailId,
        seen_by_miner: seen.has(c.reviewId),
        before: r3(similarity(before.result.body, c.finalBody)),
        after: r3(similarity(after.result.body, c.finalBody)),
        violations_before: blocking(before.result.body, before.result.used_evidence),
        violations_after: blocking(after.result.body, after.result.used_evidence),
      });
    }

    const unseen = scores.filter((s) => !s.seen_by_miner);
    const evaluation: Evaluation = {
      baseline_version: baseline.version,
      cases: scores,
      n: scores.length,
      unseen_n: unseen.length,
      mean_before: r3(mean(scores.map((s) => s.before))),
      mean_after: r3(mean(scores.map((s) => s.after))),
      unseen_before: r3(mean(unseen.map((s) => s.before))),
      unseen_after: r3(mean(unseen.map((s) => s.after))),
      violations_before: scores.reduce((s, x) => s + x.violations_before, 0),
      violations_after: scores.reduce((s, x) => s + x.violations_after, 0),
      recommended: false,
      verdict: "",
    };
    const gain = evaluation.unseen_after - evaluation.unseen_before;
    if (evaluation.violations_after > evaluation.violations_before) {
      evaluation.verdict = "Do not promote: the new lessons cause more policy violations.";
    } else if (unseen.length < this.opts.minUnseen) {
      evaluation.verdict = `Not enough unseen emails to judge (${unseen.length}/${this.opts.minUnseen}); promote only with a reason.`;
    } else if (gain >= this.opts.minGain) {
      evaluation.recommended = true;
      evaluation.verdict = `Promote: drafts are ${r3(gain * 100)} points closer to what reviewers sent, on emails the miner did not see.`;
    } else {
      evaluation.verdict = `Do not promote: no clear gain on unseen emails (${r3(gain * 100)} points).`;
    }
    this.store.saveLessonEvaluation(category, version, evaluation, actor, evaluation.verdict);
    return evaluation;
  }

  promote(category: CategoryId, version: number, actor: string, note: string | null): void {
    const v = this.store.lessonVersion(category, version);
    if (!v || v.status !== "candidate") throw new LearningError(`v${version} is not a candidate`);
    const evaluation = v.evaluation as Evaluation | null;
    if (!evaluation) throw new LearningError("run the regression check before promoting");
    if (!evaluation.recommended && !note) {
      throw new LearningError(`the check did not recommend this version (${evaluation.verdict}); promoting needs a reason`);
    }
    this.store.activateLessonVersion(category, version, actor, "promoted", note);
    this.resetAutonomy(category, `lessons changed to v${version}`);
  }

  reject(category: CategoryId, version: number, actor: string, note: string): void {
    const v = this.store.lessonVersion(category, version);
    if (!v || v.status !== "candidate") throw new LearningError(`v${version} is not a candidate`);
    this.store.rejectLessonVersion(category, version, actor, note);
  }

  /** Back to the set that was active before the current one (or to no lessons). */
  rollback(category: CategoryId, actor: string, reason: string): number {
    const current = this.store.activeLessons(category).version;
    if (current === 0) throw new LearningError("no lessons are active; nothing to roll back");
    const previous = this.store
      .lessonVersions(category)
      .find((v) => v.version < current && v.status === "retired" && v.decided_by !== null);
    if (previous) this.store.activateLessonVersion(category, previous.version, actor, "rolled_back", reason);
    else this.store.deactivateLessons(category, actor, reason);
    this.resetAutonomy(category, `lessons rolled back to v${previous?.version ?? 0}`);
    return previous?.version ?? 0;
  }

  /** How drafts fared per lesson version: the "is it getting better?" number. */
  metrics(category: CategoryId): { version: number; reviewed: number; unchanged_rate: number; mean_similarity: number }[] {
    const byVersion = new Map<number, ReviewedCase[]>();
    for (const c of this.store.reviewedCases(category)) {
      byVersion.set(c.lessonsVersion, [...(byVersion.get(c.lessonsVersion) ?? []), c]);
    }
    return [...byVersion.entries()]
      .sort(([a], [b]) => a - b)
      .map(([version, cs]) => ({
        version,
        reviewed: cs.length,
        unchanged_rate: r3(cs.filter((c) => c.outcome === "sent_unchanged").length / cs.length),
        mean_similarity: r3(mean(cs.map((c) => similarity(c.draftBody, c.finalBody)))),
      }));
  }

  private resetAutonomy(category: CategoryId, reason: string): void {
    // New instructions mean new behaviour: trust is earned again from zero, in every email
    // category (topic) that this request type's emails belong to.
    const topics = new Set(
      this.store.reviewedCases(category).map((c) => (c.classification as { topic?: string }).topic ?? "other"),
    );
    for (const topic of topics) {
      const r = revoke(this.store.autonomy(topic), reason);
      this.store.saveAutonomy(r.state, r.events, null);
    }
  }

  /**
   * The learning loop, run by itself after a correction (no button needed): once a request
   * type has `minCorrections` corrections since its current lessons, lessons are proposed,
   * checked on past emails, and made live only if the check recommends them. Anything else
   * stays a candidate for a person to decide. Returns what happened, or null if not due.
   * `beforeSpend` throws when the budget or the daily limit says stop.
   */
  async autoLearn(category: CategoryId, beforeSpend: () => void = () => undefined): Promise<string | null> {
    if (!this.miner || !this.drafter) return null;
    if (this.store.lessonVersions(category).some((v) => v.status === "candidate")) return null;
    if (this.corrections(category).length < this.opts.minCorrections) return null;
    beforeSpend();
    const version = await this.propose(category, "learning loop");
    beforeSpend();
    const evaluation = await this.evaluate(category, version, "learning loop");
    if (evaluation.recommended) {
      this.promote(category, version, "learning loop", "made live automatically: the check recommended it");
      return `lessons v${version} live: ${evaluation.verdict}`;
    }
    return `lessons v${version} proposed and checked, waiting for a person: ${evaluation.verdict}`;
  }
}
