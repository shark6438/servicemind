import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from core import get_model, settings
from servicemind.domain.supervisor import SupervisorDecision
from servicemind.runtime.structured import structured_output


class SupervisorAgent:
    """Control-plane agent: proposes the next transition but owns no domain tools."""

    async def decide(
        self,
        state_view: dict[str, Any],
        *,
        policy_feedback: str | None = None,
    ) -> SupervisorDecision:
        model = get_model(settings.DEFAULT_MODEL)
        runnable = structured_output(model, SupervisorDecision)
        feedback = (
            f"\nYour previous decision was rejected by policy: {policy_feedback}"
            if policy_feedback
            else ""
        )
        result = await runnable.ainvoke(
            [
                SystemMessage(
                    content=(
                        "You are ServiceMind Supervisor, the control plane for an enterprise "
                        "ITSM workflow. You never call GLPI, retrieve documents, analyze the "
                        "incident, approve actions, or execute tools. Select exactly one next "
                        "control action from the supplied legal_actions. For DISPATCH, select "
                        "one or more ready evidence task IDs up to max_parallel. For ANALYZE or "
                        "REVIEW, select exactly one matching ready task ID. For every other "
                        "action selected_task_ids must be empty. Use current task status, "
                        "review feedback and remaining budget. Never invent task IDs. Return "
                        "Prefer progressing ready work. Use REPLAN only for failure, conflict, "
                        "or explicit reviewer feedback; use ESCALATE only when safe progress is "
                        "not possible. "
                        "JSON matching SupervisorDecision. rationale_summary must be an auditable "
                        "business control rationale, not hidden chain-of-thought."
                        f"{feedback}\nRequired JSON Schema:\n"
                        f"{json.dumps(SupervisorDecision.model_json_schema())}"
                    )
                ),
                HumanMessage(content=json.dumps(state_view, ensure_ascii=False)),
            ]
        )
        return SupervisorDecision.model_validate(result)


supervisor_agent = SupervisorAgent()
