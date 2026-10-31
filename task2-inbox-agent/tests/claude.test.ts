import { describe, expect, it } from "vitest";
import { costUsd, loadConfig } from "../src/config.js";
import { baseName, claudeMissing, effortFor, modelId, provider } from "../src/llm/claude.js";

describe("Claude provider", () => {
  it("defaults to Amazon Bedrock inference profiles", () => {
    expect(provider({})).toBe("bedrock");
    expect(modelId("claude-opus-4-6", {})).toBe("global.anthropic.claude-opus-4-6-v1");
    const au = { CLAUDE_BEDROCK_PROFILE: "au" };
    expect(modelId("claude-sonnet-4-6", au)).toBe("au.anthropic.claude-sonnet-4-6");
    expect(modelId("claude-haiku-4-5", au)).toBe("au.anthropic.claude-haiku-4-5-20251001-v1:0");
    const full = "au.anthropic.claude-haiku-4-5-20251001-v1:0";
    expect(modelId(full, {})).toBe(full);
  });

  it("prices and effort follow the bare model name", () => {
    expect(baseName("au.anthropic.claude-haiku-4-5-20251001-v1:0")).toBe("claude-haiku-4-5");
    expect(baseName("au.anthropic.claude-opus-4-6-v1")).toBe("claude-opus-4-6");
    expect(costUsd("au.anthropic.claude-sonnet-4-6", 1_000_000, 0)).toBe(3);
    expect(effortFor("au.anthropic.claude-haiku-4-5-20251001-v1:0", "low")).toEqual({});
    expect(effortFor("claude-sonnet-4-6", "medium")).toEqual({ effort: "medium" });
  });

  it("needs a region on Bedrock, and the key on Anthropic's API", () => {
    expect(claudeMissing({})).toMatch(/AWS_REGION/);
    expect(loadConfig({}).llmEnabled).toBe(false);
    expect(loadConfig({ AWS_REGION: "ap-southeast-2" }).llmEnabled).toBe(true);
    expect(claudeMissing({ CLAUDE_PROVIDER: "anthropic", AWS_REGION: "ap-southeast-2" })).toMatch(/ANTHROPIC_API_KEY/);
    const direct = { CLAUDE_PROVIDER: "anthropic", ANTHROPIC_API_KEY: "sk-test" };
    expect(loadConfig(direct).llmEnabled).toBe(true);
    expect(modelId("au.anthropic.claude-sonnet-4-6", direct)).toBe("claude-sonnet-4-6");
  });

  it("rejects an unknown provider", () => {
    expect(() => provider({ CLAUDE_PROVIDER: "openai" })).toThrow(/CLAUDE_PROVIDER/);
  });
});
