# Knowledge Brain evals

Every eval is graded in code against labelled cases, so it needs no judge model. Run each one from
`task1-knowledge-brain/` as `uv run python -m evaluation.<script>`. Results are written in
[../../EVALS.md](../../EVALS.md).

## Scripts

| script | question it answers | cost |
|---|---|---|
| `run_retrieval_eval.py` | Does search find the right article, does the evidence gate answer or refuse correctly, and how fast is it? | free (local) |
| `run_robustness_eval.py` | Does every wording of a question (synonyms, typos, other names) find the same right article? | free |
| `run_answer_eval.py` | Which Claude model writes the best grounded answers, at what cost and speed? | paid |
| `run_vision_eval.py` | Which model should describe the help-center screenshots? | paid |
| `compare.py` | Is the difference between two retrieval runs real, or noise? | free |

Shared code:
- `metrics.py`: recall@k, MRR, nDCG, gate error rates, confidence intervals and the
  threshold sweep.
- `common.py`: timing stats, result files and loading `.env`.

## Data

```
cases/
  retrieval.jsonl     147 questions: gold articles, should_refuse, category, dev/heldout split,
                      and a smoke flag on 17 of them (run in CI)
  answer.jsonl        questions with the expected outcome (answer or refuse), gold articles and
                      the facts a correct answer must state
  vision.jsonl        screenshots with the labels a good description must mention
baselines/
  smoke.json          the known-good smoke scores; CI fails if a run falls below them
results/              one JSON per run, with the code, case-file and index versions it used
```

`dev` cases are for tuning. Run `heldout` only to compare finished versions, so its scores
stay an honest test.

## Common commands

```bash
uv run python -m evaluation.run_retrieval_eval                       # production search, dev cases
uv run python -m evaluation.run_retrieval_eval --configs all         # keyword vs vector vs hybrid vs production
uv run python -m evaluation.run_retrieval_eval --split heldout       # final comparison only
uv run python -m evaluation.run_retrieval_eval --smoke --check evaluation/baselines/smoke.json   # what CI runs
uv run python -m evaluation.run_retrieval_eval --smoke --write-baseline evaluation/baselines/smoke.json
uv run python -m evaluation.compare evaluation/results/A.json evaluation/results/B.json
```

Re-write the baseline only when a change in search or in the index is intended.
