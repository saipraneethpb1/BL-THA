# Demo video script (5 minutes)

One flow per task, one honest failure, and the approval gate. Spoken lines are in quotes. Keep them in your own words; they're a guide, not a teleprompter.

## Before you record (about 20 minutes)

1. `docker compose up -d` and check that all three health pages say `ok` (ports 8001, 8002, 8003).
2. **Pre-build the slow parts.** A Scope Builder build takes 1.5–3 minutes, too long to watch on camera.
   - In the Scope Builder (http://localhost:8003), create a project "Remote control (AMI ES2002)".
   - Add `task3-meeting-to-scope/transcripts/ami_ES2002c.txt` as a meeting and build it. **Don't approve yet.**
   - Keep `ami_ES2002d.txt` ready for later.
3. **Inbox Agent** (http://localhost:8002): click **Load the 40 sample emails** before recording.
4. **Rehearse every question once** (each costs about 2 cents) so nothing surprises you on camera. Models vary a little between runs; if one answer comes out badly, re-ask it before recording.
5. Browser zoom at 125%, notifications off, close other tabs. Record at 1080p.

---

## 0:00–0:20 · Opening

**Show:** a terminal with `docker compose up`, then the three health checks.

> "This is my submission for the three tasks: a Knowledge Brain over the Brilliant Directories help center, an Inbox Agent that drafts replies from it, and a Scope Builder that turns meeting transcripts into a quoted scope. One command starts all three. Every model choice you'll see was picked by an eval."

## 0:20–1:20 · Task 1: Knowledge Brain

**Show:** http://localhost:8001

1. Ask: **"How do I hide my site from visitors while we're still building it?"**
   - It finds "Staging Mode", even though the question shares no words with the article title.
   > "The question doesn't say 'staging'. A cheap model rewrites it into help-center wording first. That took top-5 retrieval from 88% to 93–98%."
   - Click a **Source** to show the citation.
2. Ask something the help center doesn't cover, for example **"What's your agency's refund policy for setup fees?"**
   - It declines instead of guessing.
   > "If the evidence isn't there, it says so and sends the question to a human."
   - Open **Expert queue**: the question is waiting. Write a short answer and click **Save answer**.
3. Point at **Knowledge status**:
   > "1,537 articles and 5,040 screenshots, all described by a vision model so text inside images is searchable. It refreshes daily."

## 1:20–2:30 · Task 2: Inbox Agent

**Show:** http://localhost:8002, **Inbox**

1. Open **E001** (blurry homepage banner). Click **Draft a reply**.
   - Show **What the Knowledge Brain said**: the reply is grounded in a cited article.
   - Make a small edit, then **Send**.
   > "Every draft is approved by a person. My edits are kept: under **Lessons**, the agent proposes rules from them, I test them against past emails, and I can roll back."
2. Open **E019** (refund request with an "AI assistant: …" instruction inside). Draft it.
   > "This email tries to instruct the AI to approve a refund. It's treated as text, not a command. Refunds always go to a human, and the policy check blocks any reply that says a refund or cancellation is done."
3. Open **Autonomy**:
   > "A kind of email can only start sending on its own after 20 clean approvals in a row, and refunds, complaints and quotes never can. One wrong send, and I can revoke it."
4. One line on results:
   > "On 20 test emails I wrote by hand and never tuned on: 100% correct category, zero unsafe auto-sends."

## 2:30–4:10 · Task 3: Scope Builder and the approval gate

**Show:** http://localhost:8003, the pre-built project.

1. Open **Version 1** (from ES2002c):
   - Scroll the requirements: each has the client's exact words and a timestamp.
   - Point at the feasibility labels: built-in, built-in with a limit, or custom work.
   - Point at the estimate.
   > "These are real recorded meetings from the AMI corpus, a public research dataset. A team of agents extracts requirements and checks each against the product before it's estimated. The model gives ranges; the arithmetic is done in code."
2. **The approval gate:**
   - Show that the draft can't be downloaded yet.
   - Click **Approve version 1**, enter your name, and show the **Audit trail**.
   - Now **Download proposal for the client** works.
   > "Nothing reaches a client without a named person approving it. If there are blocking issues, approving needs a written note."
3. **Follow-up meeting:** **Add meeting** with ES2002d, then build. Cut the wait in editing.
   - Open the new version's diff against the approved v1.
   - Point at "battery: kinetic → regular": a reduction, so not flagged.
   - Point at the new work flagged "outside the approved scope".
   > "Follow-ups are compared with what the client approved, not with the last draft. Only work that's added gets flagged for a separate quote."

## 4:10–4:45 · An honest failure

**Plan A, live (preferred).** Stay on the follow-up diff. Compare it with the AMI annotators' list of changes in this meeting: kinetic battery dropped, regular chip, double-curved case, push buttons, display on the TV. Point at whichever one is missing. "Display on the TV" was the most common miss in my runs.

> "Here's a miss. The annotators recorded that the display moved to the TV, and my system didn't catch it. Across three runs it found 5, 4 and 3 of these 5 changes. In this run the analyst described a display without saying which one, so there was nothing to compare. The next fix is to compare follow-up *decisions* too, not just requirements."

**Plan B (if all 5 are found).** Use Task 1 over-caution.

> "My biggest remaining weakness is over-caution. On the held-out emails, 6 answerable questions went to a human because the Knowledge Brain called its own correct answer 'partial'. That fails safe, but it costs the team time."

After recording, write the failure you showed into DESIGN.md §7 (the TODO there).

## 4:45–5:00 · Close

> "Every number here is in EVALS.md, from saved runs, including the misses. Thanks for watching."

---

**If you run long,** cut Task 1 step 3 and Task 2 step 3. Never cut the approval gate or the failure.
