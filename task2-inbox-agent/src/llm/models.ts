/**
 * The two model calls the agent makes: classify, then draft. Each is behind an interface
 * so tests (and runs without an API key) can use fakes, and so evals can swap models.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import Anthropic from "@anthropic-ai/sdk";
import { zodOutputFormat } from "@anthropic-ai/sdk/helpers/zod";
import { claudeClient, effortFor, modelId } from "./claude.js";
import { z } from "zod";
import { COMPANY } from "../company.js";
import { costUsd } from "../config.js";
import { CATEGORIES, CATEGORY_IDS, URGENCY_LEVELS, type CategoryId } from "../email/taxonomy.js";
import { DEFAULT_TOPICS, OTHER, topicsForPrompt, type Topic } from "../email/topics.js";

const PROMPTS = join(import.meta.dirname, "../../prompts");

export function loadPrompt(name: string, vars: Record<string, string>): string {
  const template = readFileSync(join(PROMPTS, `${name}.md`), "utf8");
  return template.replace(/\{\{(\w+)\}\}/g, (_, key: string) => vars[key] ?? "");
}

export const Classification = z.object({
  category: z.enum(CATEGORY_IDS),
  secondary: z.array(z.enum(CATEGORY_IDS)),
  urgency: z.enum(URGENCY_LEVELS),
  language: z.string(),
  summary: z.string(),
  questions: z.array(z.string()).max(3),
  sentiment: z.enum(["positive", "neutral", "frustrated", "angry"]),
  suspected_injection: z.boolean(),
  /** Which part of the site it's about: an id from the inbox's own category list (topics.ts). */
  topic: z.string().default(OTHER),
  /** Unsolicited sales, phishing, scams: goes to the Spam folder, no reply drafted. */
  spam: z.boolean().default(false),
});
export type Classification = z.infer<typeof Classification>;

/**
 * What the model may return. Optional fields are lenient on purpose: in the eval, a
 * stray secondary category made the whole email fail. The primary category stays
 * strict: a wrong one should fail loudly, not be guessed.
 */
const RawClassification = Classification.extend({
  secondary: z.array(z.string()),
  questions: z.array(z.string()),
  topic: z.string(),
  spam: z.boolean(),
});

export function normalizeClassification(
  raw: z.infer<typeof RawClassification>,
  topics: readonly Topic[] = DEFAULT_TOPICS,
): Classification {
  const valid = new Set<string>(CATEGORY_IDS);
  const secondary = [...new Set(raw.secondary)].filter(
    (c): c is Classification["category"] => valid.has(c) && c !== raw.category,
  );
  // A topic the list doesn't have (renamed meanwhile, or made up) becomes "other".
  const topic = topics.some((x) => x.id === raw.topic) ? raw.topic : OTHER;
  return { ...raw, secondary, topic, questions: raw.questions.filter((q) => q.trim()).slice(0, 3) };
}

export const DraftOutput = z.object({
  subject: z.string(),
  body: z.string(),
  used_evidence: z.array(z.number().int()),
  follow_up_in_days: z.number().int().nullable(),
  note_for_reviewer: z.string(),
});
export type DraftOutput = z.infer<typeof DraftOutput>;

export interface Usage {
  model: string;
  promptVersion: string;
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
}

export interface EmailForModel {
  fromName: string;
  subject: string;
  body: string; // already redacted
  thread: { from: string; date: string; body: string }[];
}

export interface EvidenceItem {
  missing_info?: string | null;
  number: number;
  question: string;
  status: string;
  answer: string;
  sources: { title: string; url: string | null }[];
}

export interface DraftRequest {
  email: EmailForModel;
  classification: Classification;
  evidence: EvidenceItem[];
  /** Learned guidance for this category, from human edits (see feedback/lessons). */
  lessons: string[];
}

export interface Classifier {
  /** `topics`: the inbox's current category list (Settings); defaults when omitted. */
  classify(email: EmailForModel, topics?: readonly Topic[]): Promise<{ result: Classification; usage: Usage }>;
}

export interface Drafter {
  draft(req: DraftRequest): Promise<{ result: DraftOutput; usage: Usage }>;
}

export function renderEmail(email: EmailForModel): string {
  const thread = email.thread
    .map((t) => `<earlier_message from="${t.from}" date="${t.date}">\n${t.body}\n</earlier_message>`)
    .join("\n");
  return [
    thread ? `<thread>\n${thread}\n</thread>` : "",
    `<email from="${email.fromName}" subject="${email.subject}">\n${email.body}\n</email>`,
  ]
    .filter(Boolean)
    .join("\n\n");
}

export function renderEvidence(evidence: EvidenceItem[]): string {
  if (evidence.length === 0) return "<evidence>none</evidence>";
  const items = evidence.map((e) => {
    const sources = e.sources.map((s) => `${s.title} (${s.url ?? "expert answer"})`).join("; ");
    return `<evidence number="${e.number}" question="${e.question}" status="${e.status}">\n<answer>${e.answer}</answer>\n<missing_info>${e.missing_info ?? ""}</missing_info>\nSources: ${sources || "none"}\n</evidence>`;
  });
  return items.join("\n");
}

function usageOf(model: string, version: string, u: Anthropic.Usage): Usage {
  const input =
    u.input_tokens + (u.cache_read_input_tokens ?? 0) + (u.cache_creation_input_tokens ?? 0);
  return {
    model,
    promptVersion: version,
    inputTokens: input,
    outputTokens: u.output_tokens,
    costUsd: costUsd(model, input, u.output_tokens),
  };
}

export class ClaudeClassifier implements Classifier {
  private readonly client = claudeClient({ maxRetries: 3, timeout: 60_000 });
  private readonly system: string;

  constructor(
    private readonly model: string,
    private readonly version = "classify_v2",
  ) {
    const categories = CATEGORY_IDS.map((id) => `- \`${id}\`: ${CATEGORIES[id].definition}`).join("\n");
    this.system = loadPrompt(version, { company_what: COMPANY.what, categories });
  }

  async classify(email: EmailForModel, topics: readonly Topic[] = DEFAULT_TOPICS) {
    const response = await this.client.messages.parse({
      model: modelId(this.model),
      max_tokens: 2000,
      system: [
        { type: "text", text: this.system, cache_control: { type: "ephemeral" } },
        // The team's own topic list changes rarely, so it caches well as its own block.
        { type: "text", text: `Topics (id, parent setting type › name: what belongs there):\n${topicsForPrompt(topics)}`, cache_control: { type: "ephemeral" } },
      ],
      messages: [{ role: "user", content: renderEmail(email) }],
      output_config: { ...effortFor(this.model, "low"), format: zodOutputFormat(RawClassification) },
    });
    if (!response.parsed_output) {
      throw new Error(`classifier returned no result (stop_reason=${response.stop_reason})`);
    }
    return {
      result: normalizeClassification(response.parsed_output, topics),
      usage: usageOf(this.model, this.version, response.usage),
    };
  }
}

export function lessonsBlock(lessons: string[]): string {
  if (lessons.length === 0) return "";
  return `Lessons from the team's past edits to replies like this (follow them):\n${lessons.map((l) => `- ${l}`).join("\n")}`;
}

export class ClaudeDrafter implements Drafter {
  private readonly client = claudeClient({ maxRetries: 3, timeout: 120_000 });

  constructor(
    private readonly model: string,
    private readonly version = "draft_v1",
  ) {}

  systemFor(category: CategoryId, lessons: string[]): string {
    const rule = CATEGORIES[category];
    return loadPrompt(this.version, {
      company_name: COMPANY.name,
      company_what: COMPANY.what,
      voice: COMPANY.voice.map((v) => `- ${v}`).join("\n"),
      category_label: rule.label,
      must_not: rule.mustNot.map((m) => `- ${m}`).join("\n"),
      next_action: rule.nextAction,
      lessons: lessonsBlock(lessons),
    });
  }

  async draft(req: DraftRequest) {
    const user = [
      renderEmail(req.email),
      renderEvidence(req.evidence),
      `<triage category="${req.classification.category}" urgency="${req.classification.urgency}" language="${req.classification.language}">${req.classification.summary}</triage>`,
      `Sign as: ${COMPANY.signature}`,
    ].join("\n\n");
    const response = await this.client.messages.parse({
      model: modelId(this.model),
      max_tokens: 4000,
      system: [
        {
          type: "text",
          text: this.systemFor(req.classification.category, req.lessons),
          cache_control: { type: "ephemeral" },
        },
      ],
      messages: [{ role: "user", content: user }],
      output_config: { ...effortFor(this.model, "medium"), format: zodOutputFormat(DraftOutput) },
    });
    if (!response.parsed_output) {
      throw new Error(`drafter returned no result (stop_reason=${response.stop_reason})`);
    }
    return { result: response.parsed_output, usage: usageOf(this.model, this.version, response.usage) };
  }
}
