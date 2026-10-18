/**
 * Guards for a public demo with no login: per-visitor rate limits and a spending cap.
 * Visitors come through the proxy, which adds X-Forwarded-For; requests without it
 * (tests, local use) aren't rate-limited.
 */
import type { FastifyRequest } from "fastify";

export class GuardError extends Error {
  constructor(message: string, readonly retryAfterS?: number) {
    super(message);
  }
}

export function visitor(req: FastifyRequest): string | null {
  const fwd = req.headers["x-forwarded-for"];
  const first = (Array.isArray(fwd) ? fwd[0] : fwd)?.split(",")[0]?.trim();
  return first || null;
}

/** At most `limit` calls per `windowS` seconds per visitor (sliding window, in memory). */
export class RateLimiter {
  private readonly calls = new Map<string, number[]>();
  constructor(readonly limit: number, readonly windowS: number, readonly what: string, private readonly now = () => Date.now()) {}

  check(req: FastifyRequest): void {
    const who = visitor(req);
    if (!who) return;
    const t = this.now();
    const recent = (this.calls.get(who) ?? []).filter((c) => t - c < this.windowS * 1000);
    if (recent.length >= this.limit) {
      const wait = Math.ceil((this.windowS * 1000 - (t - recent[0]!)) / 1000);
      const per = this.windowS >= 3600 ? `${this.windowS / 3600} hour(s)` : `${Math.round(this.windowS / 60)} minutes`;
      throw new GuardError(
        `Too many ${this.what} from your connection in a short time (this public demo allows ${this.limit} per ${per}). Please try again in about ${Math.max(1, Math.round(wait / 60))} minute(s).`,
        wait,
      );
    }
    recent.push(t);
    this.calls.set(who, recent);
  }
}

export function checkBudget(spentUsd: number, budgetUsd: number | null): void {
  if (budgetUsd !== null && spentUsd >= budgetUsd) {
    throw new GuardError(
      `This demo's AI budget for the Inbox Agent ($${budgetUsd.toFixed(2)}) is used up, so new drafts are paused. You can still browse and review emails.`,
    );
  }
}
