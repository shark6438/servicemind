"""Slice 4: per-source ingestion job register + EnterpriseRAG failure semantics.

Offline tests drive ``EnterpriseRAG.ingest`` against recording fakes and assert the
job rows each source closes (attempts bump, per-source metrics, failed sources
persisted before the error is re-raised). The docker-gated test exercises the real
RLS-protected ``KnowledgeRepository`` against PostgreSQL: upsert keyed on
``(tenant_id, source)``, attempt progression and cross-tenant isolation.
"""

from uuid import UUID

import pytest

from servicemind.domain.knowledge import (
    AuthorityLevel,
    CorpusScope,
    KnowledgeACL,
)
from servicemind.rag.models import CallableReranker, DeterministicEmbeddingProvider
from servicemind.rag.service import EnterpriseRAG
from servicemind.rag.sources import make_document

TENANT = UUID("11111111-1111-4111-8111-111111111111")
OTHER = UUID("22222222-2222-4222-8222-222222222222")


def _acl() -> KnowledgeACL:
    return KnowledgeACL(corpus_scope=CorpusScope.TENANT, tenant_id=TENANT)


def _doc(source: str, record_id: str) -> object:
    return make_document(
        title=f"{source} runbook",
        content=(
            "# Troubleshooting\n\n"
            "Authenticate against the identity provider before touching the VPN "
            "gateway configuration.\n\n"
            "## Checks\n\n"
            "Verify the clock, verify the token lifetime, then review the IdP "
            "audit log for the account.\n"
        ),
        document_type="runbook",
        source=source,
        source_version="v1",
        source_uri=f"runbook://{record_id}",
        source_record_id=record_id,
        license_name="project-owned",
        authority=AuthorityLevel.INTERNAL_KNOWLEDGE,
        acl=_acl(),
    )


class FakeJobIndex:
    """Minimal recording stand-in: ingest only needs replace_document/refresh/publish."""

    def __init__(self) -> None:
        self.replace_calls = 0
        self.published = 0
        self.refreshed = 0

    async def replace_document(self, tenant_id, document, parents, children, embedding) -> None:
        assert tenant_id == TENANT
        self.replace_calls += 1

    async def refresh(self, tenant_id) -> None:
        assert tenant_id == TENANT
        self.refreshed += 1

    async def publish(self, tenant_id, embedding) -> None:
        assert tenant_id == TENANT
        self.published += 1


class FakeJobRepository:
    """In-memory authority + ingestion register with the same job surface as
    ``KnowledgeRepository``, so ingest() closes real rows we can assert on."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.current: set[str] = set()
        self.fail_sources: set[str] = set()

    async def is_current(self, tenant_id, document) -> bool:
        assert tenant_id == TENANT
        return document.provenance.source_record_id in self.current

    async def replace(self, tenant_id, document, parents, children):
        assert tenant_id == TENANT
        if document.provenance.source in self.fail_sources:
            raise RuntimeError("simulated index failure")
        self.current.add(document.provenance.source_record_id)
        return document, parents, children

    async def mark_indexed(self, tenant_id, document_id) -> None:
        assert tenant_id == TENANT

    async def count_pending(self, tenant_id) -> int:
        assert tenant_id == TENANT
        return 0

    async def parents(self, tenant_id, ids):
        assert tenant_id == TENANT
        return {}

    async def begin_ingestion_job(self, tenant_id, source: str) -> int:
        assert tenant_id == TENANT
        attempts = self.jobs.get(source, {}).get("attempts", 0) + 1
        self.jobs[source] = {
            "status": "running",
            "attempts": attempts,
            "error_code": None,
            "metrics": {},
        }
        return attempts

    async def complete_ingestion_job(
        self, tenant_id, source: str, *, status, error_code=None, metrics=None
    ) -> None:
        assert tenant_id == TENANT
        row = self.jobs.setdefault(source, {"status": "running", "attempts": 1})
        row["status"] = status
        row["error_code"] = error_code
        row["metrics"] = metrics


def _rag(index: FakeJobIndex, repository: FakeJobRepository) -> EnterpriseRAG:
    return EnterpriseRAG(
        index=index,  # type: ignore[arg-type]
        embedding=DeterministicEmbeddingProvider(),
        reranker=CallableReranker(lambda query, text: 0),
        repository=repository,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_ingest_records_per_source_jobs_with_metrics_and_attempts() -> None:
    """Two sources in one batch close two rows; a re-run bumps attempts and reports
    the docs that were skipped as already-current instead of re-embedded."""
    index = FakeJobIndex()
    repository = FakeJobRepository()
    rag = _rag(index, repository)
    documents = [
        _doc("source_a", "a-1"),
        _doc("source_a", "a-2"),
        _doc("source_b", "b-1"),
    ]

    totals = await rag.ingest(TENANT, documents)

    assert totals["documents"] == 3
    assert index.replace_calls == 3
    assert repository.jobs["source_a"]["status"] == "succeeded"
    assert repository.jobs["source_a"]["metrics"]["documents"] == 2
    assert repository.jobs["source_b"]["status"] == "succeeded"
    assert repository.jobs["source_b"]["metrics"]["documents"] == 1

    # Second run: every document is already current, so nothing is re-embedded but
    # the register still records the run (attempts advance) and attributes skips.
    totals = await rag.ingest(TENANT, documents)
    assert totals["skipped"] == 3
    assert index.replace_calls == 3  # unchanged
    assert repository.jobs["source_a"]["attempts"] == 2
    assert repository.jobs["source_a"]["metrics"]["skipped"] == 2
    assert repository.jobs["source_b"]["attempts"] == 2
    assert repository.jobs["source_b"]["metrics"]["skipped"] == 1


@pytest.mark.asyncio
async def test_ingest_records_failed_source_before_re_raising() -> None:
    """A source that fails is closed as ``failed`` (with its coarse error code and
    partial metrics) and the exception still propagates -- never swallowed. Earlier
    sources in the same batch keep their ``succeeded`` rows."""
    index = FakeJobIndex()
    repository = FakeJobRepository()
    repository.fail_sources.add("boom")
    rag = _rag(index, repository)
    documents = [_doc("healthy", "h-1"), _doc("boom", "x-1")]

    with pytest.raises(RuntimeError, match="simulated index failure"):
        await rag.ingest(TENANT, documents)

    assert repository.jobs["healthy"]["status"] == "succeeded"
    failed = repository.jobs["boom"]
    assert failed["status"] == "failed"
    assert failed["error_code"] == "RuntimeError"
    assert failed["metrics"] == {"documents": 0, "parents": 0, "children": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_ingest_tolerates_repository_without_job_register() -> None:
    """Thin authority substitutes (smoke/eval harnesses) that only implement the
    read/write surface must not force a job row; ingestion stays functional."""

    class ThinRepository:
        """Minimal substitute exposing only the authority methods -- no job surface."""

        def __init__(self) -> None:
            self.current: set[str] = set()

        async def is_current(self, tenant_id, document) -> bool:
            assert tenant_id == TENANT
            return document.provenance.source_record_id in self.current

        async def replace(self, tenant_id, document, parents, children):
            assert tenant_id == TENANT
            self.current.add(document.provenance.source_record_id)
            return document, parents, children

        async def mark_indexed(self, tenant_id, document_id) -> None:
            assert tenant_id == TENANT

        async def count_pending(self, tenant_id) -> int:
            assert tenant_id == TENANT
            return 0

        async def parents(self, tenant_id, ids):
            assert tenant_id == TENANT
            return {}

    index = FakeJobIndex()
    rag = _rag(index, ThinRepository())  # type: ignore[arg-type]
    totals = await rag.ingest(TENANT, [_doc("source_a", "a-1")])
    assert totals["documents"] == 1
    assert index.replace_calls == 1
    assert index.published == 1  # publish gate still fires once all docs are pending-free


@pytest.mark.asyncio
async def test_reconcile_does_not_mark_old_generation_rows_during_migration() -> None:
    document_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    class MigrationIndex:
        def generation(self, embedding) -> str:
            return "desired"

        async def active_generation(self, tenant_id) -> str:
            return "old"

        async def indexed_document_ids(self, tenant_id):
            return {document_id}

        async def prune_to(self, tenant_id, present):
            return 0

    class MigrationRepository:
        def __init__(self) -> None:
            self.marked = False

        async def reconcile_indexed(self, tenant_id, document_ids):
            self.marked = True
            return len(document_ids)

        async def source_record_ids(self, tenant_id):
            return {"runbook-a"}

    repository = MigrationRepository()
    rag = EnterpriseRAG(
        index=MigrationIndex(),  # type: ignore[arg-type]
        embedding=DeterministicEmbeddingProvider(),
        reranker=CallableReranker(lambda query, text: 0),
        repository=repository,  # type: ignore[arg-type]
    )

    result = await rag.reconcile(TENANT)

    assert result == {"marked": 0, "pruned": 0}
    assert repository.marked is False


# --------------------------------------------------------------------------- live PG


@pytest.mark.docker
@pytest.mark.asyncio
async def test_knowledge_repository_job_register_upsert_and_tenant_isolation() -> None:
    """Real RLS repository: (tenant, source) upsert, attempt progression, terminal
    state and metrics survive a round-trip, and another tenant never sees the row."""
    from sqlalchemy import delete

    from servicemind.persistence.database import tenant_session
    from servicemind.persistence.models import KnowledgeIngestionJob
    from servicemind.rag.repository import KnowledgeRepository

    repository = KnowledgeRepository()
    source = "jobtest-register"
    try:
        # Fresh source starts at attempt 1; a retry advances the same row.
        assert await repository.begin_ingestion_job(TENANT, source) == 1
        assert await repository.begin_ingestion_job(TENANT, source) == 2
        await repository.complete_ingestion_job(
            TENANT,
            source,
            status="failed",
            error_code="RuntimeError",
            metrics={"documents": 0, "parents": 0, "children": 0, "skipped": 0},
        )
        rows = await repository.list_ingestion_jobs(TENANT, source=source)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["attempts"] == 2  # begin fixed the count; complete preserved it
        assert rows[0]["error_code"] == "RuntimeError"

        # A successful close keeps attempts, overwrites metrics/error.
        await repository.complete_ingestion_job(
            TENANT,
            source,
            status="succeeded",
            metrics={"documents": 3, "parents": 4, "children": 20, "skipped": 0},
        )
        rows = await repository.list_ingestion_jobs(TENANT, source=source)
        assert rows[0]["status"] == "succeeded"
        assert rows[0]["error_code"] is None
        assert rows[0]["metrics"]["children"] == 20
        assert rows[0]["attempts"] == 2

        # RLS: the other tenant's register view excludes this row entirely.
        other = await repository.list_ingestion_jobs(OTHER)
        assert all(row["source"] != source for row in other)
    finally:
        async with tenant_session(TENANT) as session:
            await session.execute(
                delete(KnowledgeIngestionJob).where(KnowledgeIngestionJob.source == source)
            )
