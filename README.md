# AI Tech Consultant

Three connected services that do a software consultant's slow work: knowing the product, answering the inbox, and turning client meetings into a priced scope of work.

| Service | What it does | Language | URL |
|---|---|---|---|
| **Knowledge Brain** (Task 1) | Crawls the Brilliant Directories help center (1,537 articles, 5,040 screenshots) and keeps it fresh daily. Answers questions with citations, and sends anything it can't answer to a human expert. | Python | http://localhost:8001 |
| **Inbox Agent** (Task 2) | Sorts customer emails and drafts replies from the Knowledge Brain. A human approves each draft. A kind of email may send on its own only after 20 unedited approvals in a row. | TypeScript | http://localhost:8002 |
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

**Live demo:** https://13-203-203-28.sslip.io (login required; the credentials are in the submission email). Each service is also at `kb.`, `inbox.` and `scope.` in front of that address.

One AWS EC2 t3.small (2 GB RAM plus 2 GB swap, Ubuntu 24.04) runs the same `docker-compose.yml`, plus an overlay, [`deploy/compose.prod.yml`](deploy/compose.prod.yml):
- **Only [Caddy](deploy/Caddyfile) is reachable from the internet.** It serves HTTPS with automatic Let's Encrypt certificates on a free `sslip.io` hostname, with one subdomain per service (`kb.`, `inbox.`, `scope.`).
- **Every page needs a login,** so strangers can't spend the API credit. Only `/health` is open.
- **Secrets are typed by the owner on the server.** [`deploy/set-secrets.sh`](deploy/set-secrets.sh) prompts for the Anthropic key and the login password. The `.env` and the password hash it writes never enter git.

To redeploy:
1. `bash deploy/setup-vm.sh` (once per VM: Docker and swap).
2. Copy the repo over.
3. Run `bash deploy/set-secrets.sh`.

## Run tests and checks

```bash
cd task1-knowledge-brain && uv run pytest && uv run mypy && uv run ruff check .
cd task2-inbox-agent && npm ci && npm test && npm run typecheck
cd task3-meeting-to-scope && uv run pytest && uv run mypy && uv run ruff check .
```

Tests use scripted fake models, so they need no API key and cost nothing.

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
