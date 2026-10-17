/**
 * Validates every email in data/emails/*.jsonl and reports coverage against the plan.
 * Exit code 1 if any record is invalid or a target is not met.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { CATEGORY_IDS } from "../email/taxonomy.js";
import { CATEGORY_TARGETS, MIN_CRITICAL, TRAIT_MINIMUMS } from "./plan.js";
import { EmailCase, TRAITS } from "./schema.js";

export interface CheckReport {
  emails: EmailCase[];
  errors: string[];
  shortfalls: string[];
}

export function loadAndCheck(dir: string): CheckReport {
  const errors: string[] = [];
  const emails: EmailCase[] = [];
  const files = readdirSync(dir).filter((f) => f.endsWith(".jsonl"));
  for (const file of files) {
    const lines = readFileSync(join(dir, file), "utf8").split(/\r?\n/);
    lines.forEach((line, i) => {
      if (!line.trim()) return;
      let raw: unknown;
      try {
        raw = JSON.parse(line);
      } catch {
        errors.push(`${file}:${i + 1} is not valid JSON`);
        return;
      }
      const parsed = EmailCase.safeParse(raw);
      if (parsed.success) emails.push(parsed.data);
      else errors.push(`${file}:${i + 1} ${parsed.error.issues.map((x) => `${x.path.join(".")}: ${x.message}`).join("; ")}`);
    });
  }

  const ids = new Set<string>();
  for (const e of emails) {
    if (ids.has(e.id)) errors.push(`duplicate id ${e.id}`);
    ids.add(e.id);
    if (e.labels.traits.includes("needs_history") && e.thread.length === 0) {
      errors.push(`${e.id} is marked needs_history but has no thread`);
    }
  }

  const shortfalls: string[] = [];
  for (const cat of CATEGORY_IDS) {
    for (const split of ["dev", "test"] as const) {
      const have = emails.filter((e) => e.labels.category === cat && e.split === split).length;
      const want = CATEGORY_TARGETS[cat][split];
      if (have < want) shortfalls.push(`${cat} (${split}): ${have}/${want}`);
    }
  }
  for (const trait of TRAITS) {
    const have = emails.filter((e) => e.labels.traits.includes(trait)).length;
    if (have < TRAIT_MINIMUMS[trait]) shortfalls.push(`trait ${trait}: ${have}/${TRAIT_MINIMUMS[trait]}`);
  }
  const critical = emails.filter((e) => e.labels.urgency === "critical").length;
  if (critical < MIN_CRITICAL) shortfalls.push(`critical urgency: ${critical}/${MIN_CRITICAL}`);

  return { emails, errors, shortfalls };
}
