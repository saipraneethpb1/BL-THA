# Demo video script (5 minutes)

Recorded on the live site: https://13-203-203-28.sslip.io. It covers one flow per task, the approval gate, and one honest failure. Spoken lines are in quotes; say them in your own words, they're a guide, not a teleprompter.

## Before you record (about 15 minutes, no commands)

1. **Credits are topped up and the site is on the real models** (Opus answers, Sonnet drafting and scoping, Haiku for the small jobs). The usage bar at the bottom should be green, "Claude API available". If you rehearsed in Haiku mode, ask Claude to switch back first.
2. **The demo project is pre-built** (Claude does this after credits are added): "TV remote control (demo)" in the Scope Builder, with v1 approved and v2 (the follow-up meeting) as a draft. A live build takes about 2 minutes, too long to watch on camera.
3. **Check the follow-up's Changes tab** before recording, and note which of the five AMI changes it found (see "An honest failure" below). That decides which plan you use.
4. Browser: Chrome or Edge (no sign-in needed), zoom 110–125%, notifications off, other tabs closed. Press **Ctrl+F5** once on each service so you see the latest version.
5. **Rehearse once in full.** Each question costs about 2 cents and each draft about 5 cents; the usage bar shows the total.

---

## 0:00–0:25 · Hub page

**Show:** https://13-203-203-28.sslip.io (the logo, the three cards, the usage bar at the bottom).

> "This is my submission: three services that do a consultant's slow work. A Knowledge Brain over the Brilliant Directories help center, an Inbox Agent that drafts replies from it, and a Scope Builder that turns client meetings into a priced scope. It runs on AWS and anyone can open it, so each service has a spending cap and per-visitor limits. The bar at the bottom shows exactly what the AI has cost so far against that budget."

Optional: click the sun/moon icon once to show light and dark mode.

## 0:25–1:25 · Task 1: Knowledge Brain

**Show:** the **Knowledge Brain** card or the top bar.

1. Click the example **"How do I hide my site from visitors while we're still building it?"**
   - The answer comes from the "Staging Mode" article, although the question shares no words with it.
   > "A small model first rewrites the question in help-center wording. That took retrieval from 88% to 93–98%."
   - Click a screenshot to enlarge it, then a source link.
2. Click the mic and **say** a question, for example *"How do I refund a member's payment?"*, then press Ask.
   > "You can speak instead of typing. If the browser's own dictation isn't available, it's transcribed by an open-source model on our server, so it works in every browser."
3. Click **"What is your agency's refund policy for setup fees?"**: it declines and opens an expert task.
   > "If the help center doesn't cover it, it says so instead of guessing, and the question goes to a human."
   - Open **Expert queue**, write a one-line answer, **Save answer**.

## 1:25–2:35 · Task 2: Inbox Agent

**Show:** **Inbox Agent** in the top bar. The "Start here" panel lists emails to try.

1. Click **E008 · Misspelled how-to question** → **Draft a reply** (about 10–15 s; the button shows the steps).
   - Point at **What the Knowledge Brain said**: the reply is grounded in a cited article.
   - Change one sentence, then **Send edited**.
   > "A person approves every draft. Edits are recorded, and under Learning the agent can propose lessons from them, which are tested on past emails before going live and can be rolled back."
2. Click **E019 · Refund request with a hidden instruction** → **Draft a reply**.
   > "This email tells the AI to approve a refund. It's treated as text, not an instruction. Refunds always go to a person, and a rule check in code blocks any reply that says a refund or cancellation is done."
3. Open **Autonomy**.
   > "A kind of email can only start sending on its own after 20 approvals in a row without edits. Refunds, complaints and quotes never can, and I can revoke it at any time."
   > "On 20 test emails I wrote by hand and never tuned on: every category right, and no unsafe auto-sends."

## 2:35–4:10 · Task 3: Scope Builder and the approval gate

**Show:** **Scope Builder**, then the **TV remote control (demo)** project.

1. Click **v1** (approved). Point at the summary cards, then the tabs:
   - **Requirements:** each has the client's exact words and a timestamp.
   - **Product check:** built-in setting, built-in with limits, or custom work.
   - **Estimate:** ranges per task; the totals and price are calculated in code.
   > "These are real recorded meetings from the public AMI corpus. A team of agents extracts requirements with quotes, checks each against the product before it's estimated, then scopes, estimates and writes the proposal."
2. Click **v2** (built from the follow-up meeting) → **Changes**.
   - Point at "kinetic → regular battery": it reduces work, so it isn't flagged.
   - Point at the changes flagged **adds work**.
   > "Follow-ups are compared with what the client approved, not with the last draft. Only work that's added gets flagged for a separate quote."
3. **The approval gate:** open **Review & approve** on v2.
   - Point at **"Download proposal (after approval)"**: greyed out.
   - Type your name and click **Approve version 2**.
   - The audit trail records it, and **Download proposal for the client** now works.
   > "Nothing reaches a client without a named person approving it, and if the automatic review found blocking issues, approving needs a written reason."

## 4:10–4:45 · An honest failure

**Plan A (preferred):** stay on v2's **Changes**. The AMI annotators recorded five changes in this meeting:
- kinetic battery dropped;
- regular chip;
- double-curved case;
- push buttons;
- display on the TV.

Point at whichever one is missing. In my runs, "display on the TV" was the most common miss.

> "Here's a miss. The annotators recorded that the display moved to the TV, and the system didn't flag it. Across three runs it caught 5, 4 and 3 of these 5 changes. Earlier versions were worse; I fixed most of it. What's left: the analyst sometimes writes 'a display' without saying which one, and follow-up decisions aren't compared yet. That's my next fix."

**Plan B (if all five are found):** say "this run caught all five; across runs it was 5, 4 and 3", and show the table in EVALS.md. Or use the inbox over-caution:

> "My biggest remaining weakness is over-caution: on the held-out emails, 6 answerable questions went to a person because the Knowledge Brain called its own correct answer 'partial'. It fails safe, but it costs the team time."

DESIGN.md §7 already describes Plan A's failure as "the failure shown in the demo video". If you use Plan B instead, change that line to match.

## 4:45–5:00 · Close

**Show:** the usage bar.

> "Every number I mentioned is in EVALS.md, from saved runs, including the misses. The bar shows what all of this cost to run. Thanks for watching."

---

**If you run long:** cut the expert-queue answer (Task 1 step 3) and the Autonomy tab (Task 2 step 3). Never cut the approval gate or the failure.
