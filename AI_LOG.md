# AI log

> **TODO (Sai):** This is an outline drafted from the real conversation. Check every quote against the chat export, write the "why it worked" lines in your own words, and delete this note.

**Tool:** Claude Code (desktop app), one continuous conversation from the first read of the brief to the final docs. The full chat export is submitted with the repo.

**How I worked:**
- I made the product decisions: categories, autonomy rules, what to drop, deploying to the cloud, writing the test emails myself.
- Claude wrote most of the code, in small steps. I reviewed and committed each step myself, with no AI co-author trailer, because I own the work.
- AI-drafted data is labelled as such. The 40 dev emails start as `ai_drafted_pending_review` and the dataset check fails until I've reviewed each one.

Some of my messages were picked from reply buttons Claude Code suggested; the ones I typed myself are marked ✍.

## My 5 most useful prompts

1. ✍ *"Before writing code, walk me through the architecture and the decisions I need to own for the live review."*
   Why it worked: **TODO (Sai)**. For example: it produced a decision list with the reason behind each choice, which became DESIGN.md and my live-review prep, instead of code I couldn't explain.

2. ✍ *"Seems good, what about the deployment part?"*
   Why it worked: **TODO (Sai)**. It surfaced things I hadn't considered: seed snapshots so a reviewer doesn't wait 40 minutes, protecting API credits on a public URL, health checks.

3. *"I'll handwrite the 20 held-out test emails. You draft the 40 dev emails for me to review and edit (marked ai_drafted_human_edited)."*
   Why it worked: **TODO (Sai)**. Splitting it this way means the AI never grades itself on emails it wrote, and the provenance field keeps the dataset honest.

4. *"Build Task 2 learning from human edits: versioned lessons, regression check, rollback."*
   Why it worked: **TODO (Sai)**. Naming all three requirements up front got a design where every change is measured before going live and can be undone.

5. *"Build the eval harnesses for all three tasks (20+ cases each, model comparisons, cost and latency) so they run as soon as the key is added."*
   Why it worked: **TODO (Sai)**. With no API credit yet, building dry-run-first harnesses meant zero wasted time once the key arrived.

> **TODO (Sai):** Swap in any prompt that mattered more to you. Keep quotes exact.

## One time I rejected the AI's answer

**TODO (Sai):** Confirm this is the one you want, and write it in your own words.

Candidate: **deployment.** Claude recommended treating cloud deployment as optional ("local Docker only is a perfectly fine submission") because the brief lists it only as "good to have" and time was short. I disagreed: ✍ *"they are saying that it is good to have, so miss out on chances the make me much more eligible for the role"* (I meant: don't miss the chance). I created an AWS account that day, and the code was built deployment-ready from then on: Docker images with health checks, environment-only secrets, seed snapshots.

**What I learned:** **TODO (Sai):** for example, that the AI optimises for the stated requirements and the deadline, while I was also optimising for the role's "good to have" skills, which the brief alone doesn't tell it to weigh.

## Times the AI was wrong and the work caught it

- **Hybrid search:** the plan assumed hybrid would beat vector search. The first eval said no: on 30 questions, hybrid had 83% top-5 vs vector's 87%. Hybrid only pulled ahead once the keyword weight was lowered to 0.3: 89% vs 86% on 35 questions. The final 42-question numbers are in EVALS.md.
- **Feasibility case:** "native app" was planned as custom work, but the help center documents a WebView-app route.
- **Seeding bug:** copying a live database as a seed silently failed (WAL files on a read-only mount). The Docker run caught it; it was fixed with `VACUUM INTO`.
