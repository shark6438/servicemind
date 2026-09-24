"""The load contract, judged on its own. No services, no cluster, no model.

The grader is a pure function over recorded observations, so every rule the concurrency
tiers rely on can be pinned here, in CI, on every pull request, against no infrastructure.

Two of the tests below exist because the rule they pin was written the other way first and
was wrong. ``test_a_question_that_did_not_succeed_alone_may_rest_differently_under_load``
records what the P7.6.6 batch showed about ``waiting_review``; without it, the next person to
read the grader sees a comparison against a baseline and "simplifies" it back into "everything
must succeed", which is the rule that would have reported a hundred false failures. The other
is the baseline-invalid case: a batch whose unloaded runs did not rest must report BLOCKED,
not FAIL -- a platform that is already broken when idle is not evidence about load, and
recording it as a load failure would put a cause in the report that the observations do not
support.

The shipped ``plan.v1.json`` is loaded here too. A plan nobody validates is one that goes
stale quietly, and the failure that matters is the one where the file still parses: a tier
that lost its concurrency-1 baseline still loads, still grades, and still reports a
degradation ratio computed against the wrong denominator.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from servicemind.evaluation.load import (
    LOAD_SCHEMA_VERSION,
    LoadPlan,
    LoadTier,
    Workload,
    WorkloadCase,
    load_plan,
    parse_citations,
    plan_digest,
    resolve_workload,
    workload_digest,
)
from servicemind.evaluation.load_grader import (
    BatchHeader,
    RunObservation,
    TierTiming,
    Verdict,
    grade,
    observations_digest,
    outcome_summary,
    percentile,
    render_report,
)
from servicemind.evaluation.quality import load_quality_cases

REPO_ROOT = Path(__file__).resolve().parents[2]
LOAD = REPO_ROOT / "evaluation" / "load"
PLAN_PATH = LOAD / "plan.v1.json"
CASES_PATH = REPO_ROOT / "evaluation" / "quality" / "cases.v1.json"
TENANT = "22222222-2222-4222-8222-222222222222"

TWO_DOCS = ("KB-Q-VPN",)


def workload_of(size: int = 2) -> Workload:
    return Workload(
        source="evaluation/quality/cases.v1.json",
        kind="answerable",
        source_digest="workload-source-digest",
        cases=tuple(
            WorkloadCase(
                id=f"Q-{index:03d}",
                subject="globex-analyst-g3",
                ticket_ref="globex-quality-access",
                question=f"question {index}",
                expected_citations=TWO_DOCS,
            )
            for index in range(1, size + 1)
        ),
    )


def plan_of(*tiers: tuple[str, int, int]) -> LoadPlan:
    return LoadPlan(
        schema_version=LOAD_SCHEMA_VERSION,
        description="test plan",
        tenant_id=TENANT,
        workload_source="evaluation/quality/cases.v1.json",
        workload_kind="answerable",
        workload_limit=2,
        settle_budget_seconds=240.0,
        tiers=tuple(
            LoadTier(name=name, concurrency=concurrency, repeats=repeats)
            for name, concurrency, repeats in tiers
        ),
    )


DEFAULT_PLAN = plan_of(("tier-1", 1, 1), ("tier-5", 5, 2))


def observation(
    tier: str = "tier-1",
    concurrency: int = 1,
    case_id: str = "Q-001",
    repeat_index: int = 0,
    *,
    status: str | None = "succeeded",
    citations: tuple[str, ...] = TWO_DOCS,
    expected: tuple[str, ...] = TWO_DOCS,
    latency: float = 10.0,
    errors: tuple[str, ...] = (),
    unreadable: int = 0,
    revision: str | None = "rev-a",
    observed_at: datetime | None = None,
    plan_digest_value: str | None = None,
    workload_digest_value: str | None = None,
) -> RunObservation:
    plan = DEFAULT_PLAN
    workload = workload_of()
    return RunObservation(
        tier=tier,
        concurrency=concurrency,
        repeat_index=repeat_index,
        case_id=case_id,
        subject="globex-analyst-g3",
        run_id=f"{tier}-{case_id}-{repeat_index}",
        terminal_status=status,
        reviewer_decision="passed",
        citations=citations,
        expected_citations=expected,
        unreadable_citations=unreadable,
        latency_seconds=latency,
        errors=errors,
        observed_at=observed_at or datetime(2026, 9, 24, tzinfo=UTC),
        deployed_revision=revision,
        plan_digest=plan_digest_value or plan_digest(plan),
        workload_digest=workload_digest_value or workload_digest(workload),
    )


def complete_batch(
    plan: LoadPlan,
    workload: Workload,
    *,
    overrides: dict[tuple[str, str, int], dict[str, object]] | None = None,
) -> list[RunObservation]:
    """A batch in which every declared run happened and behaved. ``overrides`` breaks one."""
    overrides = overrides or {}
    rows: list[RunObservation] = []
    for tier in plan.tiers:
        for repeat in range(tier.repeats):
            for case in workload.cases:
                key = (tier.name, case.id, repeat)
                kwargs: dict[str, object] = {
                    "tier": tier.name,
                    "concurrency": tier.concurrency,
                    "case_id": case.id,
                    "repeat_index": repeat,
                    "citations": case.expected_citations,
                    "expected": case.expected_citations,
                }
                kwargs.update(overrides.get(key, {}))
                rows.append(observation(**kwargs))  # type: ignore[arg-type]
    return rows


def judge(plan: LoadPlan, workload: Workload, rows: list[RunObservation], **kwargs: object):
    return grade(
        plan,
        workload,
        rows,
        generated_at=datetime(2026, 9, 24, tzinfo=UTC),
        **kwargs,  # type: ignore[arg-type]
    )


# --- the shipped plan -------------------------------------------------------


def test_the_shipped_plan_is_valid_and_has_a_baseline() -> None:
    plan = load_plan(PLAN_PATH)
    assert [tier.concurrency for tier in plan.tiers] == sorted(
        tier.concurrency for tier in plan.tiers
    )
    assert any(tier.concurrency == 1 for tier in plan.tiers)
    assert plan.tenant_id == UUID(TENANT)


def test_the_shipped_plan_resolves_the_workload_it_declares() -> None:
    plan = load_plan(PLAN_PATH)
    workload = resolve_workload(plan, CASES_PATH)
    assert len(workload.cases) == plan.workload_limit
    assert all(case.expected_citations for case in workload.cases)
    # The slice is the first N of its kind in file order. Derived here from the case list
    # rather than trusted: a loader that sampled, sorted or de-duplicated would still return
    # the right number of cases, and the workload would no longer be the one the plan named.
    case_set = load_quality_cases(CASES_PATH)
    expected = [case.id for case in case_set.cases if case.kind.value == plan.workload_kind][
        : plan.workload_limit
    ]
    assert [case.id for case in workload.cases] == expected


def test_a_workload_short_of_its_declared_size_is_refused() -> None:
    plan = plan_of(("tier-1", 1, 1)).model_copy(update={"workload_limit": 500})
    with pytest.raises(ValueError, match="short of its declared size|holds"):
        resolve_workload(plan, CASES_PATH)


# --- the plan's own invariants ---------------------------------------------


def test_a_plan_without_a_single_run_tier_is_refused() -> None:
    with pytest.raises(ValidationError, match="concurrency 1"):
        plan_of(("tier-5", 5, 1))


def test_a_single_run_tier_that_repeats_is_refused() -> None:
    # Repeats at the baseline would give one question two unloaded outcomes, and "the same as
    # unloaded" would stop naming a single thing to compare against.
    with pytest.raises(ValidationError, match="repeats"):
        plan_of(("tier-1", 1, 3), ("tier-5", 5, 1))


def test_tiers_out_of_order_are_refused() -> None:
    with pytest.raises(ValidationError, match="ascending"):
        plan_of(("tier-10", 10, 1), ("tier-1", 1, 1))


def test_duplicate_tier_names_and_concurrencies_are_refused() -> None:
    with pytest.raises(ValidationError, match="duplicate tier names"):
        plan_of(("tier-1", 1, 1), ("tier-1", 5, 1))
    with pytest.raises(ValidationError, match="share a concurrency"):
        plan_of(("tier-1", 1, 1), ("tier-x", 1, 1))


# --- the four clauses -------------------------------------------------------


def test_a_batch_where_nothing_changed_passes() -> None:
    plan, workload = DEFAULT_PLAN, workload_of()
    outcome = judge(plan, workload, complete_batch(plan, workload))
    assert outcome.verdict is Verdict.PASS
    assert all(tier.failed == 0 for tier in outcome.tiers)
    assert all(tier.passed == tier.observed_runs for tier in outcome.tiers)


def test_a_question_that_succeeded_alone_must_succeed_under_load() -> None:
    plan, workload = DEFAULT_PLAN, workload_of()
    rows = complete_batch(
        plan,
        workload,
        overrides={("tier-5", "Q-001", 0): {"status": "failed"}},
    )
    outcome = judge(plan, workload, rows)
    assert outcome.verdict is Verdict.FAIL
    assert any("idle machine" in finding and "failed" in finding for finding in outcome.findings)


def test_a_question_that_did_not_succeed_alone_may_rest_differently_under_load() -> None:
    """The rule the P7.6.6 batch falsified, pinned so it is not "simplified" back.

    A question the supervisor hands to a human when the machine is idle is not a question
    that failed because the machine was busy. Comparing against the unloaded outcome is what
    makes the difference visible; "everything must succeed" is what hides it.
    """
    plan, workload = DEFAULT_PLAN, workload_of()
    rows = complete_batch(
        plan,
        workload,
        overrides={
            ("tier-1", "Q-001", 0): {"status": "waiting_review", "citations": ()},
            ("tier-5", "Q-001", 0): {"status": "waiting_review", "citations": ()},
        },
    )
    outcome = judge(plan, workload, rows)
    assert outcome.verdict is Verdict.PASS


def test_starting_to_succeed_under_load_is_not_a_failure() -> None:
    plan, workload = DEFAULT_PLAN, workload_of()
    rows = complete_batch(
        plan,
        workload,
        overrides={
            ("tier-1", "Q-001", 0): {"status": "waiting_review", "citations": ()},
            ("tier-5", "Q-001", 0): {"status": "succeeded"},
        },
    )
    assert judge(plan, workload, rows).verdict is Verdict.PASS


def test_a_run_that_never_settled_is_a_failure() -> None:
    plan, workload = DEFAULT_PLAN, workload_of()
    rows = complete_batch(
        plan,
        workload,
        overrides={("tier-5", "Q-002", 1): {"status": None, "latency": 240.0}},
    )
    outcome = judge(plan, workload, rows)
    assert outcome.verdict is Verdict.FAIL
    assert any("never settled" in finding for finding in outcome.findings)


def test_a_success_that_lost_its_citation_is_a_failure() -> None:
    plan, workload = DEFAULT_PLAN, workload_of()
    rows = complete_batch(
        plan,
        workload,
        overrides={("tier-5", "Q-001", 0): {"citations": ("KB-Q-SOMETHING-ELSE",)}},
    )
    outcome = judge(plan, workload, rows)
    assert outcome.verdict is Verdict.FAIL
    assert any("without citing" in finding for finding in outcome.findings)


def test_a_citation_clause_is_not_applied_to_a_run_that_did_not_succeed() -> None:
    # The question was not answered, so there is nothing for a citation clause to be about.
    # Failing it twice would double-count one outcome and misname the second one.
    plan, workload = DEFAULT_PLAN, workload_of()
    rows = complete_batch(
        plan,
        workload,
        overrides={
            ("tier-1", "Q-001", 0): {"status": "waiting_review", "citations": ()},
            ("tier-5", "Q-001", 0): {"status": "waiting_review", "citations": ()},
        },
    )
    outcome = judge(plan, workload, rows)
    assert not any("without citing" in finding for finding in outcome.findings)


def test_an_unreadable_citation_row_is_a_failure() -> None:
    plan, workload = DEFAULT_PLAN, workload_of()
    rows = complete_batch(plan, workload, overrides={("tier-5", "Q-001", 0): {"unreadable": 2}})
    outcome = judge(plan, workload, rows)
    assert outcome.verdict is Verdict.FAIL
    assert any("would not parse" in finding for finding in outcome.findings)
    # The same defect at the baseline blocks instead: it makes the reference point unusable
    # rather than being a change that load caused.
    baseline_rows = complete_batch(
        plan, workload, overrides={("tier-1", "Q-001", 0): {"unreadable": 2}}
    )
    blocked = judge(plan, workload, baseline_rows)
    assert blocked.verdict is Verdict.BLOCKED


# --- observation coverage ---------------------------------------------------


def test_a_declared_run_with_no_observation_blocks_the_tier() -> None:
    plan, workload = DEFAULT_PLAN, workload_of()
    rows = [
        row
        for row in complete_batch(plan, workload)
        if row.tier != "tier-5" or row.case_id != "Q-002"
    ]
    outcome = judge(plan, workload, rows)
    assert outcome.verdict is Verdict.BLOCKED
    assert outcome.tier("tier-5").missing == ("Q-002#0", "Q-002#1")
    assert any("not measured" in blocker for blocker in outcome.blockers)


def test_a_baseline_that_did_not_rest_blocks_rather_than_fails() -> None:
    """Nothing observed under load can be attributed to load if the baseline is broken."""
    plan, workload = DEFAULT_PLAN, workload_of()
    rows = complete_batch(
        plan,
        workload,
        overrides={("tier-1", "Q-002", 0): {"status": None, "latency": 240.0}},
    )
    outcome = judge(plan, workload, rows)
    assert outcome.verdict is Verdict.BLOCKED
    assert any("not a usable baseline" in blocker for blocker in outcome.blockers)
    assert any("never settled" in blocker for blocker in outcome.blockers)


def test_an_observation_for_an_undeclared_tier_is_a_failure() -> None:
    plan, workload = DEFAULT_PLAN, workload_of()
    rows = complete_batch(plan, workload)
    rows.append(observation(tier="tier-99", concurrency=99, case_id="Q-001"))
    outcome = judge(plan, workload, rows)
    assert outcome.verdict is Verdict.FAIL
    assert any("does not declare" in finding for finding in outcome.findings)


def test_a_batch_recorded_across_two_revisions_fails() -> None:
    plan, workload = DEFAULT_PLAN, workload_of()
    rows = complete_batch(plan, workload, overrides={("tier-5", "Q-001", 0): {"revision": "rev-b"}})
    outcome = judge(plan, workload, rows)
    assert outcome.verdict is Verdict.FAIL
    assert outcome.deployed_revisions == ("rev-a", "rev-b")


# --- the latency reading ----------------------------------------------------


def test_the_percentile_is_a_real_run_not_an_interpolation() -> None:
    # Twenty values, 10.0 to 105.0 in steps of five. Nearest-rank p95 is the 19th, 100.0.
    # Interpolating at rank 0.95 * (20 - 1) = 18.05 would report 100.25 -- a duration no run
    # in the list ever took, produced precisely in the tail a reader is looking at.
    values = [float(value) for value in range(10, 110, 5)]
    assert percentile(values, 0.95) == 100.0
    assert percentile(values, 1.0) == 105.0
    assert percentile([7.0], 0.5) == 7.0


def test_an_unrecorded_latency_does_not_enter_the_percentiles() -> None:
    # -1.0 is "not recorded". If it were averaged in as zero it would report the batch as
    # faster than it was, and it would do it worst in exactly the batches that went wrong.
    plan, workload = DEFAULT_PLAN, workload_of()
    rows = complete_batch(plan, workload, overrides={("tier-1", "Q-001", 0): {"latency": -1.0}})
    outcome = judge(plan, workload, rows)
    reading = outcome.tier("tier-1").latency
    assert reading is not None
    assert reading.count == 1
    assert reading.median_seconds == 10.0


def test_the_degradation_ratio_is_relative_to_the_single_run_tier() -> None:
    plan, workload = DEFAULT_PLAN, workload_of()
    rows = complete_batch(
        plan,
        workload,
        overrides={
            ("tier-1", "Q-001", 0): {"latency": 10.0},
            ("tier-1", "Q-002", 0): {"latency": 10.0},
            ("tier-5", "Q-001", 0): {"latency": 30.0},
            ("tier-5", "Q-002", 0): {"latency": 30.0},
        },
    )
    outcome = judge(plan, workload, rows)
    assert outcome.degradation("tier-5") == pytest.approx(3.0)
    assert outcome.degradation("tier-1") == pytest.approx(1.0)


def test_latency_is_reported_and_never_decides_the_verdict() -> None:
    # Ten minutes a run, against a settle budget of four. Slow is not a clause; wedged is,
    # and this run rested. The number reaches the report and stops there.
    plan, workload = DEFAULT_PLAN, workload_of()
    rows = complete_batch(plan, workload, overrides={("tier-5", "Q-001", 0): {"latency": 600.0}})
    outcome = judge(plan, workload, rows)
    assert outcome.verdict is Verdict.PASS
    assert outcome.tier("tier-5").latency is not None
    assert outcome.tier("tier-5").latency.max_seconds == 600.0


def test_the_tier_rate_divides_the_declared_runs() -> None:
    plan, workload = DEFAULT_PLAN, workload_of()
    batch = BatchHeader(
        plan_digest=plan_digest(plan),
        workload_digest=workload_digest(workload),
        tiers=(
            TierTiming(
                name="tier-1",
                started_at=datetime(2026, 9, 24, 0, 0, tzinfo=UTC),
                finished_at=datetime(2026, 9, 24, 0, 1, tzinfo=UTC),
            ),
        ),
    )
    outcome = judge(plan, workload, complete_batch(plan, workload), batch=batch)
    reading = outcome.tier("tier-1")
    assert reading.wall_seconds == 60.0
    assert reading.runs_per_minute == pytest.approx(2.0)


def test_a_missing_batch_header_leaves_the_rate_unknown_rather_than_zero() -> None:
    plan, workload = DEFAULT_PLAN, workload_of()
    outcome = judge(plan, workload, complete_batch(plan, workload))
    assert outcome.tier("tier-1").runs_per_minute is None
    assert outcome_summary(outcome)["tiers"][0]["runs_per_minute"] is None


# --- digests and hand-edited files -----------------------------------------


def test_the_observation_digest_does_not_depend_on_who_is_asking() -> None:
    plan, workload = DEFAULT_PLAN, workload_of()
    rows = complete_batch(plan, workload)
    assert observations_digest(rows) == observations_digest(list(reversed(rows)))


def test_a_hand_edited_replay_is_coerced_rather_than_raising() -> None:
    assert parse_citations("KB-Q-VPN") == ("KB-Q-VPN",)
    assert parse_citations(["a", "b"]) == ("a", "b")
    assert parse_citations(None) == ()
    row = RunObservation.model_validate(
        {
            "tier": "tier-1",
            "concurrency": 1,
            "repeat_index": 0,
            "case_id": "Q-001",
            "terminal_status": "succeeded",
            "citations": "KB-Q-VPN",
            "expected_citations": ["KB-Q-VPN"],
            "errors": "one error",
            "plan_digest": "p",
            "workload_digest": "w",
        }
    )
    assert row.citations == ("KB-Q-VPN",)
    assert row.errors == ("one error",)


# --- the report -------------------------------------------------------------


def test_the_report_states_the_criterion_and_the_limits() -> None:
    plan, workload = DEFAULT_PLAN, workload_of()
    outcome = judge(plan, workload, complete_batch(plan, workload))
    report = render_report(outcome)
    # The two claims a reader would otherwise have to take on trust: that this batch is not
    # judging answer quality, and that the latency columns decide nothing.
    assert "不判定答案质量" in report
    assert "时延只报告，不断言" in report
    assert "p95/基线" in report
    # Both digests are printed, because a report that cannot be tied to the plan and workload
    # it graded is a report nobody can tell has gone stale.
    assert outcome.plan_digest in report
    assert outcome.workload_digest in report


def test_the_summary_carries_the_numbers_a_caller_reports_on() -> None:
    plan, workload = DEFAULT_PLAN, workload_of()
    summary = outcome_summary(judge(plan, workload, complete_batch(plan, workload)))
    assert summary["verdict"] == "PASS"
    assert summary["baseline_tier"] == "tier-1"
    assert [tier["name"] for tier in summary["tiers"]] == ["tier-1", "tier-5"]
    assert summary["tiers"][1]["p95_over_baseline_tier"] == pytest.approx(1.0)
