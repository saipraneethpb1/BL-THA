from collections import Counter

from evaluation import metrics
from evaluation.compare import compare, flips
from evaluation.run_retrieval_eval import check, load_cases, search_text

CASE = {"id": "X", "gold": ["a", "b"], "should_refuse": False}


def test_ranks_count_articles_not_passages() -> None:
    ranked = ["z", "z", "y", "b", "a"]  # two passages of z are one result
    assert metrics.first_rank(ranked, ["a", "b"]) == 3
    assert metrics.hit_at(CASE, ranked, 3) and not metrics.hit_at(CASE, ranked, 2)


def test_multi_part_needs_every_part() -> None:
    case = {"gold": ["a", "c"], "parts": [["a"], ["c"]]}
    assert not metrics.hit_at(case, ["a", "x", "y"], 5)
    assert metrics.hit_at(case, ["a", "x", "c"], 5)
    scored = metrics.case_scores(case, ["a", "x", "y"], 0.9, 0.6)
    assert scored["failure"] == "multi-part miss"


def test_ndcg_rewards_ranking_all_gold_first() -> None:
    assert metrics.ndcg_at(["a", "b", "x"], ["a", "b"]) == 1.0
    assert 0 < metrics.ndcg_at(["x", "a", "b"], ["a", "b"]) < 1
    assert metrics.ndcg_at(["x", "y"], ["a"]) == 0.0


def test_gate_failures() -> None:
    refuse = {"gold": [], "should_refuse": True}
    assert metrics.case_scores(refuse, [], 0.7, 0.6)["failure"] == "wrongly answered"
    assert metrics.case_scores(refuse, [], 0.5, 0.6)["failure"] is None
    low = metrics.case_scores(CASE, ["a"], 0.5, 0.6)
    assert low["failure"] == "wrong refusal" and low["wrongly_refused"]
    assert metrics.case_scores(CASE, ["x"] * 3, 0.9, 0.6)["failure"] == "retrieval miss"


def test_summary_and_threshold_suggestion() -> None:
    rows = [
        metrics.case_scores(CASE, ["a"], 0.80, 0.6),
        metrics.case_scores(CASE, ["x", "b"], 0.70, 0.6),
        metrics.case_scores({"gold": [], "should_refuse": True}, [], 0.65, 0.6),
    ]
    s = metrics.summarise(rows)
    assert s["recall@5"] == 1.0 and s["hit@1"] == 0.5 and s["mrr@10"] == 0.75
    assert s["wrongly_answered"] == 1.0 and s["answerable_cases"] == 2
    sweep = metrics.gate_sweep(rows, [0.6, 0.66, 0.72])
    # 0.66 refuses the off-topic case and still answers both real ones
    assert metrics.suggest_threshold(sweep) == 0.66
    lo, hi = metrics.bootstrap_ci(rows[:2], lambda rs: sum(r["hit@1"] for r in rs) / len(rs))
    assert 0 <= lo <= 0.5 <= hi <= 1


def test_check_flags_drops_and_index_changes() -> None:
    rows = [{"id": "X", "answerable": True, "hit@5": False, "passes_gate": True}]
    summary = dict.fromkeys(["recall@5", "recall@10", "mrr@10", "ndcg@10", "in_context@6"], 0.5)
    summary |= {"wrongly_answered": 0.0, "wrongly_refused": 0.0}
    index = {"chunks": 10, "chunk_text_sha256": "abc", "embedding_model": "m"}
    baseline = {
        "summary": summary | {"recall@5": 0.6},
        "cases": {"X": {"hit@5": True, "passes_gate": True}},
        "index": index,
    }
    problems = check(baseline, summary, rows, 0.0, index)
    assert any("recall@5 fell" in p for p in problems)
    assert any("X: gold article no longer" in p for p in problems)
    assert check(baseline | {"cases": {}}, summary, rows, 0.2, index) == []
    moved = check(baseline | {"cases": {}}, summary, rows, 0.2, index | {"chunks": 11})
    assert moved and "re-run with --write-baseline" in moved[0]


def test_paired_compare_and_flips() -> None:
    a = {
        f"c{i}": {
            "answerable": True,
            "hit@5": i < 5,
            "hit@10": True,
            "rr@10": 1.0,
            "ndcg@10": 1.0,
            "hit@6": True,
        }
        for i in range(20)
    }
    b = {k: v | {"hit@5": True} for k, v in a.items()}
    recall = next(r for r in compare(a, b) if r["metric"] == "recall@5")
    assert recall["diff"] == 0.75 and recall["real"]
    fixed, broken = flips(a, b)
    assert len(fixed) == 15 and broken == []


def test_case_file_is_well_formed() -> None:
    cases = load_cases("all")
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)) and len(cases) >= 100
    for c in cases:
        assert c["split"] in ("dev", "heldout")
        assert c["should_refuse"] == (not c["gold"]), c["id"]
        if c.get("parts"):
            assert set(c["gold"]) == {g for p in c["parts"] for g in p}, c["id"]
    split = Counter(c["split"] for c in cases)
    assert 0.25 <= split["heldout"] / len(cases) <= 0.35
    smoke = load_cases(smoke=True)
    assert 10 <= len(smoke) <= 20 and all(c["split"] == "dev" for c in smoke)
    assert {c["category"] for c in cases} >= {
        "how-to",
        "pricing",
        "troubleshooting",
        "out-of-scope",
        "follow-up",
        "typo",
        "multi-part",
        "adversarial",
    }


def test_follow_up_is_searched_with_its_earlier_turns() -> None:
    case = {"question": "where are the keys?", "history": ["How do I set up Stripe?"]}
    text = search_text(case)
    assert "How do I set up Stripe?" in text and text.endswith(
        "Current question: where are the keys?"
    )
    assert search_text({"question": "plain"}) == "plain"
