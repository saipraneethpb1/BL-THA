# Knowledge Brain (Task 1)

Crawls the Brilliant Directories help center (1,537 articles, 5,040 screenshots), keeps it
fresh daily, and answers questions with citations. Anything it can't answer becomes a task
for a human expert, whose answer is then added to the knowledge base. Python, FastAPI,
SQLite. Runs on http://localhost:8001 (API docs at `/docs`).

The Inbox Agent and the Scope Builder use it only through its HTTP API (`kb/api.py`).

## Run

```bash
uv run uvicorn kb.api:app --port 8001                   # the service
uv run python -m kb.ingestion.pipeline --limit 20       # crawl a sample
uv run python -m kb.retrieval.cli index                 # chunk + embed what was crawled
uv run python -m kb.retrieval.cli search "how do I refund a member?"
uv run python -m kb.snapshot                            # write a seed snapshot
```

## Test

```bash
uv run pytest && uv run mypy && uv run ruff check .
```

## How a question is answered

1. **Search** (`kb/retrieval/search.py`) finds passages by keyword (BM25) and by meaning
   (local embeddings), merged by rank.
2. **Evidence gate** (`kb/answer/service.py`): if the best passage is too weak a match, it
   refuses and raises an escalation instead of guessing.
3. **Answer** (`kb/answer/llm.py`): Claude writes the reply from the top 6 passages, and the
   citations are checked in code.

## Folders

```
kb/
  api.py              HTTP API: the only way other services reach this one
  config.py           every setting, from environment variables (KB_*)
  claude.py           where Claude is reached (Amazon Bedrock or Anthropic's API)
  db.py               SQLite storage: crawl manifest, articles, images, chunks
  bootstrap.py        first start: seed from a snapshot instead of a 40-minute crawl
  refresh.py          daily refresh: crawl what changed, describe screenshots, re-index
  snapshot.py         write a self-contained copy of the knowledge base
  guard.py            public-demo limits: per-visitor rate limits, spending cap
  session.py          demo accounts (cookie shared by the three services)
  progress.py         live progress for slow requests
  speech.py           speech to text for the mic buttons (local Whisper)

  ingestion/          getting the help center in
    pipeline.py         the crawl job: sitemap -> what changed -> fetch -> parse -> store
    sitemap.py          parse the help-center sitemap, work out what changed
    sources.py          custom sitemaps people add: validation and safe reading
    fetcher.py          polite HTTP fetcher (few requests, gaps between them)
    parser.py           article page -> clean text + screenshots
    vision.py           describe screenshots so their content is searchable
    health.py           pages that stopped working
    catalog.py          crawl bookkeeping: what's due, time estimates

  retrieval/          finding the right passages
    chunker.py          split articles into passages
    embedder.py         text embeddings, computed locally
    indexer.py          keep the passage index in step with the articles
    search.py           hybrid keyword + meaning search
    rerank.py           optional second-stage ranking (local cross-encoder)
    rewrite.py          optional query rewriting into help-center wording
    shadow.py           build and test a candidate index beside the live one
    cli.py              command line: index / search

  answer/             turning passages into a reply
    service.py          question in, grounded answer out (gate, answer, citations)
    llm.py              the Claude answering call and its prices
    local.py            the same with a local open-weight model (Ollama)
    escalations.py      unanswered questions -> expert tasks -> new knowledge

  tuning/             the agent maintaining its own rewrite prompt
    registry.py         prompts as versioned data
    writer.py           writes a new prompt from failures
    robustness.py       judges a prompt: does every wording behave the same?

  prompts/            the prompts, versioned (answer_v1.md, rewrite_v1.md, ...)
  ui/index.html       the web page

evaluation/           measuring quality, speed and cost (see evaluation/README.md)
tests/                unit tests, with scripted fake models (no API key needed)
data/                 local database, models and logs (not in git)
```

Release history: [RELEASE_NOTES.md](RELEASE_NOTES.md). Measured results:
[../EVALS.md](../EVALS.md).
