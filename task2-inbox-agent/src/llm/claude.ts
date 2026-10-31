/**
 * Where Claude is reached. Every Claude call (classify, draft, lessons, the eval judge) gets
 * its client, model id and effort setting from here; the Knowledge Brain and Scope Builder
 * do the same (kb/claude.py, scope/claude.py).
 *
 * CLAUDE_PROVIDER picks the route:
 * - `bedrock` (default): Claude on Amazon Bedrock (bedrock-runtime, InvokeModel). Region from
 *   AWS_REGION; credentials from a Bedrock bearer token (AWS_BEARER_TOKEN_BEDROCK) or the
 *   standard AWS chain (access keys, AWS_PROFILE/SSO, the EC2 or ECS role).
 * - `anthropic`: Anthropic's own API with ANTHROPIC_API_KEY.
 *
 * Model settings take a bare name (claude-sonnet-4-6) or a full Bedrock id
 * (au.anthropic.claude-sonnet-4-6). On Bedrock a bare name becomes an inference profile with
 * the prefix in CLAUDE_BEDROCK_PROFILE (`global` by default; au, us, eu, jp, apac). Prices
 * use the bare name (baseName).
 *
 * @anthropic-ai/sdk stays on 0.x (and the Bedrock package below 1.0): from 1.0 it sends API
 * version 2026-10-21, which Bedrock rejects for Claude 4.6 and older.
 */
import Anthropic from "@anthropic-ai/sdk";
import AnthropicBedrock from "@anthropic-ai/bedrock-sdk";

export type Provider = "bedrock" | "anthropic";
// Bedrock has no batches or token counting; the agent only needs parse.
export type ClaudeClient = { messages: Pick<Anthropic["messages"], "parse"> };
export type Effort = "low" | "medium" | "high" | "max";

/** Bedrock base ids that differ from "anthropic.<bare name>" (ARN-versioned models). */
const BEDROCK_IDS: Readonly<Record<string, string>> = {
  "claude-opus-4-6": "anthropic.claude-opus-4-6-v1",
  "claude-sonnet-4-6": "anthropic.claude-sonnet-4-6",
  "claude-haiku-4-5": "anthropic.claude-haiku-4-5-20251001-v1:0",
  "claude-opus-4-5": "anthropic.claude-opus-4-5-20251101-v1:0",
  "claude-sonnet-4-5": "anthropic.claude-sonnet-4-5-20250929-v1:0",
};
/** Models that reject output_config.effort (a 400): the request leaves it out. */
const NO_EFFORT = new Set(["claude-haiku-4-5", "claude-sonnet-4-5", "claude-sonnet-4", "claude-opus-4-1"]);

export function provider(env: NodeJS.ProcessEnv = process.env): Provider {
  const value = (env.CLAUDE_PROVIDER ?? "bedrock").trim().toLowerCase();
  if (value !== "bedrock" && value !== "anthropic") {
    throw new Error(`CLAUDE_PROVIDER must be "bedrock" or "anthropic", not "${value}"`);
  }
  return value;
}

/** `au.anthropic.claude-haiku-4-5-20251001-v1:0` -> `claude-haiku-4-5`. */
export function baseName(model: string): string {
  const name = (model.split("/").pop() ?? model)
    .replace(/^(global|us|eu|jp|apac|au|ca|us-gov)\./, "")
    .replace(/^anthropic\./, "");
  return name.replace(/(-\d{8})?(-v\d+(:\d+)?)?$/, "");
}

/** The id to send for a model setting on the current route. */
export function modelId(model: string, env: NodeJS.ProcessEnv = process.env): string {
  if (provider(env) === "anthropic") return baseName(model);
  if (model.includes("anthropic.") || model.startsWith("arn:")) return model;
  const profile = (env.CLAUDE_BEDROCK_PROFILE ?? "global").trim();
  const base = BEDROCK_IDS[model] ?? `anthropic.${model}`;
  return profile ? `${profile}.${base}` : base;
}

/** The effort part of output_config, empty for models that reject it. */
export function effortFor(model: string, effort: Effort): { effort?: Effort } {
  return NO_EFFORT.has(baseName(model)) ? {} : { effort };
}

export function claudeClient(opts: { maxRetries: number; timeout: number }): ClaudeClient {
  if (provider() === "anthropic") return new Anthropic(opts);
  // An empty token line in .env would be sent as an empty bearer token instead of signing
  // with the AWS credentials, so drop it.
  if (process.env.AWS_BEARER_TOKEN_BEDROCK !== undefined && !process.env.AWS_BEARER_TOKEN_BEDROCK.trim()) {
    delete process.env.AWS_BEARER_TOKEN_BEDROCK;
  }
  // Same Messages API surface either way (parse, caching, structured output).
  return new AnthropicBedrock({ ...opts, awsRegion: process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION });
}

/**
 * What is missing for Claude, in words for a 503, or null when it can be called. On Bedrock a
 * region is required; credentials may come from the AWS chain (e.g. the instance role), which
 * can't be checked without a network call, so a missing credential shows on the first call.
 */
export function claudeMissing(env: NodeJS.ProcessEnv = process.env): string | null {
  if (provider(env) === "anthropic") return env.ANTHROPIC_API_KEY ? null : "ANTHROPIC_API_KEY is not set";
  return env.AWS_REGION || env.AWS_DEFAULT_REGION ? null : "AWS_REGION is not set (Claude runs on Amazon Bedrock)";
}
