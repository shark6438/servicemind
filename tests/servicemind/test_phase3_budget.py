from datetime import UTC, datetime, timedelta

import pytest

from servicemind.domain.task import Budget, RuntimeControl
from servicemind.orchestration.budget import (
    BudgetController,
    BudgetExceeded,
    BudgetExceededCode,
)


def test_budget_snapshot_tracks_all_enforced_counters() -> None:
    budget = Budget(
        max_steps=10,
        max_replans=2,
        max_model_calls=5,
        max_tool_calls=6,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )
    control = RuntimeControl(total_steps=4, replan_count=1, model_call_count=2, tool_call_count=3)
    snapshot = BudgetController().snapshot(budget, control)
    assert snapshot.remaining_steps == 6
    assert snapshot.remaining_replans == 1
    assert snapshot.remaining_model_calls == 3
    assert snapshot.remaining_tool_calls == 3


@pytest.mark.parametrize(
    ("budget", "control", "expected"),
    [
        (
            Budget(max_steps=2, deadline=datetime.now(UTC) + timedelta(hours=1)),
            RuntimeControl(total_steps=2),
            BudgetExceededCode.BUDGET_EXCEEDED,
        ),
        (
            Budget(max_replans=1, deadline=datetime.now(UTC) + timedelta(hours=1)),
            RuntimeControl(replan_count=2),
            BudgetExceededCode.REPLAN_LIMIT_EXCEEDED,
        ),
        (
            Budget(deadline=datetime.now(UTC) - timedelta(seconds=1)),
            RuntimeControl(),
            BudgetExceededCode.DEADLINE_EXCEEDED,
        ),
        (
            Budget(deadline=datetime.now(UTC) + timedelta(hours=1)),
            RuntimeControl(consecutive_failures=3),
            BudgetExceededCode.LOOP_GUARD_TRIGGERED,
        ),
        (
            Budget(
                max_model_calls=2,
                deadline=datetime.now(UTC) + timedelta(hours=1),
            ),
            RuntimeControl(model_call_count=2),
            BudgetExceededCode.BUDGET_EXCEEDED,
        ),
        (
            Budget(
                max_tool_calls=3,
                deadline=datetime.now(UTC) + timedelta(hours=1),
            ),
            RuntimeControl(tool_call_count=3),
            BudgetExceededCode.BUDGET_EXCEEDED,
        ),
    ],
)
def test_budget_limits_terminate_deterministically(
    budget: Budget, control: RuntimeControl, expected: BudgetExceededCode
) -> None:
    with pytest.raises(BudgetExceeded) as error:
        BudgetController().check(budget, control)
    assert error.value.code is expected
