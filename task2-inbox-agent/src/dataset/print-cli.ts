/** Prints emails in a readable form for review: `npm run dataset:print -- E001 E005` (or all). */
import { join } from "node:path";
import { loadAndCheck } from "./check.js";

const wanted = new Set(process.argv.slice(2));
const { emails } = loadAndCheck(join(import.meta.dirname, "../../data/emails"));

for (const e of emails) {
  if (wanted.size > 0 && !wanted.has(e.id)) continue;
  const l = e.labels;
  console.log(`\n${"=".repeat(78)}\n${e.id} [${e.split}] ${l.category}${l.secondary.length ? ` (+${l.secondary.join(", ")})` : ""} · urgency ${l.urgency} · needsHuman ${l.needsHuman} · ${e.provenance}`);
  console.log(`From: ${e.from.name} <${e.from.email}>   Subject: ${e.subject}`);
  for (const t of e.thread) console.log(`  > [${t.date}] ${t.from}: ${t.body.replace(/\n/g, " ")}`);
  console.log(`\n${e.body}\n`);
  if (l.goldArticles.length) console.log(`gold:      ${l.goldArticles.join(", ")}`);
  for (const m of l.mustInclude) console.log(`must:      ${m}`);
  for (const m of l.mustNotInclude) console.log(`must not:  ${m}`);
  if (l.traits.length) console.log(`traits:    ${l.traits.join(", ")}`);
}
