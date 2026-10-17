import { join } from "node:path";
import { loadAndCheck } from "./check.js";

const report = loadAndCheck(join(import.meta.dirname, "../../data/emails"));
console.log(`${report.emails.length} valid emails`);
for (const e of report.errors) console.log(`ERROR  ${e}`);
for (const s of report.shortfalls) console.log(`SHORT  ${s}`);
if (report.errors.length > 0 || report.shortfalls.length > 0) process.exit(1);
console.log("Dataset meets the plan.");
