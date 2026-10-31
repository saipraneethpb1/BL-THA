/** HTTP API and review screen for the Inbox Agent. */
import { createHash, randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import Anthropic from "@anthropic-ai/sdk";
import Fastify, { type FastifyInstance, type FastifyRequest } from "fastify";
import { z } from "zod";
import { loadConfig, type Config } from "./config.js";
import { CATEGORIES, CATEGORY_IDS, eligibleByDefault, PARENTS } from "./email/taxonomy.js";
import { HttpKnowledgeClient } from "./knowledge-client/client.js";
import { claudeMissing } from "./llm/claude.js";
import { ClaudeClassifier, ClaudeDrafter } from "./llm/models.js";
import { ClaudeLessonMiner, LearningError, LearningService, LearningUnavailableError } from "./feedback/learning.js";
import { InboxAgent, ReviewError, type ReviewAction } from "./pipeline.js";
import { checkBudget, GuardError, RateLimiter, visitor } from "./guard.js";
import { readSettings, THRESHOLD_MAX, THRESHOLD_MIN, updateSettings } from "./settings.js";
import { parentsOf, TopicError, type TopicIn } from "./email/topics.js";
import type { CategoryId } from "./email/taxonomy.js";
import { InboxStore } from "./store/db.js";
import { accountFromToken, COOKIE, cookieDomain, cookieValue, GUEST, makeToken, sessionSecret } from "./session.js";
import { authUrl, exchangeCode, identity, type Fetch } from "./gmail/google.js";
import { GmailService } from "./gmail/service.js";
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
  /** For tests: a fake fetch for Google's APIs. */
  googleFetch?: Fetch;
}

export function defaultDeps(config = loadConfig()): AppDeps {
  const kb = new HttpKnowledgeClient(config.kbUrl, config.kbApiKey);
  const make = (store: InboxStore): Workspace => {
    const drafter = config.llmEnabled ? new ClaudeDrafter(config.draftModel) : null;
    const agent = drafter
      ? new InboxAgent(store, new ClaudeClassifier(config.classifyModel), drafter, kb, config)
      : null;
    const miner = config.llmEnabled ? new ClaudeLessonMiner(config.lessonModel) : null;
    // The loop runs by itself now, so each check re-drafts at most 6 past emails (cost).
    return { store, agent, learning: new LearningService(store, miner, drafter, { evalLimit: 6 }) };
  };
  const workspaces = new Workspaces(config.dbPath, make, ROOT);
  return { config, ...workspaces.get(GUEST), workspaces, sessionSecret: sessionSecret() };
}

/** The provider's own one-line reason (e.g. out of credit, no model access), not the whole error body. */
export function claudeMessage(err: InstanceType<typeof Anthropic.APIError>): string {
  const body = err.error as { error?: { message?: unknown }; message?: unknown } | undefined;
  if (typeof body?.error?.message === "string") return body.error.message;
  // Bedrock puts the reason at the top level: {"message": "You don't have access to ..."}.
  return typeof body?.message === "string" ? body.message : err.message;
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
    if (err instanceof RangeError || err instanceof TopicError) return reply.status(422).send({ error: err.message });
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
    if (!agent) return reply.status(503).send({ error: `drafting is disabled: ${claudeMissing() ?? "Claude is not configured"}` });
    draftLimiter.check(req);
    guardSpend();
    const r = await agent.process(req.params.id);
    // A Gmail email: label it in Gmail, and send it there if it was auto-sent.
    const delivery_error = await gmailOf(ws(req))?.afterProcess(req.params.id, r.autoSent).then(() => null, (e: Error) => e.message);
    return { ...r, ...(delivery_error ? { delivery_error } : {}) };
  });

  app.post<{ Params: { id: string } }>("/api/emails/:id/review", async (req, reply) => {
    const agent = ws(req).agent;
    if (!agent) return reply.status(503).send({ error: `drafting is disabled: ${claudeMissing() ?? "Claude is not configured"}` });
    const action = compact(Review.parse(req.body)) as ReviewAction;
    const r = agent.review(req.params.id, action);
    if (r.outcome === "edited" || r.outcome === "rejected") {
      // The learning loop runs by itself: enough corrections of one request type propose,
      // check and (if the check recommends it) make lessons live. Within budget and limits.
      const draft = ws(req).store.latestDraft(req.params.id);
      const type = (draft?.classification as { category?: CategoryId } | undefined)?.category;
      const learning = ws(req).learning;
      if (type) {
        void learning
          .autoLearn(type, () => { guardSpend(); siteLessonChecks.checkKey("site"); })
          .then((what) => { if (what) req.log.info({ type, what }, "learning loop"); })
          .catch((e: Error) => req.log.warn({ type, error: e.message }, "learning loop stopped"));
      }
    }
    // A Gmail email that was just sent: send it from the connected Gmail account.
    const delivery_error = await gmailOf(ws(req))?.deliver(req.params.id).then(() => null, (e: Error) => e.message);
    return { ...r, ...(delivery_error ? { delivery_error } : {}) };
  });

  const settingsOf = (req: FastifyRequest) => readSettings(ws(req).store, deps.config.autonomyThreshold);

  /** Spam, Trash, or back where it was. Nothing is deleted. Mirrored in Gmail for Gmail mail. */
  app.post<{ Params: { id: string } }>("/api/emails/:id/move", async (req, reply) => {
    const { to } = z.object({ to: z.enum(["spam", "trash", "back"]) }).parse(req.body);
    const store = ws(req).store;
    const email = store.getEmail(req.params.id);
    if (!email) return reply.status(404).send({ error: "not found" });
    const status = store.moveEmail(email.id, to);
    const gmail_error = await gmailOf(ws(req))?.mirrorMove(email.id, to, email.status).then(() => null, (e: Error) => e.message);
    return { id: email.id, status, ...(gmail_error ? { gmail_error } : {}) };
  });

  app.get("/api/autonomy", async (req) => {
    const states = new Map(ws(req).store.allAutonomy().map((s) => [s.kind, s]));
    const settings = settingsOf(req);
    return {
      threshold: settings.autonomyThreshold,
      // One counter per email category (Header, Filters...), grouped by parent setting type.
      categories: settings.topics.map((t) => ({
        ...(states.get(t.id) ?? { kind: t.id, streak: 0, status: "supervised" }),
        label: t.label,
        parent: t.parent,
        eligible: t.auto_send,
      })),
      request_types: CATEGORY_IDS.map((k) => ({ id: k, label: CATEGORIES[k].label, auto_send: settings.autoSend[k] })),
      events: ws(req).store.autonomyEvents(30),
      pending_spot_checks: ws(req).store.pendingSpotChecks(),
    };
  });

  app.post<{ Params: { kind: string } }>("/api/autonomy/:kind/revoke", async (req, reply) => {
    const kind = req.params.kind;
    if (!settingsOf(req).topics.some((t) => t.id === kind)) return reply.status(404).send({ error: "no such category" });
    const { reason } = z.object({ reason: z.string().min(1) }).parse(req.body);
    const agent = ws(req).agent;
    if (!agent) return reply.status(503).send({ error: "drafting is disabled" });
    agent.revokeAutonomy(kind, reason);
    return { revoked: kind };
  });

  /** Labels, their parent types, X and the auto-send switches for this inbox. */
  app.get("/api/settings", async (req) => {
    const settings = settingsOf(req);
    return {
      autonomy_threshold: settings.autonomyThreshold,
      default_threshold: deps.config.autonomyThreshold,
      threshold_range: [THRESHOLD_MIN, THRESHOLD_MAX],
      // Email categories (editable) and their parent setting types.
      topics: settings.topics,
      parents: parentsOf(settings.topics),
      // Request types: the safety layer. Some never auto-send unless switched on here.
      request_types: CATEGORY_IDS.map((id) => ({
        id, label: CATEGORIES[id].label, group: PARENTS[CATEGORIES[id].parent].label, risk: CATEGORIES[id].risk,
        definition: CATEGORIES[id].definition, auto_send: settings.autoSend[id], default_auto_send: eligibleByDefault(id),
      })),
    };
  });

  const SettingsIn = z.object({
    autonomy_threshold: z.number().int().optional(),
    auto_send: z.partialRecord(z.enum(CATEGORY_IDS), z.boolean()).optional(),
    topics: z.array(z.object({
      id: z.string().max(60).optional(),
      label: z.string().max(80),
      parent: z.string().max(80),
      description: z.string().max(600).default(""),
      auto_send: z.boolean().optional(),
    })).max(60).optional(),
    actor: z.string().min(1).max(80).optional(),
  });

  app.put("/api/settings", async (req, reply) => {
    const body = SettingsIn.parse(req.body);
    const account = accountOf(req);
    // The guest inbox is shared by every visitor, so its settings need a demo account.
    if (deps.workspaces && account === GUEST && visitor(req) !== null) {
      return reply.status(401).send({ error: "Sign in with a demo account to change settings: the guest inbox is shared." });
    }
    const r = updateSettings(ws(req).store, deps.config.autonomyThreshold,
      { autonomyThreshold: body.autonomy_threshold, autoSend: body.auto_send, topics: body.topics as TopicIn[] | undefined }, body.actor ?? account);
    return { autonomy_threshold: r.settings.autonomyThreshold, auto_send: r.settings.autoSend, topics: r.settings.topics, events: r.events };
  });

  // ----- Gmail (docs/GMAIL_SETUP.md). Connecting signs in with Google and opens a private
  // workspace for that Google account: Gmail never goes into the shared guest or demo inboxes.

  const google = deps.config.google;
  const gmailServices = new WeakMap<InboxStore, GmailService>();
  const gmailOf = (w: Workspace): GmailService | null => {
    if (!google) return null;
    let svc = gmailServices.get(w.store);
    if (!svc) gmailServices.set(w.store, (svc = new GmailService(w.store, w.agent, google, secret, deps.googleFetch ?? fetch)));
    return svc;
  };
  const isHttps = (req: FastifyRequest) => req.headers["x-forwarded-proto"] === "https" || req.protocol === "https";
  const redirectUri = (req: FastifyRequest) => `${isHttps(req) ? "https" : "http"}://${req.headers.host}/api/gmail/callback`;
  const cookie = (req: FastifyRequest, name: string, value: string, opts: { maxAge: number; httpOnly?: boolean; path?: string; shared?: boolean }) => {
    const domain = opts.shared ? cookieDomain(req.headers.host) : undefined;
    return [`${name}=${encodeURIComponent(value)}`, `Max-Age=${opts.maxAge}`, `Path=${opts.path ?? "/"}`, "SameSite=Lax",
      ...(domain ? [`Domain=${domain}`] : []), ...(opts.httpOnly === false ? [] : ["HttpOnly"]), ...(isHttps(req) ? ["Secure"] : [])].join("; ");
  };
  /** A Google account's workspace name: a hash of its stable Google id, never the address. */
  const googleAccount = (sub: string) => `g_${createHash("sha256").update(`google:${sub}`).digest("hex").slice(0, 16)}`;
  const gmailSync = new RateLimiter(10, 600, "Gmail syncs");

  app.get("/api/gmail/status", async (req) => {
    const account = accountOf(req);
    const svc = account.startsWith("g_") ? gmailOf(ws(req)) : null;
    return { configured: google !== null, workspace: account, ...(svc ? svc.status() : { connected: false }) };
  });

  app.get("/api/gmail/connect", async (req, reply) => {
    if (!google || !deps.workspaces) return reply.status(503).type("text/html").send("Gmail isn't set up on this server yet (see docs/GMAIL_SETUP.md).");
    const nonce = randomBytes(16).toString("hex"); // ties Google's answer to this browser
    void reply.header("set-cookie", cookie(req, "bl_oauth", nonce, { maxAge: 600, path: "/api/gmail" }));
    return reply.redirect(authUrl(google, redirectUri(req), nonce));
  });

  app.get<{ Querystring: { code?: string; state?: string; error?: string } }>("/api/gmail/callback", async (req, reply) => {
    const back = (why: string) => reply.redirect(`/#/gmail-error/${encodeURIComponent(why)}`);
    if (!google || !deps.workspaces) return back("Gmail isn't set up on this server.");
    if (req.query.error) return back(req.query.error === "access_denied" ? "Google access wasn't granted." : req.query.error);
    const expected = cookieValue(req.headers.cookie, "bl_oauth");
    if (!req.query.code || !expected || req.query.state !== expected) return back("The sign-in expired or came from another browser. Try again.");
    let tokens, who;
    try {
      tokens = await exchangeCode(google, req.query.code, redirectUri(req), deps.googleFetch ?? fetch);
      who = identity(tokens.id_token);
    } catch (e) {
      return back(e instanceof Error ? e.message : String(e));
    }
    const account = googleAccount(who.sub);
    const svc = gmailOf(deps.workspaces.get(account))!;
    try { svc.connect(tokens, who); } catch (e) { return back(e instanceof Error ? e.message : String(e)); }
    void reply.header("set-cookie", [
      cookie(req, COOKIE, makeToken(secret, account), { maxAge: 30 * 24 * 3600, shared: true }),
      cookie(req, "bl_who", who.email, { maxAge: 30 * 24 * 3600, shared: true, httpOnly: false }), // shown in the top bar
      cookie(req, "bl_oauth", "", { maxAge: 0, path: "/api/gmail" }),
    ]);
    // First import in the background; the page shows the mail arriving.
    void svc.sync({ draftLimit: 3, beforeDraft: guardSpend }).catch(() => undefined);
    return reply.redirect("/#/folder/inbox");
  });

  const gmailHere = (req: FastifyRequest) => {
    const svc = accountOf(req).startsWith("g_") ? gmailOf(ws(req)) : null;
    if (!svc?.connection()) throw new ReviewError("this inbox isn't connected to Gmail");
    return svc;
  };

  app.post("/api/gmail/sync", async (req) => {
    const svc = gmailHere(req);
    gmailSync.check(req);
    guardSpend();
    return svc.sync({ draftLimit: 5, beforeDraft: guardSpend });
  });

  app.post<{ Params: { id: string } }>("/api/gmail/deliver/:id", async (req) => {
    await gmailHere(req).deliver(req.params.id);
    return { delivered: req.params.id };
  });

  /** Revokes Google's access and erases everything imported, then signs out. */
  app.post("/api/gmail/disconnect", async (req, reply) => {
    const account = accountOf(req);
    if (!account.startsWith("g_") || !deps.workspaces) return reply.status(409).send({ error: "this inbox isn't a Gmail workspace" });
    await gmailOf(ws(req))?.disconnect();
    deps.workspaces.erase(account);
    void reply.header("set-cookie", [
      cookie(req, COOKIE, "", { maxAge: 0, shared: true }),
      cookie(req, "bl_who", "", { maxAge: 0, shared: true, httpOnly: false }),
    ]);
    return { erased: true };
  });

  // Connected Gmail inboxes are checked for new mail every few minutes (within the budget).
  if (google && deps.workspaces && deps.config.gmailSyncMinutes > 0) {
    const all = deps.workspaces;
    const timer = setInterval(() => {
      void (async () => {
        for (const name of all.names().filter((n) => n.startsWith("g_"))) {
          const svc = gmailOf(all.get(name));
          if (!svc?.connection()) continue;
          try { guardSpend(); } catch { return; }
          await svc.sync({ draftLimit: 3, beforeDraft: guardSpend }).catch(() => undefined);
        }
      })();
    }, deps.config.gmailSyncMinutes * 60_000);
    timer.unref();
    app.addHook("onClose", async () => clearInterval(timer));
  }

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
