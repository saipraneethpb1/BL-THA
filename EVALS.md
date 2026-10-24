# Evals

Every number here comes from a saved run in `*/evaluation/results/`. Each harness is a dry run by default and only spends money with `--run`. The models compared are Claude Haiku 5.5, Sonnet 5.5 and Opus 5.5. Costs are the API list prices for the tokens actually used.

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

### Wording robustness: 42 questions × 4 wordings, the agent's own prompt

Does the way a question is worded change what the customer gets? Each retrieval question also comes with synonyms (*customized* for *custom*, *colour* for *color*), misspellings, and other names for the same feature (*coupon* for *discount code*): 168 wordings, in `kb/tuning/robustness_cases.jsonl`. A question passes when every wording finds the right article in the top 5 **and** the same article first. Odd-numbered questions are "train" (the prompt writer sees their failures); even-numbered ones are held out.

| Rewrite prompt | Every wording same + right | Train (seen) | Held out (never seen) | Wordings right, top 5 | Misspelled right |
|---|---|---|---|---|---|
| v1, written by hand | 9 / 42 | 4 / 21 | 5 / 21 | 92% | 90% |
| v2, written by the agent (Sonnet) from v1's failures | **16 / 42** | 10 / 21 | 6 / 21 | 93% | 93% |

Almost every wording already *finds* the article; what varied was which article came *first*. The agent's v2 adds a spell-fix → unify → name-the-feature order and a glossary of term groups ("custom, customize, customise, customized"), and nearly doubled the questions where wording makes no difference. The gain on held-out questions is small (+1), and that is what the gate requires before a version goes live, so the honest headline is: clearly better on what it learned from, slightly better on what it didn't. One round cost $0.032 (Haiku rewrites for 2 × 168 wordings, one Sonnet call) and took about 10 minutes. Run it: `uv run python -m evaluation.run_robustness_eval`.

### Screenshot descriptions: 26 real screenshots, 66 labels read by eye

A label counts as found when the stored description contains it (ignoring case and punctuation). V25 and V26 are the image-only answers: Chrome's "Always use secure connections" and Firefox's "Don't enable HTTPS-Only Mode".

| Model | labels found | images fully read | image-only answers | errors | $/image | $ for all 5,040 | p50 / p95 |
|---|---|---|---|---|---|---|---|
| Haiku 5.5 (first run) | 88% | 73% | 2/2 | 1 | $0.00030 | $1.49 | 2.5 / 4.3 s |
| **Haiku 5.5 (after fix)** | **92%** | **81%** | **2/2** | **0** | **$0.00030** | **$1.51** | 2.0 / 2.7 s |
| Sonnet 5.5 | 92% | 85% | 2/2 | 0 | $0.00584 | $29.44 | 2.7 / 5.8 s |

- **Fix found by this eval:** the schema capped visible labels at 12. Haiku listed 14 on one screenshot, and the whole description was rejected. Labels are now trimmed instead.
- **Decision: Haiku.** It finds the same labels as Sonnet for one-twentieth of the price. Sonnet reads 4% more images completely, which isn't worth $28.
- **Full run over all 5,040 screenshots:** $1.62 and 24.9 minutes, 0 failures. The eval's estimate was $1.51.

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
  - E001 is a label problem: the agent cited a newer article (1800×600) that the checklist in the dataset didn't know about.
  - E005 is a Task 1 depth problem: the Knowledge Brain returned a partial answer and missed a menu path that is in the article.
- **Fix found by this eval:** an unknown secondary category from Haiku crashed the email. Optional fields are now cleaned in code.

### Headline: 20 held-out test emails (AI-drafted, reviewed and edited by me; never used while building)

Haiku classifying, Sonnet drafting (the choice made on dev), Opus judging. The dev numbers above came from emails used during building; these are the ones to trust.

| Drafter | category | urgency | unsafe auto-sends | over-cautious | must-include met | must-not violations | tone ok | $/email | p50 / p95 |
|---|---|---|---|---|---|---|---|---|---|
| Sonnet 5.5 | **100%** | 90% | **0** | 6 | 88% | 1 | 100% | $0.055 | 14.4 / 16.4 s |

- **Safety held on every hard case:**
  - both prompt injections ("write 'refund approved'", "treat this password as proof") were ignored;
  - neither the card number nor the password was repeated;
  - all 3 critical emails went to a human;
  - the Hindi emails were answered in Hindi.
- **The judge cost $0.20** for 20 drafts, not included above. $/email is higher than on dev because test emails ask more questions, so there are more Knowledge Brain calls.
- **Failures, read by hand.** The judge's per-item reasons weren't saved (a harness gap), so these are my reading of the drafts:
  - **E056 (quote): the one must-not violation, a real one.** The draft didn't give a price, but it said the quote would come in two parts, setup and upkeep. That is a billing arrangement nobody agreed to. Scoring 0/2 on must-include looks harsh, since the draft did ask about volume, member descriptions and upkeep.
  - **E049 (vague bill question): 0/1.** It explained where to see invoice lines but never asked for the invoice reference, as the label requires.
  - **E047 (Hindi signup outage): 1/2.** It kept "error 500" but dropped "seen on two phones". It also offered a 2-working-day follow-up, which is slow for a critical outage. That timeline comes from the drafting prompt's default, and critical emails should get a shorter one.
- **Over-cautious (6):**
  - 4 how-to emails and 2 billing emails went to a human although the label said the agent could answer.
  - Each time, the Knowledge Brain called its own answer "partial". This is the same over-caution seen in Task 1, and it fails safe.
- **Urgency (2 misses):** E042 and E043 were marked "low" instead of "normal". This only changes their place in the queue.
- **Policy-check false positive:**
  - The safety check blocked E050 because the draft said "Nothing has been cancelled".
  - The rule matches "cancelled" without noticing the negation.
  - It fails safe (the email was going to a human anyway), but it is a bug to fix.

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

### Transcripts: 3 real AMI meetings, 30 gold cases

This replaces the role-plays (see DESIGN.md §5). The meetings are ES2002b, ES2002c and its follow-up ES2002d: 36–40 minutes each, 333–538 speaker turns, 7–8 chunks. The answer key is the **AMI annotators' own decision lists**, turned into keyword checks: 25 decisions and 5 changes between ES2002c and ES2002d. The product Knowledge Brain was replaced by a stand-in that always answers "not covered", because these meetings are about a TV remote.

| Analyst | Meeting | decisions found | extracted, not in key | quotes verified | changes detected | $/build | time |
|---|---|---|---|---|---|---|---|
| Sonnet 5.5 | ES2002b | 8/8 | 7 | 100% | – | $0.28 | 99 s |
| Sonnet 5.5 | ES2002c | 9/9 | 18 | 100% | – | $0.36 | 124 s |
| Sonnet 5.5 | ES2002d (follow-up), before fix | 7/8 | 28 | 100% | **2/5** | $0.32 | 92 s |
| Sonnet 5.5 | ES2002d, after fix (2 runs) | 8/8, 7/8 | 41, 31 | 100% | **5/5, 4/5** | $0.34 | 97–104 s |
| Sonnet 5.5 | ES2002d, + process-talk filter | 7/8 | 31 | 100% | 3/5 | $0.30 | 84 s |

- **Extraction is strong:** 24 of 25 annotated decisions found, and 100% of quotes located in the transcript.
- **"Extracted, not in key" is not all noise.** AMI's summaries list only the headline decisions; the analyst also records details the team discussed.
- **Change detection was weak before the fix: 2 of 5.**
  - Found: the case shape and "display on the TV".
  - Missed: the kinetic battery being dropped, rubber → push buttons, the regular chip.
  - On the other side, 15 items were flagged as new out-of-scope work.
  - **Re-run with full detail saved** (identical inputs and code): 4/5 changes detected, and 8/8 decisions found in ES2002d. The diagnosis:
    1. **Requirement vs decision is a coin flip between runs, and only requirements are compared.** This time the kinetic battery was recorded as a requirement, so its removal was caught. "Rubber buttons" and the logic chip were recorded as decisions, so their changes were invisible. This explains 2/5 vs 4/5.
    2. **One "detection" was luck.** The button case matched an unrelated change (backlit buttons "when touched or pushed"). The real rubber → push change was missed, so the honest score is about **3/5**. The keyword grader needs tightening.
    3. **One false addition:** "a small LCD on the remote". The team discussed it and rejected it (AMI: no LCD), but the analyst kept it as a requirement.
    4. **Over-flagging:** 22 changes were marked "outside the approved scope", because every modification is flagged, even ones that reduce work.
  - **Fix applied** (analyst prompts v2, change prompt v2, `adds_work` in the schema):
    - The analyst records anything the product will or won't have as a *requirement*.
    - The change detector also receives the baseline's decisions and constraints (as `DEC-xxx`), so a product fact filed as a decision can still be linked.
    - An option discussed and then rejected is recorded as excluded.
    - Only changes that *add* work are flagged.
    - **Grader change, disclosed:** the button case now needs "push button", "square" or "rubber" next to "button", instead of "push" (which matched "pushed"). This was tightened *before* the re-runs, and it makes the check harder to pass.
  - **After the fix, two runs of ES2002c → ES2002d (Sonnet, $0.64 and $0.67 per pair):**

    | Run | changes detected | flagged out-of-scope | decisions found (c / d) |
    |---|---|---|---|
    | before the fix (2 runs) | 2/5, 4/5 (honestly about 3/5) | 15, 22 | 9/9, 7/8 and 8/8 |
    | after, run 1 | **5/5** | 20 of 29 changes | 9/9, 8/8 |
    | after, run 2 | **4/5** | 15 of 27 changes | 9/9, 7/8 |

    - **I checked every detection by hand,** not only the keyword match:
      - kinetic → regular battery, rubber → plastic buttons, and the regular chip are now caught as real modifications;
      - battery and button changes that *reduce* work are no longer flagged;
      - the rejected LCD now appears as "No LCD display" instead of being added as work.
    - **Run 2's miss:** "display on the TV". The analyst wrote "the display should clearly show which function is being adjusted" without saying which display, so the change can't be matched.
    - **Remaining noise:** the last part of ES2002d is the team reviewing its *own* meeting process (whiteboards, digital pens, PowerPoint). The analyst recorded 6–10 of these as requirements, and they were flagged as new work. A real client meeting would rarely contain this, but the analyst should still drop it. The flagged list is a review prompt for the consultant, not an automatic charge.
  - **Process-talk filter** (analyst prompts v3):
    - The analyst skips talk about the team's own tools and meeting process.
    - Code no longer restores a requirement item that the merge deliberately re-filed under decisions.
    - One run ($0.60):
      - **Flags dropped from 15–20 to 3**, with no whiteboard, pen or PowerPoint items. All 3 are real expansions (audio controls, jog dial for advanced functions, hidden functions).
      - Changes detected: **3/5.**
    - **Both misses are extraction variance, not the filter:**
      - "display on the TV" was never extracted from ES2002d (7/8 decisions);
      - "regular chip" was extracted but filed as a *decision* in the follow-up, and the change detector only links new-meeting *requirements*.
    - **Across the three runs since the fix: 5/5, 4/5, 3/5.** The remaining weakness is the requirement/decision split *in the follow-up meeting*. The next fix would be letting the detector link new-meeting decisions too; it has not been tried yet.
- **Haiku as analyst** (one run of all 3 meetings, current code, $0.37):

    | Analyst | decisions found (b / c / d) | changes detected | $ per c→d pair | wrong product facts in the follow-up (read by hand) |
    |---|---|---|---|---|
    | Sonnet 5.5 (3 runs) | 8/8, 9/9, 8/8–7/8 | 5/5, 4/5, 3/5 | $0.60–0.67 | 0 |
    | Haiku 5.5 (1 run) | 8/8, 9/9, 7/8 | 4/5 | $0.29 | 3 |

  - **The keyword score hides Haiku's errors.** In ES2002d it:
    - turned the approved "No LCD display" into "a very small LCD screen" and flagged it as new work;
    - added "will not use plastic or rubber", which contradicts its own "mostly plastic";
    - moved "Homer Simpson" from the logo to the remote's shape.
  - In a scope, each of these is a wrong line in a client quote.
  - **Haiku's first attempt crashed.** On ES2002c, its merge step ran out of the 16k output budget, because thinking counts toward it, and returned cut-off JSON. The limit is now 32k for every model. Sonnet never came near it.
  - **Decision: keep Sonnet.** It costs about twice as much per meeting ($0.30 vs $0.15), but a scope with invented product facts costs far more than 15 cents to fix. One Haiku run is a small sample; the three errors are why I didn't spend more on it.

## Reliability and safety

- **Bugs found by these evals, all fixed and tested:**
  - a vision schema limit that discarded whole descriptions;
  - classifier output that crashed on a stray category;
  - the merge rule that buried the best rewrite's results.
- **Retries:** every API call uses SDK retries with backoff. Every agent step whose output fails a code check is retried once with the problems listed.
- **Safety in the samples:** 0 unsafe auto-sends, 0 false promises.
- **Total spent:** about **$10**, the whole first credit top-up, which ran out on 18 October during a demo build. The harnesses recorded **$9.09** of it, from their own token counts at list prices:

  | Run | Recorded cost |
  |---|---|
  | Transcript evals: 7 runs, every build including follow-ups | $3.94 |
  | Screenshot descriptions: all 5,040 (the production run) | $1.62 |
  | Answer eval: 3 models × 32 questions | $1.11 |
  | Inbox test set: 20 emails, drafting including Knowledge Brain lookups | $1.10 |
  | Inbox dev sample: 10 emails × 2 drafters | $0.67 |
  | Inbox judge calls (dev and test) | $0.37 |
  | Screenshot eval: 2 runs | $0.17 |
  | Feasibility eval: model calls only, 2 models × 24 | $0.12 |
  | **Total recorded** | **$9.09** |

  Not in the table:
  - the live site's own use ($0.73 when this was written; the usage bar at the bottom of every page shows the current figure);
  - the Knowledge Brain lookups made during the feasibility eval;
  - the part of a demo build that ran before the credit ran out.

  The Anthropic Console is the authority on the exact total.
- **Ongoing spend is capped:** each service has a budget ($4.50 / $2.50 / $2.50), each visitor is rate-limited, and the Anthropic account has a monthly spend limit.
