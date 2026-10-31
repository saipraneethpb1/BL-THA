# Handoff: Knowledge Brain changes

Written from a code and eval review of Task 1 (30 Oct 2026 numbers in `EVALS.md`). The other two services already call the brain over HTTP. The work below is what has to change in the brain, and the two contract updates those services need so a better status actually changes their behaviour.

Do this in the order below. Later items depend on the status the earlier items produce. Re-measure the live stack before quoting a new headline number.

## Current baseline (do not treat these as the live system)

| What people quote | What is configured or stored |
|---|---|
| Opus 5.5 answers, 88% on 32 questions, with the Haiku rewriter | `KB_ANSWER_MODEL` is `claude-opus-4-6`, effort `low`. 4.6 has not been through that eval (`kb/config.py`). |
| Gate 0.66, wrongly answered 36% dev / 56% held-out | Calibrated with rewriting off. Live default is `query_rewrite=True`. |
| 12% chunk overlap "shipped" | Code default in `kb/retrieval/chunker.py`. The live `knowledge.db` was never fully reindexed. A refresh rechunks only changed pages. |
| Recall@5 86% dev / 89% held-out | Free stack, bge-small, no rewriter, no reranker. Production search still rewrites when a key is present. |

Dev recall@5 on ~81 answerable cases has a 95% interval of about ±8 points. A change counts only when `evaluation/compare.py` says the paired interval excludes zero, on held-out, after the live index matches the code.

## Leave these alone

- SQLite plus the in-memory matrix in `kb/retrieval/search.py`. About 8k vectors do not need another database.
- The crawl, snapshot, and publish-on-finish path (`kb/ingestion/`, `kb/retrieval/indexer.py`, `kb/snapshot.py`).
- The expert queue as the place unanswered questions go (`kb/answer/escalations.py`). New answers must pass through the claim check and the ledger once those exist.
- Claude as the writer until a local model beats it on the claim-checked answer eval. llama3.2 at 50% pass and 23–47 s is a measured reject (`answers_20261030T095952Z.json`).
- Another round of the self-rewritten query prompt (`kb/tuning/`). Held-out it moved 5/21 questions to 6/21, and one live round was rejected because findability fell. A fixed glossary plus query condensation is the replacement.
- A knowledge graph of all 1,537 articles. The ledger below is the useful subset.

## 1. Claim check owns `status`

**Why.** `kb/answer/service.py` keeps the model's `answered` / `partial` / `insufficient_evidence` as long as an answered reply cites a real passage number. A correct answer the model calls `partial` opens an expert task. The Inbox Agent treats every non-`answered` lookup as a knowledge gap (`task2-inbox-agent/src/pipeline.ts`, `knowledgeGap`). On the 20 held-out emails, 6 answerable threads went to a human for that reason. The Scope Builder only downgrades "already exists" when the brain says `insufficient_evidence` or returns no sources (`task3-meeting-to-scope/scope/agents/feasibility.py`). A `partial` with sources became "existing, with a limitation" on F08.

**Where.** New module `kb/answer/claims.py`, called from `answer_question` after citation numbers are checked. Prompt stays `kb/prompts/answer_v1.md`. The model still drafts; code overwrites `status`.

**Behaviour.**

- Split the draft into claims.
- A claim stands when a local entailment model says the cited passage supports it. Use a local cross-encoder or a DeBERTa NLI model under `data/models/`. Same pattern as `kb/retrieval/rerank.py`: lazy load, ONNX, no API key.
- Every claim supported → `answered`.
- Some claims supported → `partial`. Response includes the supported answer and `missing_info` for the rest.
- No claim supported, or no valid citation → `insufficient_evidence`, answer replaced with the existing not-covered sentence.
- `needs_human` stays `status != "answered"`.
- Record claim counts on `query_log` (new columns: `claims`, `claims_supported`).

**Done when.**

- Unit tests with a fake entailment model cover all three outcomes, including a draft that says `partial` while every claim is supported.
- The 32-question answer eval (`evaluation/run_answer_eval.py`) reports a second grade: facts by entailment, and how often the model's own status was overridden. The substring `facts` check stays as a smoke grader.
- Re-run the inbox held-out set and the 24 feasibility cases after item 7. Target: the 6 over-cautious emails and F08 move because the status changed, with zero new must-not violations and zero new false "already exists".

## 2. Feature ledger

**Why.** Passage ranking cannot represent "email forwarders were removed in June 2024" or "October 2024, not October 2020". Changelog weight 0.5 still left 4 of 6 Stripe passages as release notes. The detector is the title regex in `is_changelog` (`kb/retrieval/search.py`).

**Where.** New tables in `kb/db.py`: `features`, `feature_events`. New job `kb/ingestion/ledger.py`, invoked at the end of a refresh (`kb/refresh.py`) for articles whose `indexed_hash` changed and for every changelog article. Read path in `answer_question`: if the question is about whether a feature exists or where a setting lives, retrieve the ledger row first and pass it to the model as a passage with `source=ledger`.

**Row.** Feature name, status (`available` / `removed` / `changed`), as-of date, menu path, source article id, source chunk id. One feature has many events. The latest event wins.

**Behaviour.**

- Changelogs write events. How-to articles fill the menu path and stay the source for steps.
- Two cited sources that disagree (newer event vs older how-to) produce status `conflict`, both citations, and an escalation. Add `conflict` to the status enum in `kb/answer/llm.py`, the `/query` schema in `kb/api.py`, the inbox client (`task2-inbox-agent/src/knowledge-client/client.ts`), and the scope client (`task3-meeting-to-scope/scope/knowledge.py`).
- `conflict` is a knowledge gap for auto-send, same as `insufficient_evidence`.
- Expert answers do not overwrite a newer help-center event unless the expert record has `supersedes=1`.

**Done when.**

- A fixture based on the forwarder articles answers "removed in June 2024" with status `answered` and cites the removal event.
- A date query for "October 2024 update" returns the 2024 article ahead of the 2020 article on a small labelled set (add these cases to `evaluation/cases/retrieval.jsonl`).
- The Stripe troubleshooting question has at most one changelog passage in the six shown to the model, unless the gold article itself is a changelog.

## 3. One live stack, then new retrieval numbers

**Why.** Overlap, the gate, the rewriter, and the answer model are three different stacks. The next bake-off has to be about the database that actually serves traffic.

**Where.** `kb/retrieval/indexer.py`, `evaluation/run_retrieval_eval.py`, `evaluation/baselines/smoke.json`.

**Behaviour.**

- Run `uv run python -m kb.retrieval.cli index --force` so every article uses `ChunkingConfig` (12% overlap). Confirm `index_meta` records that chunking.
- `VectorIndex.refresh` loads only rows whose `embedding_model` and `embedding_dim` match the live embedder. Expert inserts in `add_expert_answer` must set `embedding_model`, `embedding_dim`, `chunk_key`, and `is_active`.
- Add a startup check: if active article chunks have more than one `embedding_model`, refuse to serve `/query` with a clear log line.
- Re-run `evaluation/run_retrieval_eval.py` on dev and held-out for the config that is actually live, including `production+rewrite` if Haiku rewriting stays on. Write the result into `EVALS.md`. Update `evaluation/baselines/smoke.json` only after that reindex is intentional.
- Recalibrate `KB_MIN_EVIDENCE_SCORE` on that same run. The gate must be fit with the rewriter in the state production uses. Target while item 4 is unfinished: wrongly refused under 2% of answerable dev cases, then the lowest wrongly-answered the sweep allows. Publish the held-out pair beside it.

**Done when.** The smoke baseline's chunk hash equals the live index, and `EVALS.md` names one stack: embedder, chunker, rewriter, gate, answer model, effort.

## 4. Retrieval quality

Do these as separate shadow-index runs (`kb/retrieval/shadow.py`). Promote one change at a time, only when the paired held-out interval excludes zero, or when a named failure mode drops (follow-ups, troubleshooting, date queries) with no held-out recall loss.

### 4a. Standalone follow-up query

**Why.** `with_context` in `kb/answer/service.py` embeds up to six prior turns, answers cut at 3,000 characters. The retrieval eval does the same wrap with the assistant text empty (`evaluation/run_retrieval_eval.py`, `search_text`). Dev follow-up recall@5 is 33% on 3 cases.

**Where.** New `kb/retrieval/condense.py`. `query` and `query/stream` in `kb/api.py` condense first. Search, rewrite, and the gate see only the standalone question. The condenser output is stored on `query_log`.

**Cases.** Extend the follow-up rows in `evaluation/cases/retrieval.jsonl` so `history` includes the previous answer text, not only the previous question. Add enough that the category has at least 15 cases. Keep the old empty-assistant rows as a regression slice.

**Done when.** Held-out follow-up recall@5 is at least the overall recall@5 of the same run. A unit test shows the string passed to `Searcher.search` contains no "Conversation context" wrapper.

### 4b. Contextual chunk prefix

**Why.** Paraphrases with no shared words ("a code from their phone", "Froala strips my HTML") miss. bge-small also truncates at 512 tokens, while chunks are cut at 1,500 characters plus an inlined screenshot caption.

**Where.** `kb/retrieval/chunker.py` and `kb/retrieval/indexer.py`.

**Behaviour.**

- At index time, prepend one sentence: article title, section, and what question the chunk answers. Generate that sentence with the local answer model or a small local model, and cache it on the chunk. Embed the prefixed text. Show the model the unprefixed section text at answer time.
- Cap the embedded text at the model's token limit (512 for bge-small). Put UI labels in their own short chunks (item 6) so a caption cannot push the procedure out of the window.
- Store the prefix in a column. A chunk whose source text is unchanged keeps its vector only when the prefix is unchanged too.

**Done when.** A shadow index beats the live index on held-out recall@5 or on the named paraphrase misses (R33 and the two-factor wording), with latency still interactive on 2 vCPUs. Record p50 and p95.

### 4c. Embedder bake-off

**Where.** Models already registered in `kb/retrieval/embedder.py`: `intfloat/e5-large-v2`, its int8 build, `BAAI/bge-m3`, its int8 build.

**Behaviour.** For each candidate, `kb.retrieval.shadow build`, then `run_retrieval_eval.py --db data/shadow/<name>.db` with a fresh gate sweep. int8 is the one that has to win for the 2-vCPU box. Recalibrate the gate per model. Cosine scales differ.

**Done when.** `EVALS.md` has the table, and the live model changes only if held-out recall@5 or the gate's wrongly-answered rate improves with the interval excluding zero.

### 4d. Keyword channel and the gate score

**Why.** `fts_query` ORs every non-stopword (`kb/retrieval/search.py`). Fusion can place a weaker semantic hit first when it also matches keywords. `answer_question` then gates on that hit's cosine (`hits[0].score`), so a stronger passage further down never gets to open the gate.

**Behaviour.**

- Two keyword queries: a phrase query for setting-like spans, and the current OR query only as a second list with a lower fusion weight.
- The score the gate reads is the best rerank score among the passages that will be shown to the model (item 4e). Until that reranker is on, the gate reads the best cosine in the fused list, not the cosine of rank 1.
- A hit that appears only in the keyword list, with no vector score, cannot by itself pass the gate.

**Done when.** A regression test has a rank-1 keyword-boosted passage under the threshold and a rank-2 passage over it, and the question is answered. Held-out wrongly-answered falls relative to the item 3 baseline.

### 4e. Reranker on the full passage

**Why.** MiniLM-L6 on the first 1,000 characters did not move held-out recall and cost about 1 s on a laptop, several seconds on 2 vCPUs (`EVALS.md` Phase 2). Truncation drops the menu path.

**Where.** `kb/retrieval/rerank.py`. Default stays off until the shadow run wins. Candidate models: `BAAI/bge-reranker-v2-m3` or `jina-reranker-v2`, depth 12, full passage text, scores cached by `(normalized question, chunk_id)`.

**Behaviour.** Reorder the context the model sees. The evidence gate uses this score. Fit the threshold on dev with the rewriter in its production state, then report held-out.

**Done when.** Held-out "in context @6" or wrongly-answered improves with the paired interval excluding zero, and p95 on the target machine stays under 2 s. If it does not, leave `KB_RERANKER_MODEL` unset and write the negative result into `EVALS.md`.

### 4f. Parent section, and a cap by section

**Why.** The model sees at most two chunks of an article (`max_per_article=2`), chosen after fusion. Inbox E005 missed a menu path that was in the article. Category and folder on `articles` are never used.

**Behaviour.**

- Search chunks. Pass the model the whole parent section (the level-2 heading already stored as `parent_section`), with the matched chunk marked.
- Diversity cap is one section per article in the first pass, then a second section only if fewer than six sections were found.
- Add a small boost when `articles.category` or `folder` shares a rare term with the question. Fit the boost on dev. Drop it if held-out recall does not move.

**Done when.** E005's menu path is inside the text the answer model receives, checked with a fixture. Held-out recall@5 does not fall.

### 4g. Two-part questions

**Why.** One multi-part case in six misses a part. There is a single search.

**Behaviour.** If the condenser (4a) returns two questions, search each, merge passages, and run one answer call. Cap at two searches. A question that does not split stays one search.

**Done when.** The existing multi-part retrieval cases are graded per part, and the miss rate on dev falls, with no extra model call on the single-question majority. Confirm with a counter in the retrieval eval output.

## 5. Screenshot text that can be cited

**Why.** Haiku finds 92% of labels on 26 images (`EVALS.md`). A label it invents is stored, inlined into the chunk, and later quoted. Reuse is by URL (`kb/ingestion/vision.py`), so a replaced file at the same URL keeps the old caption. Labels after the twelfth are dropped.

**Where.** `images` table: add `content_hash`. `kb/ingestion/vision.py`, `kb/retrieval/chunker.py`.

**Behaviour.**

- Hash the image bytes. Describe again when the hash changes.
- Keep a UI label only when a second pass (OCR or a second model read) returns the same string. Store rejected labels on the row for the vision eval.
- Index kept labels as short chunks linked to the parent section. Keep the long caption out of the procedure chunk's embedding.
- The answer prompt already says to mark image-only details. The claim check (item 1) must treat a label chunk as the only valid support for "the screenshot shows …".

**Done when.** The vision eval still finds at least 92% of the 66 labels, the invented-label count on that set is reported, and a changed byte hash forces a new description in a unit test.

## 6. Expert answers join the same checks

**Where.** `kb/answer/escalations.py`.

**Behaviour.**

- Set `embedding_model`, `embedding_dim`, `chunk_key`, `is_active` on the expert chunk (also required by item 3).
- Dedup open escalations by embedding similarity, not only by identical normalized text. Threshold fit so "refund a member" and "how do I give a member their money back" collapse, and two different features do not.
- An expert answer is retrieved with `source=expert`. It cannot outrank a newer ledger event unless `supersedes=1`.
- Run the claim check against the expert text the same way as against help-center passages.

**Done when.** Tests cover retract, supersede, and near-duplicate merge. A superseded official removal stays the answer when an older expert how-to is still active.

## 7. Downstream contract

Do this in the same change as item 1, so the new status is consumed.

**Inbox** (`task2-inbox-agent/src/pipeline.ts`, `src/feedback/autonomy.ts`).

- `knowledgeGap` is true when the brain is down, status is `insufficient_evidence` or `conflict`, or a `partial` answer has a non-empty `missing_info` that the draft relies on.
- A `partial` whose supported claims cover the email's question is usable evidence. The draft may use that text. Auto-send stays off if `missing_info` is non-empty.
- Draft prompt receives `answer` and `missing_info` as separate fields.

**Scope** (`scope/agents/feasibility.py`).

- `existing_configuration` requires status `answered` and at least one source.
- `existing_with_limitation` requires the limitation to be a ledger status or a sentence that survived the claim check. A model `partial` by itself is not a limitation.
- `conflict` and `insufficient_evidence` stay `needs_clarification`.

**Done when.** Inbox and feasibility evals are re-run and written into `EVALS.md`. F08 is either fixed or the case note says the brain still lacks the source. No new unsafe auto-send.

## 8. Evals to add before calling the work done

- One named live stack in `EVALS.md`, with dev and held-out side by side.
- Answer grade by entailment, plus the count of status overrides.
- Follow-up cases with real previous answers (≥15).
- Date and "removed feature" cases for the ledger.
- Product metric: share of inbox emails and feasibility rows that escalated only because status was `partial`, and that the claim check would have marked `answered`.
- Keep author-written retrieval cases as the regression set. Once `query_log` has inbox and scope questions, label a sample of those and report them separately. They are the real distribution.

The smoke workflow (`.github/workflows/knowledge-brain.yml`) should run on pull requests after the baseline is rewritten for the reindexed snapshot.

## Suggested change order

| Step | Ships | Depends on |
|---|---|---|
| A | Item 3: reindex, filter vectors by model, publish the live-stack eval, retune the gate | — |
| B | Item 1 and item 7: claim check and the inbox/scope status rules | A, so the gate and the checker are measured together |
| C | Item 4a, then 4d: condense follow-ups; gate on the best score | A |
| D | Item 2 and item 6: ledger and expert rules | B, because `conflict` uses the claim-check response shape |
| E | Item 4b, 4c, 4e, 4f: shadow indexes for prefix, embedder, reranker, parent section. Promote winners one at a time | A |
| F | Item 5 and item 4g: screenshot labels, two-part search | E for the chunker, B for citing a label |

Each step updates `EVALS.md` with the run file under `task1-knowledge-brain/evaluation/results/`. A step that does not move held-out, or that raises wrongly-answered, stays off and the negative result stays in the doc.
