from datetime import UTC, datetime
from enum import StrEnum

from servicemind.domain.task import Budget, BudgetSnapshot, RuntimeControl


class BudgetExceededCode(StrEnum):
    BUDGET_EXCEEDED = "budget_exceeded"
    REPLAN_LIMIT_EXCEEDED = "replan_limit_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    LOOP_GUARD_TRIGGERED = "loop_guard_triggered"


class BudgetExceeded(RuntimeError):
    def __init__(self, code: BudgetExceededCode) -> None:
        super().__init__(code.value)
        self.code = code


class BudgetController:
    def check(self, budget: Budget, control: RuntimeControl) -> None:
        if datetime.now(UTC) >= budget.deadline:
            raise BudgetExceeded(BudgetExceededCode.DEADLINE_EXCEEDED)
        if control.total_steps >= budget.max_steps:
            raise BudgetExceeded(BudgetExceededCode.BUDGET_EXCEEDED)
        if control.replan_count > budget.max_replans:
            raise BudgetExceeded(BudgetExceededCode.REPLAN_LIMIT_EXCEEDED)
        if control.model_call_count > budget.max_model_calls:
            raise BudgetExceeded(BudgetExceededCode.BUDGET_EXCEEDED)
        if control.tool_call_count > budget.max_tool_calls:
            raise BudgetExceeded(BudgetExceededCode.BUDGET_EXCEEDED)
        if control.consecutive_failures >= 3:
            raise BudgetExceeded(BudgetExceededCode.LOOP_GUARD_TRIGGERED)

    def snapshot(self, budget: Budget, control: RuntimeControl) -> BudgetSnapshot:
        return BudgetSnapshot(
            remaining_steps=max(budget.max_steps - control.total_steps, 0),
            remaining_replans=max(budget.max_replans - control.replan_count, 0),
            remaining_model_calls=max(
                budget.max_model_calls - control.model_call_count, 0
            ),
            remaining_tool_calls=max(
                budget.max_tool_calls - control.tool_call_count, 0
            ),
            deadline=budget.deadline,
        )


budget_controller = BudgetController()
