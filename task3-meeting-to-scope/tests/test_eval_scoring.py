from evaluation.common import load_jsonl
from evaluation.run_feasibility_eval import HERE, grade
from evaluation.run_transcript_eval import matches, score_changes, score_requirements
from scope.models import Evidence, Requirement, RequirementChange


def req(rid: str, desc: str, verified: bool = True) -> Requirement:
    ev = Evidence(
        meeting_id="m", quote="q", speaker="s", turn_index=0, timestamp=None, verified=verified
    )
    return Requirement(
        id=rid,
        description=desc,
        status="confirmed",
        priority="must",
        origin="client_stated",
        evidence=[ev],
    )


def test_feasibility_grading_separates_costly_mistakes() -> None:
    built_in = {"accept": ["existing_configuration"]}
    custom = {"accept": ["custom_development"]}
    assert grade(built_in, "existing_configuration") == {
        "correct": True,
        "missed_saving": False,
        "false_promise": False,
    }
    assert grade(built_in, "custom_development")["missed_saving"]
    assert grade(custom, "existing_with_limitation")["false_promise"]
    cases = load_jsonl(HERE / "feasibility_cases.jsonl")
    assert len(cases) >= 20 and len({c["id"] for c in cases}) == len(cases)


def test_keyword_matching_and_requirement_scores() -> None:
    assert matches("Show a MAP on search results", [["map"], ["search", "nearby"]])
    assert not matches("Show a map", [["map"], ["search", "nearby"]])
    gold = [{"id": "G1", "keywords": [["map"]]}, {"id": "G2", "keywords": [["blog"]]}]
    s = score_requirements(
        gold, [req("REQ-001", "Map of clinics"), req("REQ-002", "Logo", verified=False)]
    )
    assert (s["found"], s["missed"], s["unmatched_extracted"], s["quotes_verified"]) == (
        1,
        ["G2"],
        1,
        0.5,
    )


def test_change_detection_needs_the_right_kind_of_change() -> None:
    changes = [
        RequirementChange(
            change="removed",
            requirement_id="REQ-002",
            before="Card payments",
            after=None,
            summary="dropped",
            out_of_scope=False,
            evidence=[],
        ),
        RequirementChange(
            change="modified",
            requirement_id="REQ-001",
            before="Map",
            after="Map with opening hours",
            summary="hours",
            out_of_scope=True,
            evidence=[],
        ),
    ]
    expected = [
        {"change": "removed", "keywords": [["card"]]},
        {"change": "added", "keywords": [["map"]]},
    ]
    s = score_changes(expected, changes)
    assert (s["detected"], s["flagged_out_of_scope"]) == (1, ["REQ-001"])
