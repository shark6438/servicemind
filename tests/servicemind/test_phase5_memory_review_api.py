from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

import servicemind.api as api_module
import servicemind.security.auth as auth_module
from core import settings
from servicemind.api import phase2_router
from servicemind.context.repository import NullContextArtifactSink
from servicemind.domain.evidence import Evidence, EvidenceSourceType, join_evidence
from servicemind.memory.contracts import (
    MemoryCandidate,
    MemoryStatus,
    MemoryType,
    SemanticSubtype,
)
from servicemind.memory.repository import InMemoryMemoryRepository
from servicemind.memory.service import MemoryWriter
from servicemind.orchestration.phase5_governance import Phase5Governance
from servicemind.security.auth import TenantContext, get_tenant_context

TENANT_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _post_run_inputs(ticket_id: int, user_id: str) -> tuple[dict, dict]:
    item = Evidence.create(
        tenant_id=TENANT_A,
        source_type=EvidenceSourceType.GLPI,
        source_ref=f"glpi://ticket/{ticket_id}",
        resource_type="ticket",
        resource_id=str(ticket_id),
        content="VPN MFA login failure assigned to Network Team",
        provider="test",
        retrieval_method="read",
        confidence=1,
    )
    joined = join_evidence(TENANT_A, [item])
    analysis = {
        "classification": "VPN MFA incident",
        "priority": 2,
        "recommended_group": "Network Team",
        "reasoning_summary": "Current evidence supports network triage.",
        "confidence": 0.98,
        "recurring_incident": True,
        "problem_recommendation": "Verify gateway clock drift before resetting MFA.",
        "change_recommendation": "Resynchronise the VPN gateway clock.",
        "evidence_refs": [item.evidence_id],
    }
    state = {
        "tenant_id": str(TENANT_A),
        "run_id": str(uuid4()),
        "thread_id": f"thread-{ticket_id}",
        "user_id": user_id,
        "goal": "Analyze VPN MFA incident",
        "ticket_id": ticket_id,
        "request_write": False,
        "allowed_glpi_entity_ids": [1],
        "group_ids": [7],
        "joined_evidence": joined.model_dump(mode="json"),
        "analysis_result": analysis,
    }
    result = {
        "analysis": analysis,
        "final_state_verified": True,
        "review": {
            "decision": "passed",
            "confidence": 0.99,
            "review_id": str(uuid4()),
            "policy_version": "review-v1",
        },
        "evidence": joined.model_dump(mode="json"),
        "execution": None,
    }
    return state, result


async def _procedure(repository: InMemoryMemoryRepository):
    governance = Phase5Governance(
        context_sink=NullContextArtifactSink(),
        memory_repository_factory=lambda tenant_id: repository,
    )
    for ticket_id in (42, 43):
        state, result = _post_run_inputs(ticket_id=ticket_id, user_id="alice")
        result["analysis"].update(
            {
                "recurring_incident": True,
                "problem_recommendation": "Verify gateway clock drift before resetting MFA.",
                "change_recommendation": "Resynchronise the VPN gateway clock.",
            }
        )
        await governance.post_run(state=state, result=result, status="succeeded")
    return next(
        record for record in repository.records if record.memory_type is MemoryType.PROCEDURAL
    )


def _context(*, roles: set[str] | None = None, group_ids: set[int] | None = None):
    return TenantContext(
        tenant_id=TENANT_A,
        user_id="approver-1",
        username="approver-1",
        roles=roles if roles is not None else {"approver"},
        allowed_glpi_entity_ids={1},
        allowed_glpi_group_ids=group_ids if group_ids is not None else {7},
    )


def _app(context: TenantContext) -> FastAPI:
    app = FastAPI()
    app.include_router(phase2_router)
    app.dependency_overrides[get_tenant_context] = lambda: context
    return app


@pytest.mark.asyncio
async def test_review_queue_and_decision_bind_the_exact_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "SERVICEMIND_MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "SERVICEMIND_MEMORY_PROCEDURAL_PROPOSALS_ENABLED", True)
    repository = InMemoryMemoryRepository()
    procedure = await _procedure(repository)
    second = await MemoryWriter(repository).write(
        MemoryCandidate.model_validate(
            {
                key: value
                for key, value in procedure.model_dump().items()
                if key in MemoryCandidate.model_fields
            }
            | {
                "subject_key": f"{procedure.subject_key}:second",
                "content": f"{procedure.content} second reviewed variant",
            }
        )
    )
    assert second is not None and second.status is MemoryStatus.QUARANTINE
    monkeypatch.setattr(api_module, "PostgresMemoryRepository", lambda tenant_id: repository)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_context())), base_url="http://test"
    ) as client:
        queued = await client.get("/v1/servicemind/memories/review-queue", params={"limit": 1})
        assert queued.status_code == 200
        assert [item["memory_id"] for item in queued.json()["items"]] == [str(procedure.memory_id)]
        page = queued.json()
        second_page = await client.get(
            "/v1/servicemind/memories/review-queue",
            params={
                "limit": 1,
                "after_created_at": page["next_after_created_at"],
                "after_memory_id": page["next_after_memory_id"],
            },
        )
        assert second_page.status_code == 200
        assert [item["memory_id"] for item in second_page.json()["items"]] == [
            str(second.memory_id)
        ]

        stale = await client.post(
            f"/v1/servicemind/memories/{procedure.memory_id}/review",
            json={
                "decision": "activate",
                "expected_version": procedure.version,
                "expected_content_hash": "0" * 64,
                "review_ref": "review://phase5/1",
                "comment": "Verified against both source incidents.",
            },
        )
        assert stale.status_code == 409
        assert "content changed" in stale.json()["detail"]

        accepted = await client.post(
            f"/v1/servicemind/memories/{procedure.memory_id}/review",
            json={
                "decision": "activate",
                "expected_version": procedure.version,
                "expected_content_hash": procedure.content_hash,
                "review_ref": "review://phase5/1",
                "comment": "Verified against both source incidents.",
            },
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "active"

    stored = next(
        record for record in repository.records if record.memory_id == procedure.memory_id
    )
    assert stored.provenance["human_review_ref"] == "review://phase5/1"
    assert stored.provenance["human_review_comment_recorded"] is True


@pytest.mark.asyncio
async def test_review_queue_fails_closed_for_role_and_group_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "SERVICEMIND_MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "SERVICEMIND_MEMORY_PROCEDURAL_PROPOSALS_ENABLED", True)
    repository = InMemoryMemoryRepository()
    procedure = await _procedure(repository)
    semantic = await MemoryWriter(repository).write(
        MemoryCandidate(
            tenant_id=TENANT_A,
            memory_type=MemoryType.SEMANTIC,
            semantic_subtype=SemanticSubtype.LEARNED_FACT,
            subject_key="vpn-review-owner",
            content="VPN incidents may be assigned to Network Team",
            source_trace_id="trace-semantic-review",
            evidence_refs=(procedure.evidence_refs[0],),
            confidence=0.5,
            importance=0.8,
            provenance={"required_entity_ids": [1], "required_group_ids": [7]},
            created_by="test",
        )
    )
    assert semantic is not None and semantic.status is MemoryStatus.QUARANTINE
    monkeypatch.setattr(api_module, "PostgresMemoryRepository", lambda tenant_id: repository)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_context(roles={"viewer"}))),
        base_url="http://test",
    ) as client:
        assert (await client.get("/v1/servicemind/memories/review-queue")).status_code == 403

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_context(group_ids=set()))),
        base_url="http://test",
    ) as client:
        queued = await client.get("/v1/servicemind/memories/review-queue")
        assert queued.status_code == 200 and queued.json()["items"] == []
        hidden = await client.post(
            f"/v1/servicemind/memories/{procedure.memory_id}/review",
            json={
                "decision": "reject",
                "expected_version": procedure.version,
                "expected_content_hash": procedure.content_hash,
                "review_ref": "review://phase5/hidden",
                "comment": "Should not be authorized.",
            },
        )
        assert hidden.status_code == 404

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_context())), base_url="http://test"
    ) as client:
        semantic_queue = await client.get(
            "/v1/servicemind/memories/review-queue", params={"memory_type": "semantic"}
        )
        assert semantic_queue.status_code == 200
        assert [item["memory_id"] for item in semantic_queue.json()["items"]] == [
            str(semantic.memory_id)
        ]

    expired = procedure.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    repository._records[procedure.memory_id] = expired  # noqa: SLF001
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_context())), base_url="http://test"
    ) as client:
        queued = await client.get(
            "/v1/servicemind/memories/review-queue", params={"memory_type": "procedural"}
        )
        assert queued.status_code == 200 and queued.json()["items"] == []


@pytest.mark.asyncio
async def test_concurrent_review_decisions_have_one_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "SERVICEMIND_MEMORY_ENABLED", True)
    monkeypatch.setattr(settings, "SERVICEMIND_MEMORY_PROCEDURAL_PROPOSALS_ENABLED", True)
    repository = InMemoryMemoryRepository()
    procedure = await _procedure(repository)

    async def decide(status: MemoryStatus):
        return await repository.transition(
            procedure.memory_id,
            status,
            actor_id="approver-1",
            reason=f"HUMAN_REVIEW_{status.value.upper()}",
            human_review_ref="review://phase5/race",
            review_comment="Concurrent decision test.",
            expected_version=procedure.version,
            expected_content_hash=procedure.content_hash,
            expected_status=MemoryStatus.QUARANTINE,
        )

    results = await asyncio.gather(
        decide(MemoryStatus.ACTIVE), decide(MemoryStatus.REVOKED), return_exceptions=True
    )
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    final = next(record for record in repository.records if record.memory_id == procedure.memory_id)
    assert final.status in {MemoryStatus.ACTIVE, MemoryStatus.REVOKED}


@pytest.mark.asyncio
async def test_oidc_group_claim_reaches_the_review_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = auth_module.OIDCVerifier()

    async def jwks(*, force: bool = False):
        del force
        return {"keys": [{"kid": "test"}]}

    monkeypatch.setattr(verifier, "_load_jwks", jwks)
    monkeypatch.setattr(verifier, "_configuration", lambda: ("issuer", "jwks", "audience"))
    monkeypatch.setattr(auth_module.jwt, "get_unverified_header", lambda token: {"kid": "test"})
    monkeypatch.setattr(
        auth_module.jwt.PyJWK,
        "from_dict",
        lambda value: SimpleNamespace(key="public-key"),
    )
    monkeypatch.setattr(
        auth_module.jwt,
        "decode",
        lambda *args, **kwargs: {
            "tenant_id": str(TENANT_A),
            "sub": "approver-1",
            "preferred_username": "approver-1",
            "glpi_entity_ids": ["1"],
            "glpi_group_ids": ["1", "2"],
            "realm_access": {"roles": ["approver"]},
        },
    )

    context = await verifier.verify("token")
    assert context.allowed_glpi_entity_ids == {1}
    assert context.allowed_glpi_group_ids == {1, 2}
    assert context.roles == {"approver"}

    monkeypatch.setattr(
        auth_module.jwt,
        "decode",
        lambda *args, **kwargs: {
            "tenant_id": str(TENANT_A),
            "sub": "approver-1",
            "glpi_entity_ids": ["1"],
            "glpi_group_ids": {"not": "a list"},
            "realm_access": {"roles": ["approver"]},
        },
    )
    with pytest.raises(ValueError, match="glpi_group_ids"):
        await verifier.verify("token")
