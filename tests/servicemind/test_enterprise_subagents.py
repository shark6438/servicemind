import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import servicemind.agents.analysis as analysis_module
import servicemind.agents.data as data_module
import servicemind.agents.reviewer as reviewer_module
import servicemind.harness.executor as executor_module
import servicemind.tool_platform.providers as providers_module
from servicemind.agents.action import ActionAgent
from servicemind.agents.analysis import AnalysisAgent
from servicemind.agents.data import DataAcquisitionPlan, DataAgent, DataToolCall
from servicemind.agents.reviewer import ReviewerAgent, SemanticReview
from servicemind.domain.analysis import AnalysisClaim, AnalysisResult, AnalysisStatus
from servicemind.domain.evidence import (
    Evidence,
    EvidenceSourceType,
    join_evidence,
    self_authored_marker,
)
from servicemind.domain.handoff import HandoffEnvelope
from servicemind.domain.knowledge import Citation
from servicemind.domain.models import (
    ACTION_POLICY_VERSION_MAX,
    ACTION_PREVIEW_MAX,
    INTENT_VERSION_MAX,
    ActionIntent,
)
from servicemind.domain.review import ReviewDecision, ReviewResult, RiskLevel
from servicemind.domain.task import TICKET_ID_MAX, BudgetSnapshot
from servicemind.harness.executor import ControlledActionExecutor
from servicemind.runtime.contracts import AgentInvocationContext, AgentRunStatus
from servicemind.runtime.tool_gateway import DataToolName, TenantGlpiReadGateway
from servicemind.security.auth import TenantContext
from servicemind.tool_platform.audit import InMemoryToolAuditSink
from servicemind.tool_platform.catalog import (
    APPEND_FOLLOWUP_TOOL,
    build_glpi_registry,
)
from servicemind.tool_platform.gateway import ToolGateway
from servicemind.tool_platform.policy import DeterministicToolPolicy
from servicemind.tool_platform.providers import NativeGlpiProvider, ProductionGlpiBackend

TENANT = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT = UUID("22222222-2222-4222-8222-222222222222")


class FakeRunnable:
    def __init__(self, *results) -> None:
        self.results = list(results)

    async def ainvoke(self, messages):
        return self.results.pop(0)


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, DataToolName, int]] = []

    async def execute(self, *, invocation, tenant_context, tool_name, ticket_id):
        assert invocation.tenant_id == tenant_context.tenant_id
        self.calls.append((invocation.tenant_id, tool_name, ticket_id))
        if tool_name is DataToolName.GET_TICKET:
            return {"id": ticket_id, "name": "VPN outage", "priority": 3}
        if tool_name is DataToolName.LIST_GROUPS:
            return [{"id": 5, "name": "Network Team"}]
        return []


def invocation(
    task_id: str,
    *,
    tenant_id: UUID = TENANT,
    capabilities: frozenset[str] = frozenset(),
    max_model_calls: int = 2,
) -> AgentInvocationContext:
    return AgentInvocationContext(
        run_id=uuid4(),
        tenant_id=tenant_id,
        user_id="analyst-1",
        task_id=task_id,
        trace_id="trace-1",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        allowed_capabilities=capabilities,
        max_model_calls=max_model_calls,
        max_tool_calls=6,
        policy_version="test-policy-v2",
    )


def tenant_context(tenant_id: UUID = TENANT) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id="analyst-1",
        username="analyst",
        roles={"analyst"},
        allowed_glpi_entity_ids={1},
    )


def evidence(source: EvidenceSourceType, resource_type: str, content: str) -> Evidence:
    """One evidence fixture; KNOWLEDGE rows carry a self-consistent citation.

    The reviewer gate validates every KNOWLEDGE item's ``metadata["citation"]``
    (id == digest of its own document/parent/content and it anchors the row:
    source == provider, source_uri == source_ref, parent_chunk_id == resource_id).
    The builder reproduces the RAG service shape so fixture evidence passes.
    """
    if source is EvidenceSourceType.KNOWLEDGE:
        parent_chunk_id = uuid4()
        document_id = uuid4()
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        source_ref = f"knowledge://test/{resource_type}"
        citation = Citation(
            citation_id="cite-"
            + hashlib.sha256(
                json.dumps(
                    [str(document_id), str(parent_chunk_id), content_hash],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()[:16],
            document_id=document_id,
            parent_chunk_id=parent_chunk_id,
            source="test",
            source_uri=source_ref,
            source_record_id=f"test://{parent_chunk_id}",
            source_version="v1",
            content_hash=content_hash,
            title="Fixture runbook",
        )
        return Evidence.create(
            tenant_id=TENANT,
            source_type=source,
            source_ref=source_ref,
            resource_type=resource_type,
            resource_id=str(parent_chunk_id),
            content=content,
            provider="test",
            retrieval_method="fixture",
            metadata={"citation": citation.model_dump(mode="json")},
        )
    return Evidence.create(
        tenant_id=TENANT,
        source_type=source,
        source_ref=f"{source.value}://test/{resource_type}",
        resource_type=resource_type,
        resource_id="2",
        content=content,
        provider="test",
        retrieval_method="fixture",
    )


def model_analysis(refs: list[str]) -> AnalysisResult:
    return AnalysisResult(
        classification="network/vpn",
        impact=3,
        urgency=3,
        priority=3,
        recommended_group="Network Team",
        recurring_incident=False,
        problem_recommendation="Collect recurrence evidence.",
        change_recommendation="No change is supported.",
        reasoning_summary="Cited ticket and runbook support Network Team.",
        evidence_refs=refs,
        confidence=0.85,
        source="model",
        status=AnalysisStatus.MODEL,
        claims=[
            AnalysisClaim(
                claim_id="C1",
                claim_type="assignment_reason",
                statement="Network Team owns VPN incidents.",
                evidence_refs=refs,
                confidence=0.85,
            )
        ],
    )


@pytest.mark.asyncio
async def test_data_subgraph_compiles_invalid_model_plan_to_safe_minimum(monkeypatch) -> None:
    invalid = DataAcquisitionPlan(
        calls=[
            DataToolCall(
                tool_name=DataToolName.GET_TICKET,
                ticket_id=999,
                purpose="Attempt to change scope",
            )
        ],
        rationale_summary="Invalid model proposal",
    )
    monkeypatch.setattr(
        data_module, "structured_output", lambda model, schema: FakeRunnable(invalid)
    )
    gateway = FakeGateway()
    agent = DataAgent(gateway=gateway, model_factory=lambda: object())
    allowed = frozenset(item.value for item in DataToolName)
    result = await agent.run(
        invocation=invocation("T1", capabilities=allowed),
        tenant_context=tenant_context(),
        objective="Read the selected ticket",
        ticket_id=2,
    )
    assert result.status is AgentRunStatus.DEGRADED
    assert result.failure_code == "DATA_PLAN_DEGRADED"
    assert {(tool, ticket_id) for _, tool, ticket_id in gateway.calls} == {
        (DataToolName.GET_TICKET, 2),
        (DataToolName.LIST_GROUPS, 2),
    }
    assert result.metrics.tool_calls == 2


@pytest.mark.asyncio
async def test_tool_gateway_rejects_cross_tenant_before_resolving_credentials() -> None:
    with pytest.raises(PermissionError, match="tenant"):
        await TenantGlpiReadGateway().execute(
            invocation=invocation(
                "T1",
                capabilities=frozenset({DataToolName.GET_TICKET.value}),
            ),
            tenant_context=tenant_context(OTHER_TENANT),
            tool_name=DataToolName.GET_TICKET,
            ticket_id=2,
        )


@pytest.mark.asyncio
async def test_analysis_subgraph_revises_once_until_claims_are_grounded(monkeypatch) -> None:
    ticket = evidence(EvidenceSourceType.GLPI, "ticket", "VPN outage")
    group = evidence(EvidenceSourceType.GLPI, "support_group", "Network Team")
    runbook = evidence(EvidenceSourceType.KNOWLEDGE, "runbook", "Network Team owns VPN")
    joined = join_evidence(TENANT, [ticket, group, runbook])
    draft = model_analysis(joined.evidence_refs).model_copy(update={"claims": []})
    revised = model_analysis(joined.evidence_refs)
    runnable = FakeRunnable(draft, revised)
    monkeypatch.setattr(analysis_module, "structured_output", lambda model, schema: runnable)
    result = await AnalysisAgent(model_factory=lambda: object()).run(
        invocation=invocation("T2"),
        evidence=joined,
        goal="Analyze VPN incident",
        request_write=False,
        ticket_id=2,
    )
    assert result.status is AgentRunStatus.SUCCEEDED
    assert result.output.claims[0].evidence_refs == joined.evidence_refs
    assert result.metrics.model_calls == 2


@pytest.mark.asyncio
async def test_reviewer_semantic_judge_cannot_bypass_rule_gate(monkeypatch) -> None:
    ticket = evidence(EvidenceSourceType.GLPI, "ticket", "VPN outage")
    group = evidence(EvidenceSourceType.GLPI, "support_group", "Network Team")
    runbook = evidence(EvidenceSourceType.KNOWLEDGE, "runbook", "Network Team owns VPN")
    joined = join_evidence(TENANT, [ticket, group, runbook])
    semantic = SemanticReview(
        claims_supported=True,
        action_consistent=True,
        prompt_injection_detected=False,
        feedback="All claims are entailed by cited evidence.",
        confidence=0.9,
    )
    monkeypatch.setattr(
        reviewer_module, "structured_output", lambda model, schema: FakeRunnable(semantic)
    )
    result = await ReviewerAgent(enable_semantic_review=True, model_factory=lambda: object()).run(
        invocation=invocation("T3", max_model_calls=1),
        analysis=model_analysis(joined.evidence_refs),
        evidence=joined,
        request_write=False,
        retrieval_round=0,
        replan_count=0,
        max_replans=2,
    )
    assert result.output.decision is ReviewDecision.PASSED
    assert result.output.policy_version == "servicemind-review-policy-v5"
    assert result.metrics.model_calls == 1


def test_action_v2_hash_detects_post_review_mutation() -> None:
    ticket = evidence(EvidenceSourceType.GLPI, "ticket", "VPN outage")
    runbook = evidence(EvidenceSourceType.KNOWLEDGE, "runbook", "Network Team owns VPN")
    analysis = model_analysis([ticket.evidence_id, runbook.evidence_id])
    review = ReviewResult(
        decision=ReviewDecision.PASSED,
        risk_level=RiskLevel.LOW,
        feedback="Passed independent review.",
        reviewed_evidence_refs=analysis.evidence_refs,
    )
    envelope = HandoffEnvelope(
        run_id=uuid4(),
        tenant_id=TENANT,
        user_id="analyst-1",
        evidence_refs=analysis.evidence_refs,
        review_result=review,
        allowed_operations=["append_ticket_followup"],
        risk_level=RiskLevel.LOW,
        remaining_budget=BudgetSnapshot(
            remaining_steps=5,
            remaining_replans=1,
            remaining_model_calls=1,
            remaining_tool_calls=1,
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        ),
        idempotency_context={"run_id": "safe"},
        handoff_reason="Review passed",
    )
    intent = ActionAgent().propose_from_handoff(envelope, analysis, ticket_id=2)
    intent.verify_integrity()
    mutated = intent.model_copy(update={"arguments": {"content": "tampered"}})
    with pytest.raises(ValueError, match="integrity"):
        mutated.verify_integrity()


def reviewed_pair(
    *,
    refs: list[str],
    summary: str,
    feedback: str,
    group: str = "Network Team",
    policy_version: str = "servicemind-action-policy-v2",
) -> tuple[HandoffEnvelope, AnalysisResult]:
    """A passed review plus the analysis it covers, both fully validated.

    The envelope's digests are computed by its validator from these exact fields, so a
    caller that varies the sizes still gets a handoff the Action Agent will accept --
    unlike ``model_copy``, which would carry the previous sizes' digests.
    """
    analysis = model_analysis(refs).model_copy(
        update={"reasoning_summary": summary, "recommended_group": group}
    )
    review = ReviewResult(
        decision=ReviewDecision.PASSED,
        risk_level=RiskLevel.LOW,
        feedback=feedback,
        reviewed_evidence_refs=refs,
    )
    return HandoffEnvelope(
        run_id=uuid4(),
        tenant_id=TENANT,
        user_id="analyst-1",
        evidence_refs=refs,
        review_result=review,
        allowed_operations=["append_ticket_followup"],
        risk_level=RiskLevel.LOW,
        remaining_budget=BudgetSnapshot(
            remaining_steps=5,
            remaining_replans=1,
            remaining_model_calls=1,
            remaining_tool_calls=1,
            deadline=datetime.now(UTC) + timedelta(minutes=30),
        ),
        idempotency_context={"run_id": "safe"},
        handoff_reason="Review passed",
        policy_version=policy_version,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    ), analysis


def test_a_verbose_analysis_previews_with_a_marked_clip_instead_of_raising() -> None:
    """The preview is a join of three bounded fields, so it needs its own bound.

    Nothing bounded the composition: at the field ceilings it reached ~6600 characters
    against a 4000-character ``dry_run_preview``, and ``ActionIntent`` rejected it. The
    raise happened inside ``propose_from_handoff``, which ``action_node`` does not guard,
    so a verbose analysis ended the run in the graph's error path -- after a passed
    review, with nothing written to the ticket and no finalize record.
    """
    handoff, analysis = reviewed_pair(
        refs=[f"ev-{index:016x}" for index in range(100)],
        summary="s" * 2000,
        feedback="f" * 2000,
        group="g" * 200,
    )
    intent = ActionAgent().propose_from_handoff(handoff, analysis, ticket_id=2)

    preview = intent.dry_run_preview or ""
    assert len(preview) <= ACTION_PREVIEW_MAX
    # The clip is accounted for, not silent: the head plus the count is the whole string.
    head, _, marker = preview.partition("\n…[")
    assert marker.endswith(" characters elided]")
    assert len(head) + int(marker.removesuffix(" characters elided]")) == len(
        intent.arguments["content"]
    )
    # Only the preview is bounded. What the Action Agent would append to the ticket is
    # unchanged, so the operator is reading a prefix of the real thing.
    assert len(intent.arguments["content"]) > ACTION_PREVIEW_MAX
    assert intent.arguments["content"].startswith(head)


def _bare_intent(**overrides) -> ActionIntent:
    """The smallest legal intent, so one field can be pushed past its column alone."""
    fields = {
        "run_id": uuid4(),
        "action_type": "append_ticket_followup",
        "target_id": 2,
        "arguments": {"content": "x"},
        "action_hash": "0" * 64,
    }
    fields.update(overrides)
    return ActionIntent(**fields)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("intent_version", "v" * (INTENT_VERSION_MAX + 1)),
        ("policy_version", "p" * (ACTION_POLICY_VERSION_MAX + 1)),
        ("target_id", TICKET_ID_MAX + 1),
    ],
)
def test_an_intent_cannot_carry_a_value_its_column_cannot_hold(field: str, value: object) -> None:
    """``action_intents`` is the contract, and each of these is written into it verbatim.

    None of the three is derived or clipped on the way in: ``intent_version`` and
    ``policy_version`` arrive from the handoff and ``target_id`` from the request, so an
    over-long one produced a row the driver refused -- after the review had passed.
    """
    with pytest.raises(ValidationError, match=field):
        _bare_intent(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("intent_version", "v" * INTENT_VERSION_MAX),
        ("policy_version", "p" * ACTION_POLICY_VERSION_MAX),
        ("target_id", TICKET_ID_MAX),
    ],
)
def test_an_intent_at_each_ceiling_is_still_a_valid_intent(field: str, value: object) -> None:
    """The bounds reject only what they have to; the ceilings themselves stay legal."""
    assert getattr(_bare_intent(**{field: value}), field) == value


def test_a_handoff_cannot_forward_a_policy_version_the_intent_cannot_hold() -> None:
    """The handoff is where the value is chosen, so the handoff names the same bound."""
    with pytest.raises(ValidationError, match="policy_version"):
        reviewed_pair(
            refs=["ev-1"],
            summary="Cited evidence.",
            feedback="Passed independent review.",
            policy_version="p" * (ACTION_POLICY_VERSION_MAX + 1),
        )


def test_the_preview_ceiling_is_still_a_contract_not_only_a_habit() -> None:
    """The bound stays at the field, so a future producer cannot forget to clip."""
    with pytest.raises(ValueError, match="dry_run_preview"):
        ActionIntent(
            run_id=uuid4(),
            action_type="append_ticket_followup",
            target_id=2,
            arguments={"content": "x"},
            action_hash="0" * 64,
            dry_run_preview="x" * (ACTION_PREVIEW_MAX + 1),
        )


def test_a_preview_inside_its_ceiling_is_returned_whole() -> None:
    """The bound must not fire on the previews the Action Agent normally composes."""
    quiet_handoff, quiet = reviewed_pair(
        refs=[f"ev-{index:016x}" for index in range(3)],
        summary="Cited evidence.",
        feedback="Passed independent review.",
    )

    preview = ActionAgent().propose_from_handoff(quiet_handoff, quiet, ticket_id=2).dry_run_preview

    assert preview is not None and "elided" not in preview
    assert preview.startswith("ServiceMind reviewed analysis: Cited evidence.")
    assert preview.endswith("Reviewer: Passed independent review.")


@pytest.mark.asyncio
async def test_harness_compares_runtime_intent_with_persisted_approval(monkeypatch) -> None:
    ticket = evidence(EvidenceSourceType.GLPI, "ticket", "VPN outage")
    runbook = evidence(EvidenceSourceType.KNOWLEDGE, "runbook", "Network Team owns VPN")
    analysis = model_analysis([ticket.evidence_id, runbook.evidence_id])
    review = ReviewResult(
        decision=ReviewDecision.PASSED,
        risk_level=RiskLevel.LOW,
        feedback="Passed independent review.",
        reviewed_evidence_refs=analysis.evidence_refs,
    )
    envelope = HandoffEnvelope(
        run_id=uuid4(),
        tenant_id=TENANT,
        user_id="analyst-1",
        evidence_refs=analysis.evidence_refs,
        review_result=review,
        allowed_operations=["append_ticket_followup"],
        risk_level=RiskLevel.LOW,
        remaining_budget=BudgetSnapshot(
            remaining_steps=5,
            remaining_replans=1,
            remaining_model_calls=1,
            remaining_tool_calls=1,
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        ),
        idempotency_context={"run_id": "safe"},
        handoff_reason="Review passed",
    )
    intent = ActionAgent().propose_from_handoff(envelope, analysis, ticket_id=2)
    intent.id = uuid4()

    class FakeRepository:
        def __init__(self, tenant_id):
            assert tenant_id == TENANT

        async def get_action_intent(self, run_id):
            return SimpleNamespace(
                id=intent.id,
                action_hash="0" * 64,
                action_type=intent.action_type,
                target_id=intent.target_id,
                arguments=intent.arguments,
                intent_version=intent.intent_version,
                policy_version=intent.policy_version,
                review_digest=intent.review_digest,
                evidence_digest=intent.evidence_digest,
                evidence_refs=intent.evidence_refs,
                idempotency_context=intent.idempotency_context,
                requested_by=intent.requested_by,
                expires_at=intent.expires_at,
                status="approved",
            )

    monkeypatch.setattr(executor_module, "ServiceMindRepository", FakeRepository)
    with pytest.raises(PermissionError, match="persisted approved intent"):
        await ControlledActionExecutor().execute(tenant_context(), intent)


@pytest.mark.asyncio
async def test_the_read_gateway_carries_the_callers_group_scope_into_the_tool_call(
    monkeypatch,
) -> None:
    """The tool boundary is where a scope is most likely to go missing.

    ``TenantContext`` has carried the group coordinate all along, and everything behind
    the gateway reads it -- the registry's visibility rule, the policy engine, the graph
    read that builds its own principal. ``ToolCall`` did not, so all of them saw the
    empty set, and empty means *denial* on that axis: a group-scoped resource was
    unreachable through the platform while being reachable through the hand-built
    principal beside it. A field carried on one side of a boundary and dropped on the
    other is the shape of the defect, not an incidental omission.
    """
    import servicemind.runtime.tool_gateway as gateway_module
    from servicemind.tool_platform.contracts import ToolCall

    captured: list[ToolCall] = []

    class _CapturingGateway:
        async def execute(self, call: ToolCall):
            captured.append(call)
            return SimpleNamespace(output={"id": 2})

    context = tenant_context()
    context.allowed_glpi_group_ids = {3, 4}
    monkeypatch.setattr(gateway_module.settings, "SERVICEMIND_TOOL_PLATFORM_ENABLED", True)
    monkeypatch.setattr(gateway_module, "build_tool_gateway", _CapturingGateway)

    await TenantGlpiReadGateway().execute(
        invocation=invocation("T1", capabilities=frozenset({DataToolName.GET_TICKET.value})),
        tenant_context=context,
        tool_name=DataToolName.GET_TICKET,
        ticket_id=2,
    )

    assert len(captured) == 1
    assert captured[0].group_ids == frozenset({3, 4})
    assert captured[0].entity_ids == frozenset({1})


# ---------------------------------------------------------------- executor read-back
#
# D11: the executor verified its own write with
# ``marker in html_to_text(followup.content)`` -- "this row exists", not "this row says
# what was approved". Everything below fails on the old check and passes on the new one,
# and the passing case is here so the failures cannot be satisfied by a check that simply
# always says no.


class _ExecutorRepository:
    """The slice of ``ServiceMindRepository`` the executor touches, in memory."""

    def __init__(self, intent, *, already_completed=False, status="approved") -> None:
        self.intent = intent
        self.status = status
        self.statuses: list[str] = []
        self.completed: list[dict] = []
        self.audits: list[str] = []
        self._record = SimpleNamespace(
            id=uuid4(),
            completed=already_completed,
            result={"followup_id": 7} if already_completed else None,
        )

    async def get_action_intent(self, run_id):
        assert run_id == self.intent.run_id
        return SimpleNamespace(
            id=self.intent.id,
            action_hash=self.intent.action_hash,
            action_type=self.intent.action_type,
            target_id=self.intent.target_id,
            arguments=self.intent.arguments,
            intent_version=self.intent.intent_version,
            policy_version=self.intent.policy_version,
            review_digest=self.intent.review_digest,
            evidence_digest=self.intent.evidence_digest,
            evidence_refs=self.intent.evidence_refs,
            idempotency_context=self.intent.idempotency_context,
            requested_by=self.intent.requested_by,
            expires_at=self.intent.expires_at,
            status=self.status,
        )

    async def claim_idempotency(self, *, idempotency_key, action_hash):
        return self._record, False

    def action_lock(self, idempotency_key):
        class _Lock:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        return _Lock()

    async def get_idempotency(self, idempotency_key):
        return self._record

    async def complete_idempotency(self, record_id, result):
        self.completed.append(result)

    async def update_action_status(self, action_intent_id, status):
        # Written *and* recorded, because the row this fake answers ``get_action_intent``
        # with is read again later in the same call -- by the provider, to decide whether
        # the intent it is asked to write is approved. A fake that only appended to
        # ``statuses`` answered every read with the status it was constructed with, so the
        # executor's ``EXECUTING`` transition was invisible to the one component that read
        # that row after it happened.
        self.statuses.append(status.value)
        self.status = str(getattr(status, "value", status))

    async def audit(self, **kwargs):
        self.audits.append(kwargs["event_type"])


class _UnquestioningVerifier(NativeGlpiProvider):
    """A provider whose read-back agrees with whatever it is asked about.

    The provider has two independent refusals for a mismatched row: the reconcile
    comparison before the write, and the read-back after it. Having both is right, and it
    makes each one hard to test -- either alone refuses the case, so a test of one passes
    with the other deleted, which is how a removed guard survives a suite. Turning the
    second off is what makes an assertion about the first an assertion about the first.
    """

    async def verify(self, *args, **kwargs) -> bool:
        return True


class _FakeGlpiClient:
    """Stands in for GLPI, storing the body as markup the way GLPI does.

    ``write_returns`` models a backend that stores something other than what it was sent
    -- the shape a field limit or a careless editor produces. It is what the row *is*,
    not merely what the write call answers with, which is the point: a read-back that
    trusts the write's own response cannot see this backend at all.

    ``conceals_stored`` models the other direction: a backend that accepted the write and
    has no row to show for it. A write is only ever on a 2xx from GLPI, and a 2xx is not
    a row, so this is the shape a dropped write or a lagging replica produces.
    """

    def __init__(
        self,
        *,
        stored: str = "",
        write_returns: str | None = None,
        truncate_to_marker: bool = False,
        conceals_stored: bool = False,
    ) -> None:
        self.stored = stored
        self.write_returns = write_returns
        self.truncate_to_marker = truncate_to_marker
        self.conceals_stored = conceals_stored
        self.written: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @staticmethod
    def _as_markup(text: str) -> str:
        return f"<div>{text.replace(chr(10), '<br />')}</div>"

    async def list_ticket_followups(self, ticket_id):
        if self.conceals_stored:
            return []
        return [
            SimpleNamespace(id=7, content=self._as_markup(self.stored)),
        ]

    async def append_ticket_followup(self, ticket_id, content, *, is_private=True):
        self.written.append(content)
        returned = content if self.write_returns is None else self.write_returns
        if self.truncate_to_marker:
            # A field limit that cut everything before the last line: the marker survives,
            # so every check that looked for it passed while the body was gone.
            returned = content.rsplit(chr(10), 1)[-1]
        self.stored = returned
        return SimpleNamespace(id=7, content=self._as_markup(returned))


def _approved_intent():
    quiet_handoff, quiet = reviewed_pair(
        refs=[f"ev-{index:016x}" for index in range(3)],
        summary="Cited evidence.",
        feedback="Passed independent review.",
    )
    intent = ActionAgent().propose_from_handoff(quiet_handoff, quiet, ticket_id=2)
    intent.id = uuid4()
    return intent


def _executor_harness(
    monkeypatch,
    *,
    write_returns: str | None = None,
    truncate_to_marker: bool = False,
    conceals_stored: bool = False,
    stored: str = "",
    status: str = "approved",
    verify: bool = True,
):
    """The executor over a *real* gateway: only GLPI and the repository row are faked.

    The gateway, the registry, the policy engine and the provider are the production
    ones. Faking the gateway would test that the harness calls something, which is not
    the claim -- the claim is that a write submitted from the harness is decided and
    recorded like every other call, so the object that decides and records it has to be
    the object under test.
    """
    intent = _approved_intent()
    repository = _ExecutorRepository(intent, status=status)
    client = _FakeGlpiClient(
        stored=stored,
        write_returns=write_returns,
        truncate_to_marker=truncate_to_marker,
        conceals_stored=conceals_stored,
    )
    audit = InMemoryToolAuditSink()

    for module in (executor_module, providers_module):
        monkeypatch.setattr(module, "ServiceMindRepository", lambda _: repository)
    monkeypatch.setattr(providers_module, "resolve_glpi_config", _resolve_config)
    monkeypatch.setattr(providers_module, "GlpiClient", lambda _: client)
    monkeypatch.setattr(
        executor_module,
        "build_tool_gateway",
        lambda: ToolGateway(
            registry=build_glpi_registry("native_glpi"),
            policy=DeterministicToolPolicy(),
            providers={"native_glpi": NativeGlpiProvider() if verify else _UnquestioningVerifier()},
            audit=audit,
        ),
    )
    return intent, repository, client, audit


async def _resolve_config(_context):
    return SimpleNamespace()


@pytest.mark.asyncio
async def test_a_write_whose_stored_body_was_truncated_is_not_verified(monkeypatch) -> None:
    """The defect: a truncated followup kept the marker, so the run reported success.

    The backend stores a row carrying the marker and nothing else -- the shape a field
    limit or a careless editor produces -- while answering the write with the body it was
    given. The old check answered "the row is there"; the question the platform needs
    answered is "the row says what was approved", and a run that reports ``verified=True``
    for this one has no evidence its write landed.
    """
    intent, repository, _, _ = _executor_harness(monkeypatch, truncate_to_marker=True)

    with pytest.raises(RuntimeError, match="not the approved content"):
        await ControlledActionExecutor().execute(tenant_context(), intent)

    assert repository.statuses[-1] == "failed"
    assert repository.completed == [], "a failed write must not be recorded as complete"


@pytest.mark.asyncio
async def test_a_write_whose_stored_body_matches_is_verified(monkeypatch) -> None:
    """The ceiling on the fix: a faithful write still verifies.

    GLPI stores the body it was given, reflowed into markup -- a newline becomes a tag.
    A comparison stricter than the platform's own text normalization would fail here, so
    this is what keeps the new check from being "reject everything".
    """
    intent, repository, _, _ = _executor_harness(monkeypatch)

    result = await ControlledActionExecutor().execute(tenant_context(), intent)

    assert result.verified is True
    assert repository.statuses[-1] == "succeeded"
    assert repository.audits == ["glpi.followup.created"]


@pytest.mark.asyncio
async def test_the_write_leaves_a_policy_decision_and_an_invocation_behind(monkeypatch) -> None:
    """D9: the platform's one side effect is decided and recorded like every other call.

    Before this, the harness reached GLPI directly: no registration, no policy decision,
    no invocation row. The run's own ledger said a followup was created, and nothing in
    the tool-governance tables said a tool had been called -- so the write could not be
    reconciled against the platform's record of what it is allowed to do, and the tables
    that exist to answer "what did this tenant's agents run" could not answer it.

    Asserted as an equality rather than a membership because the count is the claim: one
    write produces exactly one decision and one invocation, and a second of either would
    mean the call was replayed.
    """
    intent, _, client, audit = _executor_harness(monkeypatch)

    await ControlledActionExecutor().execute(tenant_context(), intent)

    assert len(audit.policy) == 1
    definition, call, decision = audit.policy[0]
    assert definition.name == APPEND_FOLLOWUP_TOOL
    assert decision.allow is True
    assert decision.requires_approval is True
    assert call.approval_ref == f"action-intent://{intent.id}"
    assert call.approval_binding == call.approval_digest

    assert len(audit.invocations) == 1
    invocation_record = audit.invocations[0]
    assert invocation_record.status == "succeeded"
    assert invocation_record.verified is True
    assert invocation_record.tool_name == APPEND_FOLLOWUP_TOOL
    assert invocation_record.argument_hash == call.argument_hash
    # One call at the GLPI boundary, and the marker was composed by the provider rather
    # than handed to it already concatenated.
    assert client.written == [
        f"{intent.arguments['content']}\n{self_authored_marker(intent.run_id, intent.action_hash)}"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["proposed", "rejected", "failed", "succeeded", "withdrawn"],
)
async def test_a_call_citing_an_intent_that_is_not_approved_is_refused(
    monkeypatch, status: str
) -> None:
    """The approval is resolved where it can be checked, not only where it is claimed.

    ``approval_binding == approval_digest`` is a consistency check on two caller-supplied
    fields; anything that fills both in satisfies it, including a caller that made the
    approval up. What makes the reference mean something is the row behind it, so a call
    citing an intent the tenant never approved is refused before GLPI is touched -- and
    the refusal is recorded as a failed invocation rather than disappearing into an
    exception.

    Every status a human decision did not leave standing is exercised, not just the one
    that was obvious. ``SUCCEEDED`` is the one worth naming: it is a *past* approval, and
    a second call citing it would be a re-performance of an action whose duplicate the
    executor's idempotency record already answers for.
    """
    intent, repository, client, audit = _executor_harness(monkeypatch, status=status)
    call = ControlledActionExecutor()._tool_call(tenant_context(), intent)

    with pytest.raises(PermissionError, match="is not approved"):
        await executor_module.build_tool_gateway().execute(call)

    assert client.written == [], "a refused call must not reach GLPI at all"
    assert [row.status for row in audit.invocations] == ["failed"]
    assert [row.error_code for row in audit.invocations] == ["authorization_scope_denied"]
    assert repository.statuses == []


@pytest.mark.asyncio
async def test_the_write_proceeds_while_the_executor_holds_the_claim(monkeypatch) -> None:
    """The defect this pins: the provider refused every write the executor made.

    The executor marks the intent ``EXECUTING`` before it calls the gateway, because the
    claim and the write are one act -- an intent that is being written is no longer
    sitting in the queue. The provider then re-reads that row to decide whether the write
    is authorised, and while it accepted only ``APPROVED`` it refused *all* of them: the
    platform's single side effect could not happen, live acceptance returned 502, and the
    suite was green because the repository fake answered each read with the status it was
    built with, so the transition never reached the check.

    Asserts the status the row actually held when the provider wrote, which is the fact
    no test was looking at.
    """
    intent, repository, client, _ = _executor_harness(monkeypatch)
    seen: list[str] = []
    real_append = ProductionGlpiBackend._append_followup

    async def _recording(self, arguments, call):
        seen.append(repository.status)
        return await real_append(self, arguments, call)

    monkeypatch.setattr(ProductionGlpiBackend, "_append_followup", _recording)

    result = await ControlledActionExecutor().execute(tenant_context(), intent)

    assert result.verified is True
    assert client.written, "the approved write must reach GLPI"
    assert seen == ["executing"], "the write is performed under the executor's own claim"
    assert repository.statuses == ["executing", "succeeded"]


@pytest.mark.asyncio
async def test_the_gateway_will_not_write_a_body_the_approval_does_not_cover(monkeypatch) -> None:
    """The approval covers the bytes, so bytes the intent does not hold are refused.

    The harness is what builds the call, and this is the test that the gateway does not
    take its word for it. The altered call is internally consistent -- its binding is
    recomputed over the altered arguments, so it passes every shape check the policy can
    make -- and it is still refused, because the body is resolved from the persisted
    intent rather than accepted. Without that, the approval would authorise "some write
    by this run", which is not what a human approved.
    """
    intent, repository, client, audit = _executor_harness(monkeypatch)
    real_call = ControlledActionExecutor._tool_call

    def _altered(self, context, candidate):
        call = real_call(self, context, candidate)
        arguments = {**call.arguments, "content": "Close multi-factor authentication."}
        altered = call.model_copy(update={"arguments": arguments})
        return altered.model_copy(update={"approval_binding": altered.approval_digest})

    monkeypatch.setattr(ControlledActionExecutor, "_tool_call", _altered)

    with pytest.raises(PermissionError, match="not the content that was approved"):
        await ControlledActionExecutor().execute(tenant_context(), intent)

    assert client.written == []
    assert repository.statuses[-1] == "failed"
    assert [row.status for row in audit.invocations] == ["failed"]


@pytest.mark.asyncio
async def test_a_marker_that_does_not_name_the_approved_action_is_refused(monkeypatch) -> None:
    """A caller-supplied marker would pick the row the crash-recovery search finds.

    The marker is how a reconciled duplicate is recognised, so a caller that chooses it
    chooses what counts as "already written". It is therefore derived rather than
    accepted: the provider recomputes it from the approved action hash and refuses a call
    that arrives with a different one, which is what stops a second write from being
    mistaken for the first.
    """
    intent, repository, client, _ = _executor_harness(monkeypatch)
    real_call = ControlledActionExecutor._tool_call

    def _forged(self, context, candidate):
        call = real_call(self, context, candidate)
        arguments = {
            **call.arguments,
            "idempotency_marker": "[ServiceMind run=forged action=deadbeefdeadbeef]",
        }
        forged = call.model_copy(update={"arguments": arguments})
        return forged.model_copy(update={"approval_binding": forged.approval_digest})

    monkeypatch.setattr(ControlledActionExecutor, "_tool_call", _forged)

    with pytest.raises(PermissionError, match="does not name the approved action"):
        await ControlledActionExecutor().execute(tenant_context(), intent)

    assert client.written == []
    assert repository.statuses[-1] == "failed"


@pytest.mark.asyncio
async def test_a_principal_without_the_action_role_cannot_write(monkeypatch) -> None:
    """The tool's role set is the graph's, taken from one constant rather than restated.

    ``execute_node`` refuses to spend an approval for a principal that does not hold the
    action role, and the registry now refuses the call for the same reason. Both gates
    reading the same constant is what keeps them from drifting into a state where the
    graph lets a run through and the tool boundary denies it -- or, worse, the reverse.
    """
    intent, repository, client, _ = _executor_harness(monkeypatch)
    context = tenant_context()
    context.roles = {"viewer"}

    with pytest.raises(PermissionError, match="ROLE_DENIED"):
        await ControlledActionExecutor().execute(context, intent)

    assert client.written == []
    assert repository.statuses[-1] == "failed"


@pytest.mark.asyncio
async def test_a_crash_recovered_duplicate_with_a_different_body_is_refused(monkeypatch) -> None:
    """The recovery branch is a read-back too, and it has the same obligation.

    Reconcile-before-write exists to recover a crash between GLPI committing and the
    idempotency row completing. Finding *a* row with our marker is not enough to conclude
    the write landed as approved: this one has our marker and someone else's body, and
    reporting it as a suppressed duplicate would report success for a persisted effect
    nobody approved.

    The verification that runs after the write would also refuse this body, so it is
    turned off here. Otherwise this test passes on that second refusal alone and the
    reconcile comparison -- the guard it is named after -- can be deleted unnoticed.
    """
    intent, repository, client, _ = _executor_harness(monkeypatch, verify=False)
    marker = self_authored_marker(intent.run_id, intent.action_hash)
    client.stored = f"an entirely different followup\n{marker}"

    with pytest.raises(RuntimeError, match="already holds this run's followup"):
        await ControlledActionExecutor().execute(tenant_context(), intent)

    assert repository.statuses[-1] == "failed"
    assert repository.completed == []


@pytest.mark.asyncio
async def test_a_write_glpi_does_not_have_is_not_verified(monkeypatch) -> None:
    """A write the platform cannot read back is not a write the platform may report.

    GLPI answering the append proves the call was accepted, not that a row exists: a
    backend that accepts and drops, or one writing to a replica still catching up, both
    answer with a followup id. The read-back is the platform's only look at what is
    actually stored, so "the row I asked about is not there" is the strongest possible
    disagreement with "the write succeeded" -- and returning True for it is how a write
    that never landed becomes a SUCCEEDED run whose ticket says nothing about the
    incident.
    """
    intent, repository, client, _ = _executor_harness(monkeypatch, conceals_stored=True)

    with pytest.raises(RuntimeError, match="read-back verification failed"):
        await ControlledActionExecutor().execute(tenant_context(), intent)

    assert client.written, "the write was attempted; it is the read-back that disagrees"
    assert repository.statuses[-1] == "failed"
    assert repository.completed == [], "an unreadable write must not be recorded as complete"
    assert repository.audits == [], "and it must not be audited as a created followup"


@pytest.mark.asyncio
async def test_a_crash_recovered_duplicate_with_the_approved_body_is_suppressed(
    monkeypatch,
) -> None:
    """And the recovery it exists for still works when the body is the approved one.

    The reconciled write is deliberately *not* audited as a new followup: it is a row
    that already existed, and a second ``glpi.followup.created`` event for it would put
    two creations in the ledger for one write.
    """
    intent, repository, client, _ = _executor_harness(monkeypatch)
    marker = self_authored_marker(intent.run_id, intent.action_hash)
    client.stored = f"{intent.arguments['content']}\n{marker}"

    result = await ControlledActionExecutor().execute(tenant_context(), intent)

    assert result.duplicate_suppressed is True
    assert repository.statuses[-1] == "succeeded"
    assert client.written == [], "a reconciled duplicate is not written again"
    assert repository.audits == []
