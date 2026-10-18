from pathlib import Path

import pytest

from scope.agents.changes import apply_changes
from scope.agents.estimator import compute_estimate, pert
from scope.agents.proposal import check_prose
from scope.agents.scoping import check_scope
from scope.config import Settings
from scope.models import (
    ChangeDetection,
    ChangeLink,
    Deliverable,
    EstimateDraft,
    EstimateTask,
    ExcludedRequirement,
    MeetingAnalysis,
    ProposalText,
    Requirement,
    ScopeOfWork,
)
from scope.pipeline import ScopeBuilder, export_proposal
from scope.store import StateError, Store
from tests.fakes import TRANSCRIPT, FakeKB, kb_answer
from tests.team import FOLLOWUP, team

# ---------- pure checks ----------


def deliverable(did: str, reqs: list[str], criteria: list[str] | None = None) -> Deliverable:
    return Deliverable(
        id=did,
        title=did,
        description="d",
        requirement_ids=reqs,
        approach="custom",
        acceptance_criteria=["works"] if criteria is None else criteria,
    )


def scope_of(deliverables: list[Deliverable], excluded: list[str] = ()) -> ScopeOfWork:  # type: ignore[assignment]
    return ScopeOfWork(
        deliverables=deliverables,
        excluded=[ExcludedRequirement(requirement_id=r, reason="x") for r in excluded],
        exclusions=[],
        assumptions=[],
        dependencies=[],
        client_responsibilities=[],
        open_questions=[],
    )


def test_scope_check_catches_missing_duplicate_unknown_and_untestable() -> None:
    ids = {"REQ-001", "REQ-002", "REQ-003"}
    assert (
        check_scope(scope_of([deliverable("D-01", ["REQ-001", "REQ-002"])], ["REQ-003"]), ids) == []
    )
    problems = check_scope(
        scope_of(
            [
                deliverable("D-01", ["REQ-001", "REQ-009"], criteria=[]),
                deliverable("D-02", ["REQ-001"]),
            ]
        ),
        ids,
    )
    assert "D-01 has no acceptance criteria" in problems
    assert any("REQ-009 does not exist" in p for p in problems)
    assert any("REQ-001 appears in more than one place" in p for p in problems)
    assert "REQ-002 is in no deliverable and not excluded" in problems


def task(did: str, low: float, likely: float, high: float) -> EstimateTask:
    return EstimateTask(
        deliverable_id=did,
        name=f"t{did}",
        low_hours=low,
        likely_hours=likely,
        high_hours=high,
        assumptions="",
        confidence="medium",
    )


def test_estimate_arithmetic_is_done_in_code() -> None:
    e = compute_estimate(
        EstimateDraft(tasks=[task("D-01", 2, 4, 12), task("D-02", 1, 1, 1)], risks=[]),
        hourly_rate=40,
        currency="USD",
        contingency_pct=10,
    )
    assert (e.low_hours, e.likely_hours, e.high_hours) == (3, 5, 13)
    assert e.expected_hours == round(pert(2, 4, 12) + 1, 1) == 6.0
    assert e.price_low == round(6.0 * 40 * 1.1, -1) == 260
    assert e.price_high == round(13 * 40 * 1.1, -1) == 570
    assert [d.deliverable_id for d in e.per_deliverable] == ["D-01", "D-02"]


def test_prose_with_numbers_is_rejected() -> None:
    p = ProposalText(
        title="t",
        executive_summary="Costs about $4,000 and 40 hours.",
        client_problem="",
        proposed_solution="",
        timeline="",
        next_steps="",
    )
    assert len(check_prose(p)) == 2


def req(rid: str, desc: str) -> Requirement:
    return Requirement(
        id=rid,
        description=desc,
        status="confirmed",
        priority="must",
        origin="client_stated",
        evidence=[],
    )


def test_changes_are_applied_against_the_baseline_and_unlinked_items_count_as_added() -> None:
    baseline = [req("REQ-001", "Map"), req("REQ-002", "Card payments"), req("REQ-003", "Logo")]
    followup = MeetingAnalysis(
        meeting_ids=["m2"],
        requirements=[req("X", "Map with hours"), req("Y", "Blog")],
        decisions=[],
        constraints=[],
        open_questions=[],
        commitments=[],
    )
    detection = ChangeDetection(
        links=[
            ChangeLink(
                new_item_id="NEW-01",
                baseline_id="REQ-001",
                change="modified",
                summary="hours added",
            ),
            ChangeLink(
                new_item_id=None,
                baseline_id="REQ-002",
                change="removed",
                summary="payments dropped",
            ),
        ],
        constraint_changes=[],
    )
    revised, changes = apply_changes(baseline, followup, detection)
    assert [(r.id, r.description) for r in revised] == [
        ("REQ-001", "Map; Map with hours"),  # no revised_statement: old + delta, never delta alone
        ("REQ-003", "Logo"),
        ("REQ-004", "Blog"),
    ]
    by_id = {c.requirement_id: c for c in changes}
    assert (by_id["REQ-001"].change, by_id["REQ-001"].out_of_scope) == ("modified", True)
    assert (by_id["REQ-002"].change, by_id["REQ-002"].out_of_scope) == ("removed", False)
    assert by_id["REQ-003"].change == "unchanged"
    assert (by_id["REQ-004"].change, by_id["REQ-004"].out_of_scope) == ("added", True)


def test_changed_decisions_are_caught_and_only_added_work_is_flagged() -> None:
    baseline = [req("REQ-001", "Kinetic battery"), req("REQ-002", "Logo")]
    followup = MeetingAnalysis(
        meeting_ids=["m2"],
        requirements=[req("X", "Regular batteries"), req("Y", "Square push buttons")],
        decisions=[],
        constraints=[],
        open_questions=[],
        commitments=[],
    )
    detection = ChangeDetection(
        links=[
            ChangeLink(
                new_item_id="NEW-01",
                baseline_id="REQ-001",
                change="modified",
                summary="cheaper battery",
                revised_statement="Regular batteries",
                adds_work=False,
            ),
            ChangeLink(
                new_item_id="NEW-02",
                baseline_id="DEC-002",
                change="modified",
                summary="rubber to push buttons",
            ),
            ChangeLink(new_item_id=None, baseline_id="DEC-001", change="removed", summary="no LCD"),
        ],
        constraint_changes=[],
    )
    notes = ["Small LCD on the remote", "Rubber buttons"]
    revised, changes = apply_changes(baseline, followup, detection, notes)
    assert [r.description for r in revised] == ["Regular batteries", "Logo", "Square push buttons"]
    by_id = {c.requirement_id: c for c in changes}
    assert (by_id["REQ-001"].change, by_id["REQ-001"].out_of_scope) == ("modified", False)
    assert (by_id["REQ-003"].change, by_id["REQ-003"].before) == ("modified", "Rubber buttons")
    assert by_id["REQ-003"].out_of_scope
    assert (by_id["DEC-001"].change, by_id["DEC-001"].out_of_scope) == ("removed", False)


# ---------- end to end with a scripted team ----------


@pytest.fixture
async def setup(tmp_path: Path) -> tuple[Store, ScopeBuilder, int]:
    store = Store(tmp_path / "scope.db")
    await store.init()
    kb = FakeKB({"map": kb_answer("Turn on the Google Map for search results.")})
    builder = ScopeBuilder(store, team(), kb, Settings(db_path=tmp_path / "x.db", hourly_rate=40))
    pid = await store.create_project("Green Valley Vets", "Green Valley")
    await store.add_meeting(pid, "Kickoff", "2026-10-10", TRANSCRIPT)
    return store, builder, pid


async def test_first_build_then_approval_gate(setup: tuple[Store, ScopeBuilder, int]) -> None:
    store, builder, pid = setup
    n = await builder.start(pid)
    await builder.build(pid, n)
    v = await store.version(pid, n)
    assert v is not None and v.status == "draft", v.error if v else None
    assert v.data is not None
    assert [r.description for r in v.data.analysis.requirements] == [
        "Show a map of nearby clinics on search results",
        "Clinics pay a yearly listing fee by card",
    ]
    assert {f.verdict for f in v.data.feasibility} == {
        "existing_configuration",
        "custom_development",
    }
    assert "Already possible with built-in features" in v.data.proposal_md
    assert "DRAFT" in v.data.proposal_md
    assert v.usage["calls"] > 0

    with pytest.raises(StateError, match="only an approved version"):
        await export_proposal(store, pid, n, "sai")
    await store.approve(pid, n, "Sai", None)
    md = await export_proposal(store, pid, n, "sai")
    assert "APPROVED" in md and "DRAFT" not in md
    with pytest.raises(StateError, match="no new meetings"):
        await builder.start(pid)
    log = [a["action"] for a in await store.audit_log(pid)]
    assert {"build_started", "draft_ready", "approved", "exported"} <= set(log)


async def test_follow_up_is_compared_with_the_approved_baseline(
    setup: tuple[Store, ScopeBuilder, int],
) -> None:
    store, builder, pid = setup
    v1 = await builder.start(pid)
    await builder.build(pid, v1)
    await store.approve(pid, v1, "Sai", None)
    await store.add_meeting(pid, "Follow-up", "2026-10-24", FOLLOWUP)

    v2 = await builder.start(pid)
    await builder.build(pid, v2)
    v = await store.version(pid, v2)
    assert v is not None and v.data is not None and v.data.diff is not None, v.error if v else None
    assert v.baseline_version == v1
    kinds = {c.change: c for c in v.data.diff.changes}
    assert kinds["removed"].before == "Clinics pay a yearly listing fee by card"
    assert kinds["modified"].after == (
        "Show a map of nearby clinics on search results, including each clinic's opening hours"
    )
    assert kinds["added"].after == "Add a blog for pet care tips" and kinds["added"].out_of_scope
    assert v.data.diff.constraint_changes == ["Launch moved from March to April"]
    assert "outside the approved scope" in v.data.proposal_md
    assert [r.id for r in v.data.analysis.requirements] == ["REQ-001", "REQ-003"]
    # Only the modified map and the new blog were re-checked; v1 asked about 2 requirements.
    kb = builder.feasibility.kb
    assert isinstance(kb, FakeKB) and len(kb.questions) == 2 + 2

    await store.approve(pid, v2, "Sai", None)
    first = await store.version(pid, v1)
    assert first is not None and first.status == "superseded"


async def test_blocking_issues_need_a_note_to_approve(
    setup: tuple[Store, ScopeBuilder, int],
) -> None:
    store, builder, pid = setup
    n = await builder.start(pid)
    await builder.build(pid, n)
    async with store.db() as db:  # simulate a draft whose estimate check failed
        await db.execute(
            "UPDATE versions SET issues = ? WHERE number = ?",
            ('[{"severity": "block", "message": "estimate: bad hours"}]', n),
        )
        await db.commit()
    with pytest.raises(StateError, match="needs a note"):
        await store.approve(pid, n, "Sai", None)
    await store.approve(pid, n, "Sai", "Checked the hours by hand")
    v = await store.version(pid, n)
    assert v is not None and v.approval_note == "Checked the hours by hand"


async def test_failed_build_is_recorded_not_raised(setup: tuple[Store, ScopeBuilder, int]) -> None:
    store, builder, pid = setup
    builder.estimator.llm = team()
    builder.estimator.llm.handlers.pop(EstimateDraft)  # type: ignore[attr-defined]
    n = await builder.start(pid)
    await builder.build(pid, n)
    v = await store.version(pid, n)
    assert v is not None and v.status == "failed" and "KeyError" in (v.error or "")
    assert v.usage["calls"] > 0  # what it spent before failing is kept for the usage bar
    with pytest.raises(StateError, match="only a draft"):
        await store.approve(pid, n, "Sai", "x")


async def test_usage_reports_spend_and_a_failure_newer_than_any_success(
    setup: tuple[Store, ScopeBuilder, int],
) -> None:
    store, builder, pid = setup
    assert (await store.usage())["last_error"] is None
    await builder.build(pid, await builder.start(pid))
    ok = await store.usage()
    assert ok["calls"] == 1 and ok["last_success_at"] is not None and ok["last_error"] is None

    builder.estimator.llm = team()
    builder.estimator.llm.handlers.pop(EstimateDraft)  # type: ignore[attr-defined]
    await builder.build(pid, await builder.start(pid))
    failed = await store.usage()
    assert failed["calls"] == 2 and "KeyError" in failed["last_error"]["message"]
