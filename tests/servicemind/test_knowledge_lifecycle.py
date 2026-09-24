"""Knowledge document retirement: the flag, the projection, and the request for it.

A document's ``is_active`` ACL flag is the tenant's only way to say "stop citing this".
Three separate things have to hold for that to be true, and each has its own way of
appearing to hold while not holding:

1. The write has to report whether it matched anything. ``source_record_id`` is supplied
   by the caller and validated by nothing, so a retirement aimed at a mistyped id must be
   *refused*; a signature that returns nothing makes it indistinguishable from success.
2. The search projection has to move with it, and become visible. Retrieval pre-filters
   on the indexed copy, and that copy is patched without a refresh by default, so a
   retirement that does not refresh is a document that stays citable for the
   near-real-time window -- including by the run the operator is looking at.
3. The request needs somewhere to come from. There was no HTTP entry point at all.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from servicemind import api as api_module
from servicemind.api import phase2_router
from servicemind.interfaces.http import knowledge as knowledge_module
from servicemind.rag.repository import DocumentActivation
from servicemind.rag.service import EnterpriseRAG
from servicemind.security.auth import TenantContext, get_tenant_context

pytestmark = pytest.mark.asyncio

TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")


def _context(roles: set[str]) -> TenantContext:
    return TenantContext(
        tenant_id=TENANT_ID,
        user_id="globex-operator",
        username="globex-operator",
        roles=roles,
    )


def _app(context: TenantContext) -> FastAPI:
    app = FastAPI()
    app.include_router(phase2_router)
    app.dependency_overrides[get_tenant_context] = lambda: context
    return app


class _Index:
    """Records the order of the two calls whose order is the point."""

    def __init__(self, *, patched: int | None = 3) -> None:
        self._patched = patched
        self.calls: list[str] = []

    async def set_document_active(
        self, tenant_id: UUID, source_record_id: str, *, is_active: bool
    ) -> int | None:
        self.calls.append(f"patch:{source_record_id}:{is_active}")
        return self._patched

    async def refresh(self, tenant_id: UUID) -> None:
        self.calls.append("refresh")


def _rag(repository: object, index: object) -> EnterpriseRAG:
    """A RAG whose two collaborators are the ones under test and nothing else.

    ``set_document_active`` reads exactly ``self.repository`` and ``self.index``; building
    the real object would mean an embedding provider, a reranker and an OpenSearch client
    for a method that touches none of them.
    """
    rag = EnterpriseRAG.__new__(EnterpriseRAG)
    rag.repository = repository  # type: ignore[assignment]
    rag.index = index  # type: ignore[assignment]
    return rag


class _Repository:
    def __init__(self, report: DocumentActivation) -> None:
        self.report = report
        self.seen: list[tuple[str, bool]] = []

    async def set_document_active(
        self, tenant_id: UUID, source_record_id: str, *, is_active: bool
    ) -> DocumentActivation:
        self.seen.append((source_record_id, is_active))
        return self.report


# ------------------------------------------------------------------ the service's report


async def test_a_retirement_whose_id_matched_nothing_is_not_reported_as_a_change() -> None:
    """The whole reason the call returns a report: zero matches is not a no-op success."""
    repository = _Repository(DocumentActivation(matched=0, changed=0))
    index = _Index(patched=0)
    report = await _rag(repository, index).set_document_active(
        TENANT_ID, "KB-GLOBEX-TYPO", is_active=False
    )
    assert report.found is False
    assert report.changed == 0


async def test_a_retirement_reports_what_it_matched_and_what_it_moved() -> None:
    repository = _Repository(DocumentActivation(matched=2, changed=1))
    index = _Index(patched=7)
    report = await _rag(repository, index).set_document_active(
        TENANT_ID, "KB-GLOBEX-VPN-MFA-LEGACY", is_active=False
    )
    assert (report.found, report.matched, report.changed, report.index_rows) == (True, 2, 1, 7)
    assert repository.seen == [("KB-GLOBEX-VPN-MFA-LEGACY", False)]


async def test_replaying_a_retirement_reports_no_further_change() -> None:
    """Idempotent, and visibly so: the second call moved nothing and says so."""
    repository = _Repository(DocumentActivation(matched=1, changed=0))
    report = await _rag(repository, _Index()).set_document_active(
        TENANT_ID, "KB-GLOBEX-VPN-MFA-LEGACY", is_active=False
    )
    assert report.matched == 1
    assert report.changed == 0


async def test_the_projection_is_refreshed_after_it_is_patched() -> None:
    """Order matters: refreshing first would refresh the state the patch replaces."""
    index = _Index(patched=4)
    await _rag(_Repository(DocumentActivation(matched=1, changed=1)), index).set_document_active(
        TENANT_ID, "KB-GLOBEX-VPN-MFA-LEGACY", is_active=False
    )
    assert index.calls == [
        "patch:KB-GLOBEX-VPN-MFA-LEGACY:False",
        "refresh",
    ]


async def test_a_backend_that_reports_no_patch_count_does_not_fail_the_retirement() -> None:
    """The PostgreSQL write is the authority and has already committed by then."""
    repository = _Repository(DocumentActivation(matched=1, changed=1))
    report = await _rag(repository, _Index(patched=None)).set_document_active(
        TENANT_ID, "KB-GLOBEX-VPN-MFA-LEGACY", is_active=False
    )
    assert report.changed == 1
    assert report.index_rows == 0


async def test_a_rag_without_a_repository_reports_that_nothing_matched() -> None:
    """No authority layer means no document was found, not that all of them were."""
    report = await _rag(None, _Index()).set_document_active(
        TENANT_ID, "KB-GLOBEX-VPN-MFA-LEGACY", is_active=False
    )
    assert report.found is False


# -------------------------------------------------------------------- the HTTP endpoint


class _AuditRepository:
    tenant_ids: list[UUID] = []
    audited: list[dict[str, object]] = []

    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_ids.append(tenant_id)

    async def audit(self, **kwargs: object) -> SimpleNamespace:
        self.audited.append(kwargs)
        return SimpleNamespace(id="audit-1")


class _RecordingRag:
    def __init__(self, report: DocumentActivation) -> None:
        self.report = report
        self.calls: list[tuple[str, bool]] = []

    async def set_document_active(
        self, tenant_id: UUID, source_record_id: str, *, is_active: bool
    ) -> DocumentActivation:
        self.calls.append((source_record_id, is_active))
        return self.report


@pytest.fixture
def _endpoint(monkeypatch: pytest.MonkeyPatch):
    def install(report: DocumentActivation) -> tuple[_RecordingRag, _AuditRepository]:
        rag = _RecordingRag(report)
        monkeypatch.setattr(
            knowledge_module, "knowledge_agent", SimpleNamespace(rag=rag), raising=True
        )
        monkeypatch.setattr(knowledge_module, "ServiceMindRepository", _AuditRepository)
        _AuditRepository.tenant_ids = []
        _AuditRepository.audited = []
        return rag, _AuditRepository

    return install


async def test_the_endpoint_refuses_a_record_id_that_matches_nothing(_endpoint) -> None:
    """A 404, not a 200 with a zero count -- and nothing written to the ledger."""
    _endpoint(DocumentActivation(matched=0, changed=0))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_context({"operator"}))), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/servicemind/knowledge/documents:activation",
            json={
                "source_record_id": "KB-GLOBEX-TYPO",
                "is_active": False,
                "reason": "superseded by the rebind runbook",
            },
        )
    assert response.status_code == 404
    assert _AuditRepository.audited == []


async def test_the_endpoint_reports_the_effect_and_audits_the_reason(_endpoint) -> None:
    _endpoint(DocumentActivation(matched=1, changed=1, index_rows=5))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_context({"operator"}))), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/servicemind/knowledge/documents:activation",
            json={
                "source_record_id": "KB-GLOBEX-VPN-MFA-LEGACY",
                "is_active": False,
                "reason": "it advises disabling MFA",
            },
        )
    assert response.status_code == 200
    assert response.json() == {
        "source_record_id": "KB-GLOBEX-VPN-MFA-LEGACY",
        "is_active": False,
        "documents_matched": 1,
        "documents_changed": 1,
        "index_rows": 5,
    }
    assert _AuditRepository.tenant_ids == [TENANT_ID]
    assert len(_AuditRepository.audited) == 1
    audited = _AuditRepository.audited[0]
    assert audited["event_type"] == "knowledge.document.activation"
    assert audited["resource_id"] == "KB-GLOBEX-VPN-MFA-LEGACY"
    assert audited["payload"]["reason"] == "it advises disabling MFA"  # type: ignore[index]


async def test_the_endpoint_refuses_a_reader_that_is_not_an_operator(_endpoint) -> None:
    """Read access to the corpus does not confer the right to retire part of it."""
    rag, _ = _endpoint(DocumentActivation(matched=1, changed=1))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_context({"viewer", "analyst"}))),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/servicemind/knowledge/documents:activation",
            json={"source_record_id": "KB-1", "is_active": False, "reason": "nope"},
        )
    assert response.status_code == 403
    assert rag.calls == []


async def test_the_endpoint_requires_a_reason(_endpoint) -> None:
    """A ledger row that says something changed and nothing about why is not a record."""
    _endpoint(DocumentActivation(matched=1, changed=1))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_context({"operator"}))), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/servicemind/knowledge/documents:activation",
            json={"source_record_id": "KB-1", "is_active": False, "reason": ""},
        )
    assert response.status_code == 422


async def test_the_route_is_registered_under_the_tenant_scoped_prefix() -> None:
    """The capability exists only if the served application publishes it.

    Asserted against the mounted application's OpenAPI schema rather than against the
    router object, because mounting is what applies the prefix, and it is the schema --
    not the router -- that tells a caller the endpoint is there.
    """
    app = FastAPI()
    app.include_router(api_module.phase2_router)
    operation = app.openapi()["paths"].get("/v1/servicemind/knowledge/documents:activation")
    assert operation is not None
    assert set(operation) == {"post"}


async def test_the_activation_view_forbids_extra_fields() -> None:
    with pytest.raises(ValueError):
        knowledge_module.DocumentActivationRequest.model_validate(
            {"source_record_id": "KB-1", "is_active": True, "reason": "why", "actor": "self"}
        )


# ------------------------------------------------------------------ the real repository


#: The two tenants the live fixtures install; the same pair ``test_phase4_jobs`` uses.
LIVE_TENANT = UUID("11111111-1111-4111-8111-111111111111")
LIVE_OTHER = UUID("22222222-2222-4222-8222-222222222222")
RECORD_ID = "kb-lifecycle-probe"


async def _seed_document(
    tenant_id: UUID, source: str, record_id: str, *, acl: dict[str, object]
) -> UUID:
    from uuid import uuid4

    from servicemind.persistence.database import tenant_session
    from servicemind.persistence.models import KnowledgeDocumentRecord

    document_id = uuid4()
    async with tenant_session(tenant_id) as session:
        session.add(
            KnowledgeDocumentRecord(
                id=document_id,
                tenant_id=tenant_id,
                source=source,
                source_record_id=record_id,
                source_version="1",
                title="VPN MFA rebind runbook",
                content="Rebind the authenticator device.",
                content_hash="0" * 64,
                document_type="runbook",
                language="en",
                document_metadata={},
                acl={"tenant_id": str(tenant_id), **acl},
                provenance={},
                index_status="indexed",
            )
        )
    return document_id


@pytest.mark.docker
async def test_the_repository_counts_matches_and_real_moves_against_postgres() -> None:
    """The counts the service reports upward are only worth reporting if they are real.

    Asserted against PostgreSQL rather than against a double, because ``matched`` and
    ``changed`` are read off the stored ACL on the way out of the write -- the one place
    an implementation that looks right can still be wrong about what it did. The two
    documents share a record id under different sources, which the schema permits
    (``(tenant_id, source, source_record_id)``), so a single-match implementation is
    wrong here rather than merely conservative.
    """
    from sqlalchemy import delete

    from servicemind.persistence.database import tenant_session
    from servicemind.persistence.models import KnowledgeDocumentRecord
    from servicemind.rag.repository import KnowledgeRepository

    repository = KnowledgeRepository()
    try:
        await _seed_document(LIVE_TENANT, "runbook", RECORD_ID, acl={"is_active": True})
        # A row written before the flag existed carries no ``is_active`` key. It is not
        # therefore a retired document: the ACL model's default is active, so retiring it
        # is a real change and has to be counted as one.
        await _seed_document(LIVE_TENANT, "wiki", RECORD_ID, acl={})
        await _seed_document(
            LIVE_TENANT, "runbook", "kb-lifecycle-untouched", acl={"is_active": True}
        )

        retired = await repository.set_document_active(LIVE_TENANT, RECORD_ID, is_active=False)
        assert (retired.found, retired.matched, retired.changed) == (True, 2, 2)

        # Replayed: still two documents, and now nothing left to move.
        replayed = await repository.set_document_active(LIVE_TENANT, RECORD_ID, is_active=False)
        assert (replayed.found, replayed.matched, replayed.changed) == (True, 2, 0)

        restored = await repository.set_document_active(LIVE_TENANT, RECORD_ID, is_active=True)
        assert (restored.found, restored.matched, restored.changed) == (True, 2, 2)

        missing = await repository.set_document_active(
            LIVE_TENANT, "kb-lifecycle-absent", is_active=False
        )
        assert (missing.found, missing.matched) == (False, 0)

        # Another tenant's window does not contain the row, so a foreign record id is a
        # miss here rather than a cross-tenant write.
        foreign = await repository.set_document_active(LIVE_OTHER, RECORD_ID, is_active=False)
        assert (foreign.found, foreign.matched) == (False, 0)
        assert (
            await repository.set_document_active(LIVE_TENANT, RECORD_ID, is_active=True)
        ).changed == 0
    finally:
        async with tenant_session(LIVE_TENANT) as session:
            await session.execute(
                delete(KnowledgeDocumentRecord).where(
                    KnowledgeDocumentRecord.source_record_id.in_(
                        [RECORD_ID, "kb-lifecycle-untouched"]
                    )
                )
            )
