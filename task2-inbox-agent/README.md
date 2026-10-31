# Inbox Agent (Task 2)

Sorts customer emails and drafts replies from the Knowledge Brain. It's a Gmail-style inbox:
each email gets a label, and a human approves every draft. A label may start sending on its
own after X unedited approvals in a row (set in Settings, 20 by default). It can connect to
a real Gmail account ([setup](../docs/GMAIL_SETUP.md)). TypeScript, Fastify, Node's built-in
SQLite. Runs on http://localhost:8002.

## Run and test

```bash
npm ci
npm start                 # the service
npm test                  # unit tests, with fake models (no API key needed)
npm run typecheck
npm run dataset:check     # validate the labelled emails against the coverage plan
npm run eval              # dry run; add -- --run to call Claude (paid)
```

## How an email is handled (`src/pipeline.ts`)

1. **Clean**: card numbers and passwords are removed (`email/pii.ts`).
2. **Classify**: Claude picks the type and label (`llm/models.ts`, `email/taxonomy.ts`,
   `email/topics.ts`).
3. **Look up**: the Knowledge Brain is asked for grounded facts (`knowledge-client/`).
4. **Draft**: Claude writes the reply, then code checks the policy rules
   (`drafting/policy.ts`).
5. **Review**: a human approves, edits or rejects the draft. Edits teach lessons
   (`feedback/learning.ts`) and unedited streaks earn auto-send (`feedback/autonomy.ts`).

## Folders

```
src/
  main.ts, server.ts    start-up and the HTTP API + pages
  pipeline.ts           one email in, one reviewed (or auto-sent) reply out
  config.ts             settings from environment variables, model prices
  settings.ts           each inbox's own settings (X, autonomy)
  company.ts            the business the inbox belongs to
  workspaces.ts         one inbox per demo account
  session.ts, guard.ts  demo accounts; rate limits and spending cap
  llm/                  Claude: client (Bedrock or Anthropic) and the classify/draft calls
  email/                taxonomy, categories, PII removal
  drafting/             rule checks on every draft
  feedback/             learning from edits, autonomy, draft-vs-sent similarity
  gmail/                Google OAuth and the Gmail calls
  knowledge-client/     typed client for the Knowledge Brain API
  store/                SQLite persistence
  dataset/              labelled-email schema, coverage plan, checks
  eval/                 the eval runner and its grading
prompts/                versioned prompts (classify, draft, judge, lesson mining)
data/emails/            the labelled test emails (dev.jsonl, test.jsonl) and their plan
evaluation/results/     eval run results
ui/index.html           the web page
tests/                  unit tests
```
