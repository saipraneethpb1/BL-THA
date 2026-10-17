import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { loadAndCheck } from "../src/dataset/check.js";
import { CATEGORIES, CATEGORY_IDS, canEverAutoSend } from "../src/email/taxonomy.js";

const valid = {
  id: "E001",
  split: "dev",
  from: { name: "Asha", email: "asha@example.com" },
  subject: "Logo size?",
  body: "What size should my homepage logo be?",
  receivedAt: "2026-10-17T09:00:00Z",
  labels: { category: "how_to", urgency: "normal", needsHuman: false },
  provenance: "handwritten",
};

function dirWith(lines: unknown[]): string {
  const dir = mkdtempSync(join(tmpdir(), "emails-"));
  writeFileSync(join(dir, "a.jsonl"), lines.map((l) => JSON.stringify(l)).join("\n"));
  return dir;
}

describe("dataset check", () => {
  it("accepts a valid record and fills defaults", () => {
    const r = loadAndCheck(dirWith([valid]));
    expect(r.errors).toEqual([]);
    expect(r.emails[0]?.labels.traits).toEqual([]);
    expect(r.shortfalls.length).toBeGreaterThan(0); // one email cannot meet the plan
  });

  it("rejects bad labels, unknown fields and duplicate ids", () => {
    const bad = { ...valid, id: "E002", labels: { ...valid.labels, category: "refund" } };
    const extra = { ...valid, id: "E003", mood: "happy" };
    const r = loadAndCheck(dirWith([valid, valid, bad, extra]));
    expect(r.errors.some((e) => e.includes("labels.category"))).toBe(true);
    expect(r.errors.some((e) => e.includes("mood") || e.includes("Unrecognized"))).toBe(true);
    expect(r.errors).toContain("duplicate id E001");
  });

  it("requires a thread when an email needs history", () => {
    const e = { ...valid, labels: { ...valid.labels, traits: ["needs_history"] } };
    expect(loadAndCheck(dirWith([e])).errors).toContain(
      "E001 is marked needs_history but has no thread",
    );
  });
});

describe("taxonomy rules", () => {
  it("never lets money, access or commitments auto-send", () => {
    for (const id of ["refund_cancellation", "complaint", "quote_request", "project_request", "account_access", "follow_up"] as const) {
      expect(canEverAutoSend(id, "low")).toBe(false);
    }
  });

  it("never auto-sends critical emails, even in eligible categories", () => {
    expect(canEverAutoSend("how_to", "normal")).toBe(true);
    expect(canEverAutoSend("how_to", "critical")).toBe(false);
  });

  it("gives every category a definition and a next action", () => {
    for (const id of CATEGORY_IDS) {
      expect(CATEGORIES[id].definition.length).toBeGreaterThan(20);
      expect(CATEGORIES[id].nextAction.length).toBeGreaterThan(0);
    }
  });
});
