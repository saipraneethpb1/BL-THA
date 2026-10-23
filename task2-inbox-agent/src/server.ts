/** HTTP API and review screen for the Inbox Agent. */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import Anthropic from "@anthropic-ai/sdk";
import Fastify, { type FastifyInstance, type FastifyRequest } from "fastify";
import { z } from "zod";
import { loadConfig, type Config } from "./config.js";
import { CATEGORIES, CATEGORY_IDS } from "./email/taxonomy.js";
import { HttpKnowledgeClient } from "./knowledge-client/client.js";
import { ClaudeClassifier, ClaudeDrafter } from "./llm/models.js";
import { ClaudeLessonMiner, LearningError, LearningService, LearningUnavailableError } from "./feedback/learning.js";
import { InboxAgent, ReviewError, type ReviewAction } from "./pipeline.js";
import { checkBudget, GuardError, RateLimiter } from "./guard.js";
import { InboxStore } from "./store/db.js";
import { accountFromToken, cookieValue, GUEST, sessionSecret } from "./session.js";
import { seedEmails, Workspaces, type Workspace } from "./workspaces.js";

const ROOT = join(import.meta.dirname, "..");

const IncomingEmail = z.object({
  id: z.string().min(1).max(64),
  from: z.object({ name: z.string().min(1), email: z.email() }),
  subject: z.string().max(500),
  body: z.string().min(1).max(50_000),
  thread: z.array(z.object({ from: z.string(), date: z.string(), body: z.string() })).default([]),
  receivedAt: z.iso.datetime().optional(),
});

const Review = z.discriminatedUnion("action", [
  z.object({ action: z.literal("send"), subject: z.string().optional(), body: z.string().min(1).optional(), reviewer: z.string().min(1), reason: z.string().optional() }),
  z.object({ action: z.literal("reject"), subject: z.string().optional(), body: z.string().min(1), reviewer: z.string().min(1), reason: z.string().min(1) }),
  z.object({ action: z.literal("escalate"), reviewer: z.string().min(1), reason: z.string().min(1) }),
  z.object({ action: z.literal("archive"), reviewer: z.string().min(1), reason: z.string().optional() }),
  z.object({ action: z.literal("spot_check"), ok: z.boolean(), reviewer: z.string().min(1), reason: z.string().optional() }),
]);

export interface AppDeps extends Workspace {
  config: Config;
  /** One inbox per demo account (src/workspaces.ts). Without it, everyone shares the one
   *  inbox above: that's how the tests run. */
  workspaces?: Workspaces;
  sessionSecret?: string;
}

export function defaultDeps(config = loadConfig()): AppDeps {
  const kb = new HttpKnowledgeClient(config.kbUrl, config.kbApiKey);
  const make = (store: InboxStore): Workspace => {
    const drafter = config.llmEnabled ? new ClaudeDrafter(config.draftModel) : null;
    const agent = drafter
      ? new InboxAgent(store, new ClaudeClassifier(config.classifyModel), drafter, kb, config)
      : null;
    const miner = config.llmEnabled ? new ClaudeLessonMiner(config.lessonModel) : null;
    return { store, agent, learning: new LearningService(store, miner, drafter) };
  };
  const workspaces = new Workspaces(config.dbPath, make, ROOT);
  return { config, ...workspaces.get(GUEST), workspaces, sessionSecret: sessionSecret() };
}

/** Anthropic's own one-line reason (e.g. out of credit), not the whole error body. */
export function claudeMessage(err: InstanceType<typeof Anthropic.APIError>): string {
  const inner = (err.error as { error?: { message?: unknown } } | undefined)?.error;
  return typeof inner?.message === "string" ? inner.message : err.message;
}

/** Drop undefined keys so the parsed body fits ReviewAction under exactOptionalPropertyTypes. */
function compact<T extends object>(o: T): T {
  return Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined)) as T;
}

export function buildApp(deps: AppDeps): FastifyInstance {
  const app = Fastify({ logger: false });
  // Which inbox a request uses: the signed-in demo account's own, or the shared guest inbox.
  const secret = deps.sessionSecret ?? sessionSecret();
  const single: Workspace = { store: deps.store, agent: deps.agent, learning: deps.learning };
  const accountOf = (req: FastifyRequest) => accountFromToken(secret, cookieValue(req.headers.cookie)) ?? GUEST;
  const ws = (req: FastifyRequest): Workspace => (deps.workspaces ? deps.workspaces.get(accountOf(req)) : single);
  // Spend is site-wide: budgets and the usage bar count every inbox together.
  const siteUsage = () => {
    const parts = (deps.workspaces ? deps.workspaces.all() : [single]).map((w) => w.store.usage());
    const ok = parts.map((u) => u.last_success_at).filter((t): t is string => t !== null).sort().at(-1) ?? null;
    return {
      total_usd: parts.reduce((a, u) => a + u.total_usd, 0),
      today_usd: parts.reduce((a, u) => a + u.today_usd, 0),
      calls: parts.reduce((a, u) => a + u.calls, 0),
      last_success_at: ok,
    };
  };
  let lastClaudeError: { message: string; at: string } | null = null;
  // Public demo guards: per-visitor limits on what costs money, and a spending cap.
  const draftLimiter = new RateLimiter(deps.config.draftLimitPer10Min, 600, "drafts");
  const lessonLimiter = new RateLimiter(3, 3600, "lesson checks");
  // A lesson check re-drafts past emails and its cost isn't recorded, so it is also
  // limited for the whole site, not only per visitor.
  const siteLessonChecks = new RateLimiter(deps.config.lessonChecksPerDay, 86400, "lesson checks on this demo today");
  const guardSpend = () => checkBudget(siteUsage().total_usd, deps.config.budgetUsd);

  // Accept an empty body sent as JSON (browsers and curl often label body-less POSTs).
  app.removeContentTypeParser("application/json");
  app.addContentTypeParser("application/json", { parseAs: "string" }, (_req, body, done) => {
    const text = String(body);
    if (text.trim() === "") return done(null, {});
    try {
      done(null, JSON.parse(text));
    } catch {
      done(Object.assign(new Error("body is not valid JSON"), { statusCode: 400 }), undefined);
    }
  });

  app.setErrorHandler((err, _req, reply) => {
    if (err instanceof GuardError) {
      if (err.retryAfterS) void reply.header("Retry-After", String(err.retryAfterS));
      return reply.status(429).send({ error: err.message });
    }
    if (err instanceof z.ZodError) return reply.status(422).send({ error: "invalid input", issues: err.issues });
    if (err instanceof ReviewError) return reply.status(409).send({ error: err.message });
    if (err instanceof LearningUnavailableError) return reply.status(503).send({ error: err.message });
    if (err instanceof LearningError) return reply.status(409).send({ error: err.message });
    // A failed model call (no credit, overloaded, network) is the provider's problem, not a bug:
    // say so plainly. Browsing and reviewing keep working.
    if (err instanceof Anthropic.APIError) {
      lastClaudeError = { message: claudeMessage(err), at: new Date().toISOString() };
      return reply.status(502).send({ error: `Claude API error: ${lastClaudeError.message}` });
    }
    return reply.status(500).send({ error: err instanceof Error ? err.message : String(err) });
  });

  app.get("/", (_req, reply) => reply.type("text/html").send(readFileSync(join(ROOT, "ui/index.html"), "utf8")));

  app.get("/health", async () => ({ status: "ok", drafting_enabled: single.agent !== null }));

  app.get("/api/emails", async (req) => ws(req).store.listEmails());

  // For the usage bar: Claude spend recorded here, and the last Claude API failure.
  // `includes_kb_lookups` tells the bar not to count those Knowledge Brain calls twice.
  app.get("/api/usage", async () => {
    const u = siteUsage();
    const budget = deps.config.budgetUsd;
    return { service: "inbox", ...u, includes_kb_lookups: true, last_error: lastClaudeError, budget_usd: budget, over_budget: budget !== null && u.total_usd >= budget };
  });

  app.get<{ Params: { id: string } }>("/api/emails/:id", async (req, reply) => {
    const email = ws(req).store.getEmail(req.params.id);
    if (!email) return reply.status(404).send({ error: "not found" });
    return { email, draft: ws(req).store.latestDraft(email.id) ?? null, reviews: ws(req).store.reviewsFor(email.id) };
  });

  app.post("/api/emails", async (req, reply) => {
    const e = IncomingEmail.parse(req.body);
    const added = ws(req).store.addEmail({
      id: e.id, from_name: e.from.name, from_email: e.from.email, subject: e.subject, body: e.body,
      thread: e.thread, received_at: e.receivedAt ?? new Date().toISOString(),
    });
    return reply.status(added ? 201 : 200).send({ id: e.id, added });
  });

  app.post<{ Params: { id: string } }>("/api/emails/:id/process", async (req, reply) => {
    const agent = ws(req).agent;
    if (!agent) return reply.status(503).send({ error: "drafting is disabled: ANTHROPIC_API_KEY is not configured" });
    draftLimiter.check(req);
    guardSpend();
    return agent.process(req.params.id);
  });

  app.post<{ Params: { id: string } }>("/api/emails/:id/review", async (req, reply) => {
    const agent = ws(req).agent;
    if (!agent) return reply.status(503).send({ error: "drafting is disabled: ANTHROPIC_API_KEY is not configured" });
    const action = compact(Review.parse(req.body)) as ReviewAction;
    return agent.review(req.params.id, action);
  });

  app.get("/api/autonomy", async (req) => {
    const states = new Map(ws(req).store.allAutonomy().map((s) => [s.kind, s]));
    return {
      threshold: deps.config.autonomyThreshold,
      categories: CATEGORY_IDS.map((k) => ({
        ...(states.get(k) ?? { kind: k, streak: 0, status: "supervised" }),
        eligible: CATEGORIES[k].autonomy === "eligible",
      })),
      events: ws(req).store.autonomyEvents(30),
      pending_spot_checks: ws(req).store.pendingSpotChecks(),
    };
  });

  app.post<{ Params: { kind: string } }>("/api/autonomy/:kind/revoke", async (req, reply) => {
    const kind = z.enum(CATEGORY_IDS).parse(req.params.kind);
    const { reason } = z.object({ reason: z.string().min(1) }).parse(req.body);
    const agent = ws(req).agent;
    if (!agent) return reply.status(503).send({ error: "drafting is disabled" });
    agent.revokeAutonomy(kind, reason);
    return { revoked: kind };
  });

  /** Loads the labelled dataset into this inbox (labels are not shown to the agent). */
  app.post<{ Params: { split: string } }>("/api/seed/:split", async (req) => {
    const split = z.enum(["dev", "test"]).parse(req.params.split);
    return { split, ...seedEmails(ws(req).store, ROOT, split) };
  });

  /** Which inbox this visitor is using (for the UI). */
  app.get("/api/session", async (req) => ({ workspace: accountOf(req) }));

  // ----- learning from edits -----

  const Actor = z.object({ actor: z.string().min(1), note: z.string().min(1).optional() });
  const kindParam = (raw: string) => z.enum(CATEGORY_IDS).parse(raw);

  app.get("/api/learning", async (req) => ({
    categories: CATEGORY_IDS.map((c) => ({
      category: c,
      active: ws(req).store.activeLessons(c),
      versions: ws(req).store.lessonVersions(c),
      corrections_since_active: ws(req).learning.corrections(c).length,
      metrics: ws(req).learning.metrics(c),
    })),
    events: ws(req).store.lessonEvents(40),
  }));

  app.post<{ Params: { category: string } }>("/api/learning/:category/propose", async (req, reply) => {
    const { actor } = Actor.parse(req.body);
    lessonLimiter.check(req);
    guardSpend();
    const version = await ws(req).learning.propose(kindParam(req.params.category), actor);
    return reply.status(201).send({ version });
  });

  app.post<{ Params: { category: string; version: string } }>(
    "/api/learning/:category/:version/evaluate",
    async (req) => {
      const { actor } = Actor.parse(req.body);
      lessonLimiter.check(req); // a check re-drafts past emails: the costliest action here
      siteLessonChecks.checkKey("site");
      guardSpend();
      return ws(req).learning.evaluate(kindParam(req.params.category), Number(req.params.version), actor);
    },
  );

  app.post<{ Params: { category: string; version: string } }>(
    "/api/learning/:category/:version/promote",
    async (req) => {
      const { actor, note } = Actor.parse(req.body);
      ws(req).learning.promote(kindParam(req.params.category), Number(req.params.version), actor, note ?? null);
      return { promoted: Number(req.params.version) };
    },
  );

  app.post<{ Params: { category: string; version: string } }>(
    "/api/learning/:category/:version/reject",
    async (req) => {
      const { actor, note } = Actor.extend({ note: z.string().min(1) }).parse(req.body);
      ws(req).learning.reject(kindParam(req.params.category), Number(req.params.version), actor, note);
      return { rejected: Number(req.params.version) };
    },
  );

  app.post<{ Params: { category: string } }>("/api/learning/:category/rollback", async (req) => {
    const { actor, note } = Actor.extend({ note: z.string().min(1) }).parse(req.body);
    return { active_version: ws(req).learning.rollback(kindParam(req.params.category), actor, note) };
  });

  return app;
}
