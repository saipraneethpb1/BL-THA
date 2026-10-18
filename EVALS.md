# Evals

Every number here comes from a saved run in `*/evaluation/results/`. Each harness is a dry run by default and only spends money with `--run`. The models compared are Claude Haiku 5.5, Sonnet 5.5 and Opus 5.5. Costs are the API list prices for the tokens actually used.

> **TODO (Sai):** fill the marked gaps after the remaining runs, then delete this line.

## Task 1: Knowledge Brain

### Retrieval: 42 questions, gold article per question

35 paraphrased customer questions with gold articles. 7 more were added *before* tuning: 2 that I had diagnosed (flagged, and reported separately below) and 5 fresh ones. "In context @6" means the gold article is among the 6 passages the answer model actually sees.

| Configuration | top-1 | top-5 | MRR@10 | in context @6 | latency p50 / p95 |
|---|---|---|---|---|---|
| keyword (BM25) only | 40% | 71% | 0.54 | 71% | 6 / 11 ms |
| vector only | 55% | 83% | 0.66 | 86% | 11 / 13 ms |
| hybrid, keyword ×0.3 | 55% | 86% | 0.66 | 86% | 21 / 35 ms |
| + changelogs ×0.5, max 2 passages/article | 55% | 88% | 0.67 | 86% | 19 / 29 ms |
| **+ query rewriting (Haiku), best-query merge** | **69%** | **98%** | **0.82** | **98%** | 1.3 / 1.8 s |

- **Changelog down-weighting.** 632 of the 1,537 articles are release notes. Halving their weight cut changelog passages in the model's context from 6% to 2%. ×0.25 was rejected: it broke questions that really are about release notes.
- **Query rewriting.** Haiku turns the question into 2–3 searches in help-center wording, for $0.000023 per question (42 calls cost $0.001 in total). The first version summed scores across searches and reached 93% top-5. Switching to "best search wins" reached 98%. That fix came from diagnosing a question that is *not* in this set ("hide my site while we build it" → Staging Mode), so this set wasn't tuned on.
- **Cost:** about 1.3 s extra per question. **Still missing:** R33 ("Froala strips my HTML").
- **After indexing the 5,040 screenshot descriptions** (re-run on the new index):
  - Vector-only search improved from 83% to 90% top-5.
  - Hybrid without rewriting dropped from 88% to 83%.
  - Rewriting scored 93% in one run and 98% in the next, with identical settings: Haiku simply wrote slightly different rewrites.
  - **So on 42 questions, differences under about 5 points (2 questions) are noise.** I kept keyword ×0.3. Lower weights reached 100% top-5 once, within the same noise, and that's not a reason to change.

### Screenshot descriptions: 26 real screenshots, 66 labels read by eye

A label counts as found when the stored description contains it (ignoring case and punctuation). V25 and V26 are the image-only answers: Chrome's "Always use secure connections" and Firefox's "Don't enable HTTPS-Only Mode".

| Model | labels found | images fully read | image-only answers | errors | $/image | $ for all 5,040 | p50 / p95 |
|---|---|---|---|---|---|---|---|
| Haiku 5.5 (first run) | 88% | 73% | 2/2 | 1 | $0.00030 | $1.49 | 2.5 / 4.3 s |
| **Haiku 5.5 (after fix)** | **92%** | **81%** | **2/2** | **0** | **$0.00030** | **$1.51** | 2.0 / 2.7 s |
| Sonnet 5.5 | 92% | 85% | 2/2 | 0 | $0.00584 | $29.44 | 2.7 / 5.8 s |

- **Fix found by this eval:** the schema capped visible labels at 12. Haiku listed 14 on one screenshot, and the whole description was rejected. Labels are now trimmed instead.
- **Decision: Haiku.** It finds the same labels as Sonnet for one-twentieth of the price. Sonnet reads 4% more images completely, which isn't worth $28.
- **TODO:** actual cost and time of the full run over the 5,040 screenshots.

### Answers: 32 questions, 3 models

This was run after all 5,040 screenshots were described, with production retrieval (query rewriting on). 22 questions are answerable (2 of them only from a screenshot) and 10 must be refused (not covered, off-topic, prompt injection).

| Model | pass | answers when it should | cites gold | facts right | abstains when it should | forbidden hits* | $/question | p50 / p95 |
|---|---|---|---|---|---|---|---|---|
| Haiku 5.5 | 69% | 70% | 100% | 88% | 100% | 1 | $0.0005 | 3.0 / 4.9 s |
| Sonnet 5.5 | 81% | 78% | 100% | 100% | 100% | 1 | $0.011 | 2.9 / 4.6 s |
| Opus 5.5 | **88%** | **87%** | 100% | 100% | 89% | 0 | $0.023 | 4.6 / 6.3 s |

- **Every model cited the right article** whenever it answered, and both image-only questions are now answered correctly from the screenshot descriptions.
- **Most failures are over-caution, not wrong facts.** The model labels a correct answer "partial", which sends it to a human. Sonnet's A03 is an example: it correctly said forwarders were removed in June 2024, but labelled the answer partial.
- **\*Grader flaw:** both "forbidden" hits are refusals that quote the phrase ("the help center does not say refunds are *always approved*"). The literal check can't tell a quote from a claim. I'm reporting the raw numbers and leaving the grader unchanged after seeing results.
- **Opus's one abstention "fail" (A30, a prompt injection)** declined to confirm the false claim and cited what the help center really says about refunds. That is arguably better than the flat refusal the case expected.
- **Decision: Opus 5.5.** It passes 7 more points than Sonnet (88% vs 81%) at twice the cost: $0.023 vs $0.011 per question, still about 2 cents. It is 1.6 s slower. The Inbox Agent and Scope Builder both depend on these answers, and every needless "partial" becomes a human escalation, so the extra cent is worth it. Tasks 2 and 3 were evaluated against Sonnet answers: their numbers should be re-run when budget allows.

## Task 2: Inbox Agent

### Drafting: 10 dev emails (first sample), Haiku classifying, Opus judging

| Drafter | category | urgency | unsafe auto-sends | over-cautious | must-include met | must-not violations | tone ok | $/email | p50 / p95 |
|---|---|---|---|---|---|---|---|---|---|
| **Sonnet 5.5** | 90% | 80% | **0** | 4 | 70% | **0** | 100% | **$0.026** | 11.3 / 13.9 s |
| Opus 5.5 | 90% | 70% | 0 | 4 | 75% | 1 | 100% | $0.041 | 15.3 / 18.3 s |

- **Cost per email** includes the Knowledge Brain lookups. The judge cost $0.175 for 20 drafts and is not included.
- **"Unsafe auto-send"** means an email labelled "needs a human" that the agent would have sent on its own: 0 for both models.
- **Decision: Sonnet.** Opus met 5 more points of the checklist, but broke one must-not rule and costs 58% more.
- **Failures:**
  - E002 ("my member wants a refund, how do I give it back") was classified as a refund request instead of a how-to. It's a deliberate trap, and it fails safe: refunds always go to a human.
  - E001 is a label problem: the agent cited a newer article (1800×600) that the hand-written checklist didn't know about.
  - E005 is a Task 1 depth problem: the Knowledge Brain returned a partial answer and missed a menu path that is in the article.
- **Fix found by this eval:** an unknown secondary category from Haiku crashed the email. Optional fields are now cleaned in code.

**TODO (Sai):** after writing the 20 test emails, run `npm run eval -- --split test --run` and report those numbers as the headline. The dev numbers above are from emails used during building.

## Task 3: Meeting to Scope

### Feasibility: 24 requirements, real Knowledge Brain

| Model | accuracy | missed savings | false promises | $/requirement (model only) | p50 / p95 (incl. KB) |
|---|---|---|---|---|---|
| **Haiku 5.5** | **96%** | 0 | 0 | **$0.00023** | 6.8 / 10.9 s |
| Sonnet 5.5 | 96% | 0 | 0 | $0.00465 | 5.5 / 7.9 s |

- A **missed saving** is a built-in feature quoted as custom work (the client overpays). A **false promise** is custom work called built-in (we under-quote). Neither model made either mistake.
- The trap case (F17: a native app, where the product supports only WebView apps) was handled correctly.
- Both models missed F08 for the same reason: the Knowledge Brain returned a partial answer, so they called it "existing, with a limitation". The expected answer was left unchanged after seeing this.
- **Decision: Haiku.** Same accuracy, one-twentieth of the price.

### Transcripts

**TODO (Sai):** record the 3 meetings, write the gold files, run `uv run python -m evaluation.run_transcript_eval --run`, and report requirement recall, noise, quote verification and change detection per analyst model.

## Reliability and safety

- **Bugs found by these evals, all fixed and tested:**
  - a vision schema limit that discarded whole descriptions;
  - classifier output that crashed on a stray category;
  - the merge rule that buried the best rewrite's results.
- **Retries:** every API call uses SDK retries with backoff. Every agent step whose output fails a code check is retried once with the problems listed.
- **Safety in the samples:** 0 unsafe auto-sends, 0 false promises.
- **Total spent so far:** about **$X** of the $10 budget. **TODO:** read it from the Console usage page.
