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
1. A snapshot at `task1-knowledge-brain/data/snapshot/knowledge.db`. Make one from a crawled install with `uv run python -m kb.snapshot`; it's about 42 MB.
2. `KB_SEED_URL` in `.env`, for example the same file attached to a GitHub release.
3. A full polite crawl: about 25 minutes plus about 15 minutes of indexing. Progress shows on its Status tab.

After that, a daily refresh at 02:00 UTC fetches only pages that changed.

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
