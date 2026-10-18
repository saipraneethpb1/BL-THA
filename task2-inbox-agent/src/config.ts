/** All runtime settings come from the environment. Nothing secret lives in code. */
export interface Config {
  readonly kbUrl: string;
  readonly kbApiKey: string | undefined;
  readonly llmEnabled: boolean;
  readonly classifyModel: string;
  readonly draftModel: string;
  readonly lessonModel: string;
  readonly dbPath: string;
  readonly port: number;
  /** Unedited approvals in a row before a kind of email may auto-send. */
  readonly autonomyThreshold: number;
  /** Share of auto-sent emails a human still checks afterwards (0..1). */
  readonly spotCheckRate: number;
  /** Public demo: cap on recorded drafting spend in USD (null = no cap). */
  readonly budgetUsd: number | null;
  /** Public demo: drafts per visitor per 10 minutes. */
  readonly draftLimitPer10Min: number;
  /** Public demo: lesson checks per day for the whole site. */
  readonly lessonChecksPerDay: number;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  return {
    kbUrl: env.KB_URL ?? "http://localhost:8001",
    kbApiKey: env.KB_API_KEY,
    llmEnabled: Boolean(env.ANTHROPIC_API_KEY),
    classifyModel: env.INBOX_CLASSIFY_MODEL ?? "claude-haiku-5-5",
    draftModel: env.INBOX_DRAFT_MODEL ?? "claude-sonnet-5-5",
    lessonModel: env.INBOX_LESSON_MODEL ?? "claude-sonnet-5-5",
    dbPath: env.INBOX_DB_PATH ?? "data/inbox.db",
    port: Number(env.INBOX_PORT ?? 8002),
    autonomyThreshold: Number(env.INBOX_AUTONOMY_THRESHOLD ?? 20),
    spotCheckRate: Number(env.INBOX_SPOT_CHECK_RATE ?? 0.2),
    budgetUsd: env.INBOX_BUDGET_USD ? Number(env.INBOX_BUDGET_USD) : null,
    draftLimitPer10Min: Number(env.INBOX_DRAFT_LIMIT_PER_10MIN ?? 15),
    lessonChecksPerDay: Number(env.INBOX_LESSON_CHECKS_PER_DAY ?? 3),
  };
}

/** USD per million tokens (input, output), for cost reporting. */
export const PRICES: Readonly<Record<string, readonly [number, number]>> = {
  "claude-haiku-5-5": [0.1, 0.5],
  "claude-sonnet-5-5": [2, 10],
  "claude-opus-5-5": [4, 20],
};

export function costUsd(model: string, inputTokens: number, outputTokens: number): number {
  const [pin, pout] = PRICES[model] ?? [0, 0];
  return (inputTokens * pin + outputTokens * pout) / 1_000_000;
}
