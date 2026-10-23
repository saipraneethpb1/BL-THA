# AI Tech Consultant

Three connected services that do a software consultant's slow work: knowing the product, answering the inbox, and turning client meetings into a priced scope of work.

| Service | What it does | Language | URL |
|---|---|---|---|
| **Knowledge Brain** (Task 1) | Crawls the Brilliant Directories help center (1,537 articles, 5,040 screenshots) and keeps it fresh daily. Answers questions with citations, and sends anything it can't answer to a human expert. | Python | http://localhost:8001 |
| **Inbox Agent** (Task 2) | Sorts customer emails and drafts replies from the Knowledge Brain. Gmail-style inbox: each email gets a label under a parent type, and a human approves each draft. A label may send on its own after X unedited approvals in a row (X is set in Settings, 20 by default). | TypeScript | http://localhost:8002 |
| **Scope Builder** (Task 3) | A team of agents turns meeting transcripts into requirements, a scope, an estimate and a proposal. It checks existing product features first, compares follow-up meetings with the approved scope, and sends nothing without approval. | Python | http://localhost:8003 |

Tasks 2 and 3 use Task 1 only through its HTTP API (docs at http://localhost:8001/docs).

## Run it (one command)

You need Docker and, for the AI features, an Anthropic API key.

```bash
cp .env.example .env        # then put your ANTHROPIC_API_KEY in .env
docker compose up --build
```

Without a key, all three services still start. Search, the inboxes and review screens work; answering, drafting and building show as switched off.

**First start of the Knowledge Brain.** It needs the help center in its database. It tries, in order:
1. A snapshot at `task1-knowledge-brain/data/snapshot/knowledge.db`. Make one from a crawled install with `uv run python -m kb.snapshot`; it's about 50 MB.
2. `KB_SEED_URL`, which defaults to the snapshot published in this repo's [releases](https://github.com/saipraneethpb1/BL-THA/releases). So a fresh clone is ready in about 1.5 minutes.
3. A full polite crawl: about 25 minutes plus about 15 minutes of indexing. Progress shows on its Status tab.

After that, a daily refresh at 02:00 UTC fetches only pages that changed.

## Live deployment

**Live demo:** https://13-203-203-28.sslip.io. Open it and start using it: no sign-in. Each service is also at `kb.`, `inbox.` and `scope.` in front of that address.

One AWS EC2 t3.small (2 GB RAM plus 2 GB swap, Ubuntu 24.04) runs the same `docker-compose.yml`, plus an overlay, [`deploy/compose.prod.yml`](deploy/compose.prod.yml):
- **Only [Caddy](deploy/Caddyfile) is reachable from the internet.** It serves HTTPS with automatic Let's Encrypt certificates on a free `sslip.io` hostname, with one subdomain per service (`kb.`, `inbox.`, `scope.`).
- **Open to anyone, with spending guarded in the services:**
  - each service caps its own recorded Claude spend: Knowledge Brain $4.50 (drafts and scope builds use it too), Inbox Agent $2.50, Scope Builder $2.50 (`*_BUDGET_USD`);
  - each visitor is rate-limited on the actions that cost money: questions, drafts, builds and lesson checks. Lesson checks, whose cost isn't recorded, are also limited to 3 a day for the whole site;
  - when a cap is reached the service says so and stops calling Claude, and browsing keeps working.
- **A spend limit on the Anthropic account** is the final backstop.
- **Search engines are asked not to index it** (`X-Robots-Tag` and `robots.txt`).
- **To require a login again,** add `import protected` back to each site in [`deploy/Caddyfile`](deploy/Caddyfile).
- **The API key is typed by the owner on the server:** [`deploy/set-secrets.sh`](deploy/set-secrets.sh) prompts for it, and the `.env` it writes never enters git.

**Usage bar.** A bar fixed to the bottom of every page shows the Claude spend recorded by the site, today and in total, against a budget (`SITE_BUDGET_USD` in the server's `.env`, default $10). It also says plainly when the API is failing, for example when the account is out of credit. The figures come from each call's token counts at list prices. The account's actual credit balance is only visible in the Anthropic Console: there's no API for it.

To redeploy:
1. `bash deploy/setup-vm.sh` (once per VM: Docker and swap).
2. Copy the repo over.
3. Run `bash deploy/set-secrets.sh` (first time), or for code-only updates:
   `docker compose -f docker-compose.yml -f deploy/compose.prod.yml up -d --build && docker compose -f docker-compose.yml -f deploy/compose.prod.yml restart caddy`.
   Caddy only reads its config at start, so restart it whenever `deploy/Caddyfile` changes.

**Voice input.** The mic buttons use the browser's own dictation when it works. Behind many VPNs, in Brave-like browsers and in Firefox, they record instead and transcribe on the server, using an open-source Whisper model inside the Knowledge Brain: no third-party service, no per-use cost.

## Run tests and checks

```bash
cd task1-knowledge-brain && uv run pytest && uv run mypy && uv run ruff check .
cd task2-inbox-agent && npm ci && npm test && npm run typecheck
cd task3-meeting-to-scope && uv run pytest && uv run mypy && uv run ruff check .
python scripts/sync_suite.py --check   # the three UIs share one look (shared/)
```

Tests use scripted fake models, so they need no API key and cost nothing.

**Shared UI.** The three services and the hub page share one design and top bar. These live in `shared/` and are copied into each page by `python scripts/sync_suite.py`, because each service is its own Docker image.

## Run a service without Docker

```bash
cd task1-knowledge-brain && uv run python -m kb.ingestion.pipeline --limit 20   # crawl a sample
cd task1-knowledge-brain && uv run python -m kb.retrieval.cli index             # chunk + embed
cd task1-knowledge-brain && uv run uvicorn kb.api:app --port 8001
cd task2-inbox-agent && npm start
cd task3-meeting-to-scope && uv run uvicorn scope.api:app --port 8003
```

## Layout

```
task1-knowledge-brain/   kb/ingestion (crawl, parse, vision), kb/retrieval (chunk, embed, search),
                         kb/answer (answers, escalations), kb/ui, evaluation/, tests/
task2-inbox-agent/       src/ (pipeline, policy, autonomy, store, server), prompts/, ui/,
                         data/emails/ (labelled dataset), tests/
task3-meeting-to-scope/  scope/agents (analyst, feasibility, scope, estimator, proposal, changes),
                         scope/prompts, scope/ui, transcripts/, tests/
```

More: [DESIGN.md](DESIGN.md) (why it is built this way), [EVALS.md](EVALS.md) (measured quality, cost and speed), [AI_LOG.md](AI_LOG.md) (how AI tools were used).
