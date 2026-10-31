# Knowledge Brain release notes

## Unreleased: Claude on Amazon Bedrock (30 Oct 2026)

- **Claude now runs on Amazon Bedrock by default** (`CLAUDE_PROVIDER=bedrock`, the
  `bedrock-runtime` InvokeModel API), for answering, query rewriting, screenshots and the
  prompt writer; the Inbox Agent and Scope Builder switch the same way. Set `AWS_REGION`,
  `CLAUDE_BEDROCK_PROFILE` (inference profile prefix: `global`, `au`, `us`, ...) and Bedrock
  credentials: a bearer token (`AWS_BEARER_TOKEN_BEDROCK`), AWS keys or a profile, or the
  EC2/ECS role. `CLAUDE_PROVIDER=anthropic` with `ANTHROPIC_API_KEY` uses Anthropic's API.
- **Default models are now the ones the Bedrock account has:** Opus 4.6 answers, Haiku 4.5
  rewrites queries and describes screenshots, Sonnet 4.6 writes prompts. A model setting is a
  bare name (`claude-opus-4-6`, turned into `au.anthropic.claude-opus-4-6-v1`) or a full
  Bedrock id. **Not yet measured:** the answer eval chose Opus 5.5 (88%); re-run
  `evaluation.run_answer_eval` on the 4.6 models before relying on the old numbers.
- Haiku 4.5 rejects the `effort` setting, so it is left out for that model; Opus and Sonnet
  4.6 keep it.
- The SDK is pinned below `anthropic` 2.0 (and `@anthropic-ai/sdk` below 1.0): from those
  releases it sends API version 2026-10-21, which Bedrock rejects for Claude 4.6 and older.
- Bedrock does not fetch image URLs, so screenshots are downloaded by the service (through the
  same private-address guard as the crawler, 5 MB cap, JPEG/PNG/GIF/WebP) and sent inline.
- Recorded spend uses Anthropic's list prices for these models (Opus 4.6 $5/$25, Sonnet 4.6
  $3/$15, Haiku 4.5 $1/$5 per million tokens), so budget caps keep working; actual charges
  are in AWS Billing, and regional profiles such as `au.` cost about 10% more than `global.`.

## Unreleased: revised RAG pipeline, phases 1 and 2 (30 Oct 2026)

Numbers and method: EVALS.md, "Revised RAG pipeline, free models only".

### Changed
- **Evidence gate 0.60 → 0.66** (`KB_MIN_EVIDENCE_SCORE`), recalibrated for bge-small on the
  new 147-case eval. Off-topic questions passing the gate: 59% → 36% on dev, 67% → 56% on
  held-out; answerable questions refused: 1% on dev, 0% on held-out.
- **Chunking: 12% overlap** between the pieces of a long section, and every chunk records
  its parent section. Takes effect for a page when it is next re-indexed; run
  `uv run python -m kb.retrieval.cli index --force` to switch the whole index at once.
- **Re-indexing publishes a page only when it is complete**: new chunks are written
  inactive and switched on in one step, so search never sees half a page. Chunks have
  stable ids, and unchanged chunks keep their vectors instead of being re-embedded.
- The database gains `chunks.chunk_key`, `parent_section`, `is_active`, `run_id`,
  `embedding_model`, `embedding_dim` and an `index_meta` table. Applied automatically at
  start-up; existing chunks stay active.

### Added
- **Free local answering and rewriting.** Set `KB_ANSWER_MODEL=ollama:<model>` and/or
  `KB_REWRITE_MODEL=ollama:<model>` to use an open-weight model through Ollama on the same
  machine: no API key, no per-call cost, and no question or passage leaves the server.
  Replies are validated against the same schema and retried, and the citation check is
  unchanged.
- Optional local reranker (`KB_RERANKER_MODEL`, off: no held-out gain), a skip rule in
  front of the query rewriter (`KB_REWRITE_SKIP_ABOVE=0.80`), and shadow indexes
  (`python -m kb.retrieval.shadow build ...`) so a new model or chunking can be measured
  beside the live index.
- Retrieval eval with dev / held-out / smoke splits, recall@5/10, MRR, nDCG, gate error
  rates, bootstrap intervals and paired comparisons, and a CI job that fails on a drop.

### Limits of the free stack
- **No high availability and no autoscaling.** The service, its SQLite database, the
  embedding model and any local answer model run on one EC2 instance. If the instance is
  down, the Knowledge Brain is down, and Tasks 2 and 3 treat it as "knowledge service
  unavailable". This cannot be had at zero cost; it needs a second instance and a shared
  database (phase 4, Postgres).
- **A local answer model is slower and weaker.** llama3.2 (3B) on a laptop CPU passed 50%
  of the 32-question answer eval (Opus: 88%), at 23 s p50 / 47 s p95 per answer, one
  answer at a time; a 2-vCPU server is slower still. It refused 8 of 9 questions it
  should refuse. The default answer model therefore stays Claude until a local model
  passes the eval.
- The Haiku query rewriter and the Claude answer model remain available, but only with an
  API key. The free configuration runs without them.
