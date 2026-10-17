/**
 * Inbox Agent eval. Dry run by default; add --run to spend API credits.
 *
 *   npm run eval -- --split dev                                 # plan only
 *   npm run eval -- --split test --run                          # held-out numbers for EVALS.md
 *   npm run eval -- --classify claude-haiku-5-5,claude-sonnet-5-5 --draft none --run
 *                                                               # classifier comparison only
 *
 * Each (classifier, drafter) pair runs every email through the real pipeline (with the
 * Knowledge Brain over HTTP) in a fresh in-memory inbox. A separate, stronger model
 * judges each draft against the human-written checklist in the dataset.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { parseArgs } from "node:util";
import Anthropic from "@anthropic-ai/sdk";
import { zodOutputFormat } from "@anthropic-ai/sdk/helpers/zod";
import { z } from "zod";
import { costUsd, loadConfig } from "../config.js";
import { loadAndCheck } from "../dataset/check.js";
import type { EmailCase } from "../dataset/schema.js";
import { findSecrets } from "../email/pii.js";
import { HttpKnowledgeClient } from "../knowledge-client/client.js";
import { ClaudeClassifier, ClaudeDrafter, loadPrompt, type Classification, type EvidenceItem } from "../llm/models.js";
import { InboxAgent } from "../pipeline.js";
import { InboxStore } from "../store/db.js";
import { gradeCase, summarise, type AgentOutput, type CaseGrade, type Judgement, type Summary } from "./grade.js";

const ROOT = join(import.meta.dirname, "../..");

const JudgeOutput = z.object({
  must_include: z.array(z.object({ item: z.string(), met: z.boolean(), reason: z.string() })),
  must_not_include: z.array(z.object({ item: z.string(), violated: z.boolean(), reason: z.string() })),
  tone_ok: z.boolean(),
});

class Judge {
  private readonly client = new Anthropic({ maxRetries: 3, timeout: 120_000 });
  private readonly system = loadPrompt("judge_v1", {});
  cost = 0;
  constructor(readonly model: string) {}

  async grade(e: EmailCase, draft: string): Promise<Judgement> {
    const user = JSON.stringify({
      customer_email: { subject: e.subject, body: e.body },
      draft_reply: draft,
      must_include: e.labels.mustInclude,
      must_not_include: e.labels.mustNotInclude,
    }, null, 1);
    const r = await this.client.messages.parse({
      model: this.model,
      max_tokens: 3000,
      system: [{ type: "text", text: this.system, cache_control: { type: "ephemeral" } }],
      messages: [{ role: "user", content: user }],
      output_config: { effort: "medium", format: zodOutputFormat(JudgeOutput) },
    });
    this.cost += costUsd(this.model, r.usage.input_tokens, r.usage.output_tokens);
    if (!r.parsed_output) throw new Error(`judge returned nothing (${r.stop_reason})`);
    return r.parsed_output;
  }
}

/** Classification-only mode: the drafter is never called. */
class NoDrafter {
  async draft(): Promise<never> {
    throw new Error("drafting disabled in classify-only mode");
  }
}

async function runPair(
  emails: EmailCase[],
  classifyModel: string,
  draftModel: string | null,
  judge: Judge | null,
): Promise<{ summary: Summary; grades: CaseGrade[]; outputs: (AgentOutput & { id: string })[] }> {
  const config = loadConfig();
  const store = new InboxStore(":memory:");
  const classifier = new ClaudeClassifier(classifyModel);
  const kb = new HttpKnowledgeClient(config.kbUrl, config.kbApiKey);
  const grades: CaseGrade[] = [];
  const outputs: (AgentOutput & { id: string })[] = [];

  for (const e of emails) {
    store.addEmail({
      id: e.id, from_name: e.from.name, from_email: e.from.email, subject: e.subject,
      body: e.body, thread: e.thread, received_at: e.receivedAt,
    });
    const started = performance.now();
    let out: AgentOutput;
    if (draftModel) {
      const agent = new InboxAgent(store, classifier, new ClaudeDrafter(draftModel), kb, config);
      await agent.process(e.id);
      const d = store.latestDraft(e.id)!;
      out = {
        classification: d.classification as Classification, body: d.body, violations: d.violations,
        secretsFound: d.secrets_found, evidence: d.evidence as EvidenceItem[],
        costUsd: d.cost_usd, latencyMs: performance.now() - started,
      };
    } else {
      const c = await classifier.classify({ fromName: e.from.name, subject: e.subject, body: e.body, thread: e.thread });
      out = {
        classification: c.result, body: "", violations: [],
        secretsFound: findSecrets(e.body).length > 0, evidence: [],
        costUsd: c.usage.costUsd, latencyMs: performance.now() - started,
      };
    }
    const judgement = judge && draftModel && out.body ? await judge.grade(e, out.body) : null;
    const g = gradeCase(e, out, judgement);
    grades.push(g);
    outputs.push({ ...out, id: e.id });
    const flag = g.unsafe ? " UNSAFE" : "";
    console.log(`  ${e.id} ${out.classification.category}${g.category_ok ? "" : ` (want ${e.labels.category})`}` +
      `${judgement ? ` include ${g.must_include_met}/${g.must_include_total} mustNot ${g.must_not_violated}` : ""}${flag}`);
  }
  return { summary: summarise(classifyModel, draftModel, grades, outputs), grades, outputs };
}

function pct(x: number | null): string {
  return x === null ? "-" : `${Math.round(x * 100)}%`;
}

async function main(): Promise<void> {
  const { values } = parseArgs({
    options: {
      split: { type: "string", default: "dev" },
      classify: { type: "string", default: "claude-haiku-5-5" },
      draft: { type: "string", default: "claude-sonnet-5-5,claude-opus-5-5" },
      judge: { type: "string", default: "claude-opus-5-5" },
      limit: { type: "string" },
      run: { type: "boolean", default: false },
    },
  });
  const { emails: all } = loadAndCheck(join(ROOT, "data/emails"));
  let emails = all.filter((e) => e.split === values.split);
  if (values.limit) emails = emails.slice(0, Number(values.limit));
  const classifyModels = values.classify.split(",").filter(Boolean);
  const draftModels = values.draft === "none" ? [null] : values.draft.split(",").filter(Boolean);
  const pairs = classifyModels.flatMap((c) => draftModels.map((d) => [c, d] as const));
  const drafting = draftModels[0] !== null;
  console.log(
    `${emails.length} ${values.split} emails x ${pairs.length} model pair(s): ` +
      `${emails.length * pairs.length} pipeline runs${drafting ? ` + ${emails.length * pairs.length} judge calls (${values.judge})` : ""}.`,
  );
  if (emails.length === 0) {
    console.log(`No ${values.split} emails yet.`);
    return;
  }
  if (!values.run) {
    console.log("Dry run. Add --run to execute (needs ANTHROPIC_API_KEY and, for drafting, the Knowledge Brain).");
    return;
  }
  if (drafting) {
    const config = loadConfig();
    const ok = await fetch(`${config.kbUrl}/health`).then((r) => r.ok).catch(() => false);
    if (!ok) throw new Error(`Knowledge Brain not reachable at ${config.kbUrl}; start it first`);
  }

  const judge = drafting ? new Judge(values.judge) : null;
  const results = [];
  for (const [c, d] of pairs) {
    console.log(`\n== classify ${c} / draft ${d ?? "none"}`);
    results.push(await runPair(emails, c, d, judge));
  }

  console.log("\n| classifier | drafter | category | urgency | unsafe auto-sends | over-cautious | must-include met | must-not violations | tone ok | $/email | p50 s | p95 s |");
  console.log("|---|---|---|---|---|---|---|---|---|---|---|---|");
  for (const { summary: s } of results) {
    console.log(`| ${s.classify_model} | ${s.draft_model ?? "-"} | ${pct(s.category_accuracy)} | ${pct(s.urgency_accuracy)} | ${s.unsafe_auto_sends} | ${s.over_cautious} | ${pct(s.must_include_rate)} | ${s.must_not_violations} | ${pct(s.tone_ok_rate)} | ${s.cost_per_email_usd.toFixed(4)} | ${(s.p50_ms / 1000).toFixed(1)} | ${(s.p95_ms / 1000).toFixed(1)} |`);
  }
  if (judge) console.log(`\nJudge cost: $${judge.cost.toFixed(3)} (not included in $/email).`);

  const dir = join(ROOT, "evaluation/results");
  mkdirSync(dir, { recursive: true });
  const file = join(dir, `inbox_${values.split}_${new Date().toISOString().replace(/[:.]/g, "")}.json`);
  writeFileSync(file, JSON.stringify({ split: values.split, judge: values.judge, results }, null, 2));
  console.log(`saved ${file}`);
}

await main();
