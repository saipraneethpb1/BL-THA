# Scope Builder (Task 3)

A team of agents turns meeting transcripts into requirements, a scope, an estimate and a
proposal. It checks what the product already does first, compares follow-up meetings with the
approved scope, and sends nothing without approval. Python, FastAPI, SQLite. Runs on
http://localhost:8003.

## Run and test

```bash
uv run uvicorn scope.api:app --port 8003
uv run pytest && uv run mypy && uv run ruff check .
uv run python -m evaluation.run_feasibility_eval     # does it know what the product already does?
uv run python -m evaluation.run_transcript_eval      # the whole pipeline on recorded meetings
```

## The agent team (`scope/pipeline.py`)

Every handoff between agents is a typed model (`scope/models.py`). An output that doesn't fit
is checked in code and retried once (`agents/base.py`).

1. **Analyst** (`agents/analyst.py`): transcripts become requirements, decisions and open
   questions, each with evidence quoted from the meeting.
2. **Feasibility** (`agents/feasibility.py`): asks the Knowledge Brain whether the product
   already does each requirement.
3. **Scope** (`agents/scoping.py`): deliverables with acceptance criteria.
4. **Estimator** (`agents/estimator.py`): tasks with low / likely / high hours.
5. **Proposal** (`agents/proposal.py`): writes the proposal, then reviews it.
6. **Changes** (`agents/changes.py`): for a follow-up meeting, what changed since the
   approved scope.

## Folders

```
scope/
  api.py              HTTP API and pages
  pipeline.py         runs the agent team, stores a new draft version
  agents/             the six agents above, plus base.py (run, check, retry)
  models.py           the typed handoffs between agents
  llm.py, claude.py   structured Claude calls; where Claude is reached (Bedrock or Anthropic)
  knowledge.py        client for the Knowledge Brain API (never imports its code)
  transcript.py       read a transcript and cut it into model-sized chunks
  store.py            projects, meetings, scope versions, audit trail (SQLite)
  config.py           settings and models
  workspaces.py       one workspace per demo account
  session.py, guard.py  demo accounts; rate limits and spending cap
  prompts/            versioned prompts
  ui/index.html       the web page
transcripts/          sample meetings (AMI corpus; see AMI_ATTRIBUTION.md)
evaluation/
  cases/feasibility.jsonl   requirements with the known answer
  cases/gold/               per-meeting gold labels (TEMPLATE.json for new meetings)
  results/                  eval run results
tests/                unit tests, with scripted fake agents (no API key needed)
```
