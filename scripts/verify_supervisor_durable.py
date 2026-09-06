"""Verify the real Supervisor graph across two Python processes and PostgreSQL."""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

from memory import initialize_database
from servicemind.agents.action import ActionAgent
from servicemind.domain.analysis import AnalysisResult
from servicemind.domain.evidence import Evidence, EvidenceSourceType
from servicemind.domain.models import ExecutionResult
from servicemind.domain.review import ReviewDecision, ReviewResult, RiskLevel
from servicemind.domain.supervisor import SupervisorAction, SupervisorDecision
from servicemind.domain.task import AgentName, Budget, Task, TaskPlan
from servicemind.orchestration.dispatcher import TaskDispatcher
from servicemind.orchestration.router import FastPathRouter
from servicemind.orchestration.supervisor_policy import SupervisorPolicy
from servicemind.orchestration.supervisor_workflow import (
    SupervisorRuntimeServices,
    build_supervisor_graph,
)

TENANT = UUID("11111111-1111-4111-8111-111111111111")


class NoopRepository:
    def __init__(self, tenant_id: UUID) -> None:
        assert tenant_id == TENANT

    async def update_run(self, run_id, status, *, result=None, error=None):
        return SimpleNamespace(id=run_id, status=status.value, result=result, error=error)

    async def append_event(self, run_id, event_type, payload):
        return SimpleNamespace()

    async def save_action_intent(self, **kwargs):
        return SimpleNamespace(id=uuid4(), **kwargs)


def evidence(source: EvidenceSourceType, resource: str, content: str) -> Evidence:
    return Evidence.create(
        tenant_id=TENANT,
        source_type=source,
        source_ref=f"{source.value}://durable/{resource}",
        resource_type=resource,
        resource_id="2",
        content=content,
        provider="supervisor-durable-verification",
        retrieval_method="fixture",
        metadata=(
            {"ticket_facts": {"id": 2, "impact": 3, "urgency": 4, "priority": 4}}
            if resource == "ticket"
            else {}
        ),
    )


class CountingData:
    calls = 0

    async def get_ticket_evidence(self, context, ticket_id):
        type(self).calls += 1
        return [
            evidence(EvidenceSourceType.GLPI, "ticket", "VPN MFA facts"),
            evidence(
                EvidenceSourceType.GLPI,
                "support_group",
                "GLPI support group: Network Team",
            ),
        ]


class CountingKnowledge:
    calls = 0

    async def retrieve(self, *, tenant_id, query, retrieval_round=0):
        type(self).calls += 1
        return [
            evidence(
                EvidenceSourceType.KNOWLEDGE,
                "runbook",
                "Network Team owns VPN incidents",
            )
        ]


class FailingAnalysis:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.calls = 0

    async def analyze_evidence(self, joined, goal, *, request_write, ticket_id):
        self.calls += 1
        if self.fail:
            raise RuntimeError("intentional supervisor durable failure")
        return AnalysisResult(
            classification="network/vpn",
            impact=3,
            urgency=4,
            priority=4,
            recommended_group="Network Team",
            recurring_incident=False,
            problem_recommendation="Collect recurrence evidence.",
            change_recommendation="No change supported.",
            reasoning_summary="Evidence supports Network Team.",
            evidence_refs=joined.evidence_refs,
            confidence=0.8,
            source="durable-verification",
        )


class PassingReviewer:
    async def review(self, *, analysis, evidence, **kwargs):
        return ReviewResult(
            decision=ReviewDecision.PASSED,
            risk_level=RiskLevel.LOW,
            feedback="Passed durable verification.",
            reviewed_evidence_refs=evidence.evidence_refs,
        )


class FixedDynamicPlanner:
    async def create_plan(self, *, goal, ticket_id, request_write, correction=None):
        due = datetime.now(UTC) + timedelta(minutes=5)
        budget = Budget(deadline=due)
        return TaskPlan(
            goal=goal,
            tasks=[
                Task(task_id="T1", agent=AgentName.DATA, task_type="get_ticket", deadline=due),
                Task(
                    task_id="T2",
                    agent=AgentName.KNOWLEDGE,
                    task_type="retrieve_knowledge",
                    deadline=due,
                ),
                Task(
                    task_id="T3",
                    agent=AgentName.ANALYSIS,
                    task_type="analyze_ticket",
                    depends_on=["T1", "T2"],
                    deadline=due,
                ),
                Task(
                    task_id="T4",
                    agent=AgentName.REVIEWER,
                    task_type="review_analysis",
                    depends_on=["T3"],
                    deadline=due,
                ),
            ],
            max_parallel=2,
            max_steps=budget.max_steps,
            max_replans=budget.max_replans,
            deadline=due,
            budget=budget,
        )

    async def revise_plan(self, **kwargs):
        return kwargs["previous"]


class StateSupervisor:
    async def decide(self, state_view, *, policy_feedback=None):
        legal = set(state_view["legal_actions"])
        for action in (
            SupervisorAction.PLAN,
            SupervisorAction.DISPATCH,
            SupervisorAction.JOIN_EVIDENCE,
            SupervisorAction.ANALYZE,
            SupervisorAction.REVIEW,
            SupervisorAction.FINALIZE,
        ):
            if action.value not in legal:
                continue
            selected = []
            if action is SupervisorAction.DISPATCH:
                selected = [item["task_id"] for item in state_view["ready_tasks"]]
            elif action in {SupervisorAction.ANALYZE, SupervisorAction.REVIEW}:
                target = "analysis" if action is SupervisorAction.ANALYZE else "reviewer"
                selected = [
                    item["task_id"]
                    for item in state_view["ready_tasks"]
                    if item["agent"] == target
                ][:1]
            return SupervisorDecision(
                action=action,
                selected_task_ids=selected,
                rationale_summary=f"Durable verifier selected {action.value}.",
                confidence=1,
            )
        raise AssertionError(legal)


class NoopExecutor:
    async def execute(self, context, intent):
        return ExecutionResult(
            tool_name="noop", followup_id=1, ticket_id=2, verified=True
        )


def initial(thread_id: str) -> dict:
    return {
        "run_id": str(uuid4()),
        "tenant_id": str(TENANT),
        "user_id": "durable-verifier",
        "username": "durable-verifier",
        "roles": ["viewer", "analyst"],
        "allowed_glpi_entity_ids": [1],
        "thread_id": thread_id,
        "ticket_id": 2,
        "raw_request": "Analyze VPN with the relevant runbook",
        "goal": "Analyze VPN with the relevant runbook",
        "request_write": False,
        "data_evidence": [],
        "knowledge_evidence": [],
        "branch_timings": [],
        "branch_errors": [],
        "task_completions": [],
        "trajectory": [],
        "control": {},
        "control_owner": "supervisor",
        "active_agent": "router",
        "evidence_dirty": False,
        "plan_revision": 0,
    }


async def run(mode: str, thread_id: str) -> None:
    analysis = FailingAnalysis(fail=mode == "fail")
    graph = build_supervisor_graph(
        SupervisorRuntimeServices(
            router=FastPathRouter(),
            supervisor=StateSupervisor(),
            planner=FixedDynamicPlanner(),
            policy=SupervisorPolicy(),
            dispatcher=TaskDispatcher(),
            data=CountingData(),
            knowledge=CountingKnowledge(),
            analysis=analysis,
            reviewer=PassingReviewer(),
            action=ActionAgent(),
            executor=NoopExecutor(),
            repository_factory=NoopRepository,
        )
    )
    config = {"configurable": {"thread_id": thread_id}}
    async with initialize_database() as saver:
        graph.checkpointer = saver
        if mode == "fail":
            try:
                await graph.ainvoke(initial(thread_id), config=config)
            except RuntimeError as exc:
                if "intentional supervisor durable" not in str(exc):
                    raise
            else:
                raise AssertionError("Failure phase unexpectedly completed")
            snapshot = await graph.aget_state(config)
            assert "data:T1" in snapshot.values["trajectory"]
            assert "knowledge:T2" in snapshot.values["trajectory"]
            assert CountingData.calls == CountingKnowledge.calls == 1
            print(f"PASS Supervisor durable phase A thread={thread_id}")
            return

        result = await graph.ainvoke(None, config=config)
        assert result["final_result"]["review"]["decision"] == "passed"
        assert CountingData.calls == CountingKnowledge.calls == 0
        assert analysis.calls == 1
        await saver.adelete_thread(thread_id)
        print(
            "PASS Supervisor durable phase B: resumed Analysis without repeating "
            f"completed branches thread={thread_id}"
        )


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in {"fail", "resume"}:
        raise SystemExit("usage: verify_supervisor_durable.py <fail|resume> <thread-id>")
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(run(sys.argv[1], sys.argv[2]), loop_factory=factory)


if __name__ == "__main__":
    main()
