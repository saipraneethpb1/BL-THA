# AI log

**Tool:** Claude Code (desktop app), one continuous conversation from the first read of the brief to the final docs. I used ChatGPT's Codex tool for only drafting the 20 test emails, and an edit pass on the dev emails (never used it or its models for development purposes, only used to create a data of emails so that anthropic models can work on it). The full chat export is submitted with the repo.

*These notes were drafted with Claude from that conversation, then read and edited by me.*

**How I worked:**
- **I made the product and judgement calls.** Among them:
  - the email categories and autonomy rules, and what to drop;
  - deploying to the cloud;
  - opening the demo without a login, with spending caps instead;
  - choosing the coverage required for held-out test emails;
  - staying with Anthropic models rather than switching provider for cheaper testing.
- **Claude wrote most of the code, in small steps.** I reviewed and approved each step before it was committed, with no AI co-author trailer, because I own the work.
- **AI-made data is labelled as such.**
  - The 40 dev emails were drafted by Claude, edited in a review pass by a second AI, Codex (see `DEV_REVIEW.md`), and only then read and approved by me.
  - The dataset check refused them until I had.
  - The 20 held-out test emails were drafted by Codex, separately from the development set, then reviewed and edited by me.

Some of my messages were picked from reply buttons Claude Code suggested; the ones I typed myself are marked ✍.

## My 5 most useful prompts

1. ✍ *"Before writing code, walk me through the architecture and the decisions I need to own for the live review."*

   Why it worked: I got a list of decisions with the reason behind each, before any code existed. That list became DESIGN.md and my preparation for the live review, so I can explain every part instead of defending code I never understood.

2. *"I'll handwrite the 20 held-out test emails. You draft the 40 dev emails for me to review and edit (marked ai_drafted_human_edited)."*

   Why it worked: separating development and test emails gave me a separate evaluation set. My original plan was to handwrite the test emails, but Codex ultimately drafted them. They should therefore be described as AI-drafted.

3. *"Build Task 2 learning from human edits: versioned lessons, regression check, rollback."*

   Why it worked: naming all three requirements up front produced a design where every lesson is measured on past emails before it goes live, and can be undone in one click. "Learning" without those three is just a prompt that drifts.

4. *"Build the eval harnesses for all three tasks (20+ cases each, model comparisons, cost and latency) so they run as soon as the key is added."*

   Why it worked: with no API credit yet, building them dry-run first meant no time was wasted once the key arrived. The harnesses then found real bugs before any reviewer could:
   - a schema limit that threw away screenshot descriptions;
   - a classifier crash;
   - a merge rule that buried the best search results.

5. *"Commit this first (no AI co-author trailer), then re-run ES2002c→d (~$0.70) to confirm why changes are missed and propose a fix."*

   Why it worked: it asked for a diagnosis before a fix. The re-run showed the problem wasn't the change detector but the analyst filing product facts as "decisions". The fix then took follow-up change detection from about 3 of 5 to 5, 4 and 3 of 5 across three runs. A blind prompt tweak would have chased the wrong cause.

## One time I rejected the AI's answer

**Deployment.** Claude recommended treating cloud deployment as optional ("local Docker only is a perfectly fine submission"), because the brief lists it only as "good to have" and time was short. I disagreed: ✍ *"they are saying that it is good to have, so miss out on chances the make me much more eligible for the role"* (I meant: don't miss the chance).

I created an AWS account that day, and the code was built deployment-ready from then on: Docker images with health checks, secrets kept in the environment only, and seed snapshots. It went live on AWS (EC2 t3.small in Mumbai) on 2026-10-18, with HTTPS.

Later I overrode the login Claude had put in front of it: I wanted reviewers to open the link and start. Claude flagged the risk, an open site spending my API credit, and replaced the login with guards:
- a spending cap per service;
- per-visitor rate limits;
- no search-engine indexing;
- my own spend limit on the Anthropic account.

**What I learned:** the AI optimises for what the brief states and for the deadline. I was also optimising for things the brief can't tell it to weigh: the role's "good to have" skills, and what a reviewer feels in the first ten seconds of opening a link. Those calls were mine to make, and the AI was good at making them safe once I had made them.

## Times the AI was wrong and the work caught it

- **Hybrid search:** the plan assumed hybrid would beat vector search. The first eval said no: on 30 questions, hybrid had 83% top-5 vs vector's 87%. Hybrid only pulled ahead once the keyword weight was lowered to 0.3: 89% vs 86% on 35 questions. The final 42-question numbers are in EVALS.md.
- **Feasibility case:** "native app" was planned as custom work, but the help center documents a WebView-app route.
- **Seeding bug:** copying a live database as a seed silently failed (WAL files on a read-only mount). The Docker run caught it; it was fixed with `VACUUM INTO`.
- **Follow-up change detection** started at 2 of 5 changes found. It was diagnosed (product facts filed as decisions, a rejected option kept as new work) and fixed. EVALS.md reports every run, including the misses.
- **A keyword score that hid errors:** Haiku as the analyst scored 4 of 5 on the keyword check, but reading its output showed three wrong product facts in a client scope. That's why Sonnet stayed.
- **Voice input:** a test with real recorded speech showed the newest PyAV broke faster-whisper. A unit test with a fake transcriber would never have caught it.
- **Deployment slips caught by checking the live system:**
  - Caddy hadn't reloaded its config, so the mic's server fallback was missing live. It now restarts on every deploy.
  - The usage bar would have said "available" while the account was out of credit, because a build's failure was timed by its start. It now uses the time the build ended.
