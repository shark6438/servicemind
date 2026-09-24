"""Governed lifecycle operations on the tenant's knowledge corpus.

A stored document carries an ACL, and that ACL's ``is_active`` flag is the tenant's only
way to say "this document is wrong, stop citing it": retrieval pre-filters on it, the
context envelope ranks around it, and a re-ingest re-derives it from the same ACL. Until
this router existed nothing outside the process could write it. The flag was readable on
every retrieval and written by re-ingest, but an operator who needed a superseded runbook
to stop being served had no request to make -- the acceptance fixtures had to retire one
by calling the service in-process, which is not a capability a deployment has.

The write is a governance action, so it is audited, and it reports what it did rather
than what it was asked to do. ``source_record_id`` is a key the caller supplies and
nothing on the path validates it, so a retirement aimed at a mistyped id is the failure
mode that matters here: it must be refused, not silently counted as done.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from servicemind.agents.knowledge import knowledge_agent
from servicemind.persistence.repository import ServiceMindRepository
from servicemind.security.auth import TenantContextDependency

router = APIRouter(prefix="/knowledge", tags=["ServiceMind Knowledge"])

#: Longest ``source_record_id`` the document table stores (``String(500)``). Bounded here
#: so a request that could never match a row is rejected as malformed rather than
#: answered with a 404 that reads like "this tenant does not have that document".
SOURCE_RECORD_ID_MAX = 500


class DocumentActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_record_id: str = Field(min_length=1, max_length=SOURCE_RECORD_ID_MAX)
    is_active: bool
    #: Required. A retirement is an operator saying a document must stop being cited, and
    #: the ledger row is the only place that says why; without it the audit trail records
    #: that something changed and nothing about what was wrong with it.
    reason: str = Field(min_length=1, max_length=1000)


class DocumentActivationView(BaseModel):
    """The effect, not the request: what matched, what moved, what retrieval will see."""

    source_record_id: str
    is_active: bool
    documents_matched: int
    documents_changed: int
    index_rows: int


@router.post("/documents:activation", response_model=DocumentActivationView)
async def set_document_activation(
    body: DocumentActivationRequest,
    context: TenantContextDependency,
) -> DocumentActivationView:
    """Suspend or reinstate every stored document under one source record id.

    Addressed by record id alone because that is the key retrieval binds a passage back
    to, and so the key a citation names. The schema's uniqueness is
    ``(tenant_id, source, source_record_id)``, so one record id under two sources is two
    documents and this reaches both; the response's ``documents_matched`` is how the
    caller sees that rather than having to know it.
    """
    context.require_any_role("operator", "tenant_admin")
    report = await knowledge_agent.rag.set_document_active(
        context.tenant_id, body.source_record_id, is_active=body.is_active
    )
    if not report.found:
        # 404 rather than 200 with a zero count: the caller asked for a specific effect
        # on a specific document and no such document exists in this tenant. The row-level
        # security window is why this answer says nothing about other tenants -- the
        # record id is looked up inside this tenant's session, so a miss here is a miss
        # for every id this caller is not allowed to see, not just for the ones that
        # genuinely do not exist.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No stored knowledge document carries that source_record_id in this tenant",
        )
    await ServiceMindRepository(context.tenant_id).audit(
        actor_id=context.user_id,
        event_type="knowledge.document.activation",
        resource_type="KnowledgeDocument",
        resource_id=body.source_record_id,
        run_id=None,
        payload={
            "is_active": body.is_active,
            "reason": body.reason,
            "documents_matched": report.matched,
            "documents_changed": report.changed,
            "index_rows": report.index_rows,
        },
    )
    return DocumentActivationView(
        source_record_id=body.source_record_id,
        is_active=body.is_active,
        documents_matched=report.matched,
        documents_changed=report.changed,
        index_rows=report.index_rows,
    )
