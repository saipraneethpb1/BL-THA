/** HTTP API and review screen for the Inbox Agent. */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import Fastify, { type FastifyInstance } from "fastify";
import { z } from "zod";
import { loadConfig, type Config } from "./config.js";
import { EmailCase } from "./dataset/schema.js";
import { CATEGORIES, CATEGORY_IDS } from "./email/taxonomy.js";
import { HttpKnowledgeClient } from "./knowledge-client/client.js";
import { ClaudeClassifier, ClaudeDrafter } from "./llm/models.js";
import { InboxAgent, ReviewError, type ReviewAction } from "./pipeline.js";
import { InboxStore } from "./store/db.js";

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

export interface AppDeps {
  config: Config;
  store: InboxStore;
  /** null when no ANTHROPIC_API_KEY: the inbox and reviews still work, drafting does not. */
  agent: InboxAgent | null;
}

export function defaultDeps(config = loadConfig()): AppDeps {
  const store = new InboxStore(config.dbPath);
  const kb = new HttpKnowledgeClient(config.kbUrl, config.kbApiKey);
  const agent = config.llmEnabled
    ? new InboxAgent(store, new ClaudeClassifier(config.classifyModel), new ClaudeDrafter(config.draftModel), kb, config)
    : null;
  return { config, store, agent };
}

/** Drop undefined keys so the parsed body fits ReviewAction under exactOptionalPropertyTypes. */
function compact<T extends object>(o: T): T {
  return Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined)) as T;
}

export function buildApp(deps: AppDeps): FastifyInstance {
  const app = Fastify({ logger: false });
  const { store } = deps;

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
    if (err instanceof z.ZodError) return reply.status(422).send({ error: "invalid input", issues: err.issues });
    if (err instanceof ReviewError) return reply.status(409).send({ error: err.message });
    return reply.status(500).send({ error: err instanceof Error ? err.message : String(err) });
  });

  app.get("/", (_req, reply) => reply.type("text/html").send(readFileSync(join(ROOT, "ui/index.html"), "utf8")));

  app.get("/health", async () => ({ status: "ok", drafting_enabled: deps.agent !== null }));

  app.get("/api/emails", async () => store.listEmails());

  app.get<{ Params: { id: string } }>("/api/emails/:id", async (req, reply) => {
    const email = store.getEmail(req.params.id);
    if (!email) return reply.status(404).send({ error: "not found" });
    return { email, draft: store.latestDraft(email.id) ?? null, reviews: store.reviewsFor(email.id) };
  });

  app.post("/api/emails", async (req, reply) => {
    const e = IncomingEmail.parse(req.body);
    const added = store.addEmail({
      id: e.id, from_name: e.from.name, from_email: e.from.email, subject: e.subject, body: e.body,
      thread: e.thread, received_at: e.receivedAt ?? new Date().toISOString(),
    });
    return reply.status(added ? 201 : 200).send({ id: e.id, added });
  });

  app.post<{ Params: { id: string } }>("/api/emails/:id/process", async (req, reply) => {
    if (!deps.agent) return reply.status(503).send({ error: "drafting is disabled: ANTHROPIC_API_KEY is not configured" });
    return deps.agent.process(req.params.id);
  });

  app.post<{ Params: { id: string } }>("/api/emails/:id/review", async (req, reply) => {
    if (!deps.agent) return reply.status(503).send({ error: "drafting is disabled: ANTHROPIC_API_KEY is not configured" });
    const action = compact(Review.parse(req.body)) as ReviewAction;
    return deps.agent.review(req.params.id, action);
  });

  app.get("/api/autonomy", async () => {
    const states = new Map(store.allAutonomy().map((s) => [s.kind, s]));
    return {
      threshold: deps.config.autonomyThreshold,
      categories: CATEGORY_IDS.map((k) => ({
        ...(states.get(k) ?? { kind: k, streak: 0, status: "supervised" }),
        eligible: CATEGORIES[k].autonomy === "eligible",
      })),
      events: store.autonomyEvents(30),
      pending_spot_checks: store.pendingSpotChecks(),
    };
  });

  app.post<{ Params: { kind: string } }>("/api/autonomy/:kind/revoke", async (req, reply) => {
    const kind = z.enum(CATEGORY_IDS).parse(req.params.kind);
    const { reason } = z.object({ reason: z.string().min(1) }).parse(req.body);
    if (!deps.agent) return reply.status(503).send({ error: "drafting is disabled" });
    deps.agent.revokeAutonomy(kind, reason);
    return { revoked: kind };
  });

  /** Loads the labelled dataset into the inbox (labels are not shown to the agent). */
  app.post<{ Params: { split: string } }>("/api/seed/:split", async (req) => {
    const split = z.enum(["dev", "test"]).parse(req.params.split);
    const lines = readFileSync(join(ROOT, `data/emails/${split}.jsonl`), "utf8").split(/\r?\n/).filter(Boolean);
    let added = 0;
    for (const line of lines) {
      const e = EmailCase.parse(JSON.parse(line));
      if (store.addEmail({ id: e.id, from_name: e.from.name, from_email: e.from.email, subject: e.subject, body: e.body, thread: e.thread, received_at: e.receivedAt })) added++;
    }
    return { split, added, total: lines.length };
  });

  return app;
}
