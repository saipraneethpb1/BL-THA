from evaluation.run_answer_eval import grade, load_cases, summarise
from kb.answer.service import QueryResult, Source


def result(status: str, answer: str, urls: list[str]) -> QueryResult:
    return QueryResult(
        status=status,  # type: ignore[arg-type]
        answer=answer,
        sources=[
            Source(title="t", url=u, source_type="article", section=None, score=0.8) for u in urls
        ],
        needs_human=status != "answered",
        escalation_id=None,
        missing_info=None,
        model="m",
        prompt_version="v",
        top_score=0.8,
    )


CASE = {
    "expect": "answer",
    "gold": ["12000000238"],
    "facts": [["700"], ["1400"]],
    "forbidden": ["Paris"],
}


def test_answer_case_needs_status_citation_facts_and_no_forbidden() -> None:
    url = "https://x/articles/12000000238-recommended-image-sizes"
    assert grade(CASE, result("answered", "Use 700px to 1400px wide.", [url]))["passed"]
    assert not grade(CASE, result("answered", "Use 700px wide.", [url]))["passed"]  # missing a fact
    assert not grade(CASE, result("answered", "700 to 1400", ["https://x/other"]))[
        "passed"
    ]  # wrong source
    assert not grade(CASE, result("partial", "700 to 1400", [url]))["passed"]
    bad = grade(CASE, result("answered", "700 to 1400, like in Paris", [url]))
    assert bad["forbidden_found"] == ["Paris"] and not bad["passed"]


def test_abstain_case_fails_on_confident_answer() -> None:
    case = {"expect": "abstain", "gold": [], "facts": [], "forbidden": []}
    assert grade(case, result("insufficient_evidence", "Not covered.", []))["passed"]
    assert not grade(case, result("answered", "Sure, here is how.", ["https://x"]))["passed"]


def test_summary_and_case_file() -> None:
    cases = load_cases()
    assert len(cases) >= 30 and {c["expect"] for c in cases} == {"answer", "abstain"}
    assert len({c["id"] for c in cases}) == len(cases)
    rows = [
        {
            "expect": "answer",
            "behaviour_ok": True,
            "citation_ok": True,
            "facts_ok": False,
            "passed": False,
            "forbidden_found": [],
            "cost_usd": 0.01,
            "latency_ms": 1000.0,
        },
        {
            "expect": "abstain",
            "behaviour_ok": True,
            "citation_ok": True,
            "facts_ok": True,
            "passed": True,
            "forbidden_found": [],
            "cost_usd": 0.0,
            "latency_ms": 100.0,
        },
    ]
    s = summarise("m", rows)
    assert s["pass_rate"] == 0.5 and s["facts_ok"] == 0 and s["abstained_when_unanswerable"] == 1
    assert s["total_cost_usd"] == 0.01


def test_vision_label_matching_ignores_case_and_punctuation() -> None:
    from evaluation.run_vision_eval import label_hits, load_cases

    labels = [
        ["Don't enable HTTPS-Only Mode", "Don’t enable HTTPS-Only Mode"],
        ["Order by Name A-Z"],
    ]
    text = "Firefox settings with 'dont enable https only mode' selected. Visible labels: Order by Name A-Z."
    assert label_hits(text, labels) == [False, True]  # "dont" is not "don t": a real miss
    assert label_hits("Choose: Don't enable HTTPS-Only Mode.", labels) == [True, False]
    cases = load_cases()
    assert len(cases) >= 25 and all(c["labels"] and c["src"].startswith("https://") for c in cases)
