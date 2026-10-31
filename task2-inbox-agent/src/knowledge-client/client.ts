/**
 * Typed client for the Knowledge Brain (Task 1) HTTP API. The Inbox Agent never touches
 * Task 1's code or database: this contract is the only coupling.
 */
import { z } from "zod";

const Source = z.object({
  title: z.string(),
  url: z.string().nullable(),
  source_type: z.enum(["article", "expert"]),
  section: z.string().nullable(),
  score: z.number(),
  image_urls: z.array(z.string()),
});

export const KnowledgeAnswer = z.object({
  status: z.enum(["answered", "partial", "insufficient_evidence", "conflict"]),
  answer: z.string(),
  sources: z.array(Source),
  needs_human: z.boolean(),
  escalation_id: z.number().nullable(),
  missing_info: z.string().nullable(),
  cost_usd: z.number(),
  latency_ms: z.number(),
});
export type KnowledgeAnswer = z.infer<typeof KnowledgeAnswer>;

export class KnowledgeUnavailableError extends Error {
  override readonly name = "KnowledgeUnavailableError";
}

export interface KnowledgeClient {
  query(question: string): Promise<KnowledgeAnswer>;
}

export class HttpKnowledgeClient implements KnowledgeClient {
  constructor(
    private readonly baseUrl: string,
    private readonly apiKey: string | undefined,
    private readonly timeoutMs = 30_000,
    private readonly retries = 2,
    private readonly fetchImpl: typeof fetch = fetch,
  ) {}

  async query(question: string): Promise<KnowledgeAnswer> {
    const body = JSON.stringify({ question, caller: "inbox-agent" });
    let lastError = "unknown error";
    for (let attempt = 0; attempt <= this.retries; attempt++) {
      try {
        const res = await this.fetchImpl(`${this.baseUrl}/query`, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            ...(this.apiKey ? { "X-API-Key": this.apiKey } : {}),
          },
          body,
          signal: AbortSignal.timeout(this.timeoutMs),
        });
        if (res.ok) return KnowledgeAnswer.parse(await res.json());
        lastError = `HTTP ${res.status}`;
        // 4xx (bad key, bad input, answering disabled) will not get better by retrying.
        if (res.status < 500 && res.status !== 429) break;
      } catch (err) {
        if (err instanceof z.ZodError) {
          throw new KnowledgeUnavailableError(`unexpected response shape: ${err.message}`);
        }
        lastError = err instanceof Error ? err.message : String(err);
      }
      if (attempt < this.retries) await sleep(500 * 2 ** attempt);
    }
    throw new KnowledgeUnavailableError(`Knowledge Brain unavailable: ${lastError}`);
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
