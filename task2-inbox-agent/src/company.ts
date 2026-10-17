/**
 * The business the inbox belongs to. Another company plugs in by editing this file
 * (and taxonomy.ts); nothing else in the agent mentions a company by name.
 */
export const COMPANY = {
  name: "Lakeview Web Studio",
  signature: "The Lakeview team",
  what: "a small agency that builds, hosts and maintains Brilliant Directories websites for clients",
  voice: [
    "Warm, plain and direct. Short sentences. No marketing language.",
    "Start with the answer or the next step, not with 'Thank you for reaching out'.",
    "Use the customer's first name. Sign off with the team signature.",
    "When we don't know something yet, say so and say when they will hear back.",
    "Reply in the language the customer wrote in.",
  ],
} as const;
