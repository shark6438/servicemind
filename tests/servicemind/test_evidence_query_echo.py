"""Retrieval bookkeeping must never reach the model as evidence.

Live regression, 2026-09-22, GLPI ticket 17. After a RETRIEVE_MORE round the semantic
judge wrote::

    ev-394c13b80e6adb18 is a PagerDuty incident-commander chunk whose metadata.query
    merely echoes the retrieval query; ... In fact ev-394c13b80e6adb18's query payload
    embeds a VPN/MFA triage passage listing exactly those checks

The judge was not confused. ``EnterpriseRAG.to_evidence`` copied the processed query
into every evidence row's metadata, ``Phase5Governance`` serialized the whole evidence
object with ``model_dump_json()`` into the model-visible content at trust=VERIFIED and
authority=0.95, and ``SupervisorWorkflow`` builds a later round's query *from the
Reviewer's own feedback*. So the round-0 critique came back on round 1 wearing the
Reviewer's uniform: the analysis was then rejected for failing to match text that had
originated in the review, and no amount of re-retrieval could ever resolve it. The
echo also doubled the payload -- 1003 of the 2006 bytes of that evidence item were the
query repeated.

The rule pinned here: evidence tells the model what was found, never how it was looked
for. The providers no longer emit the key, and the governance boundary strips it from
anything that does, so a future provider cannot reintroduce the loop.
"""

import json
from uuid import UUID, uuid4

from servicemind.domain.evidence import Evidence, EvidenceSourceType
from servicemind.domain.knowledge import (
    AuthorityLevel,
    Citation,
    CorpusScope,
    KnowledgeACL,
    KnowledgeQuery,
    KnowledgeRAGResult,
    RetrievalHit,
)
from servicemind.domain.knowledge import (
    ContextItem as RagContextItem,
)
from servicemind.orchestration.phase5_governance import _model_visible_evidence
from servicemind.rag.service import EnterpriseRAG

TENANT = UUID("11111111-1111-4111-8111-111111111111")

#: Stands in for the Reviewer's feedback that SupervisorWorkflow injects into a
#: re-retrieval query -- text that must never be readable back as evidence.
FEEDBACK = "verify identity-provider health, token clock skew and gateway reachability"

DOCUMENT = "Incident Commander Playbook: declare a bridge and post updates every 30 minutes."


def rag_item() -> RagContextItem:
    document_id, parent_id = uuid4(), uuid4()
    hit = RetrievalHit(
        child_chunk_id=uuid4(),
        parent_chunk_id=parent_id,
        document_id=document_id,
        child_content=DOCUMENT,
        score=0.8,
        rerank_score=0.9,
        title="Incident Commander Playbook",
        source="pagerduty",
        source_uri="pagerduty://docs/incident-commander.md",
        source_record_id="incident-commander",
        source_version="1",
        license="CC-BY-4.0",
        authority_level=AuthorityLevel.EXTERNAL_BEST_PRACTICE,
        synthetic=False,
        content_hash="a" * 64,
        acl=KnowledgeACL(corpus_scope=CorpusScope.GLOBAL_LICENSED),
    )
    return RagContextItem(
        parent_content=DOCUMENT, hit=hit, citation=Citation.from_hit(hit), token_count=20
    )


def rag_result() -> KnowledgeRAGResult:
    return KnowledgeRAGResult(
        query=KnowledgeQuery(
            raw_query=f"Analyze the VPN gateway certificate {FEEDBACK}",
            normalized_query=f"Analyze the VPN gateway certificate {FEEDBACK}",
            rewritten_queries=[f"VPN gateway certificate {FEEDBACK}"],
        ),
        items=[rag_item()],
        retrieval_mode="hybrid+rerank",
        candidate_count=1,
        latency_ms=1.0,
    )


def test_knowledge_evidence_does_not_carry_the_retrieval_query() -> None:
    """The source: a row of evidence is the document, not the search that found it."""
    # ``to_evidence`` reads no instance state (unlike ``graph_evidence``, which owns the
    # graph store), so the projection can be tested without standing up index/embedding/
    # reranker providers.
    rag = EnterpriseRAG.__new__(EnterpriseRAG)
    evidence = rag.to_evidence(TENANT, rag_result())

    assert evidence, "the fixture must produce evidence for this test to mean anything"
    for item in evidence:
        assert "query" not in item.metadata
        assert FEEDBACK not in item.model_dump_json()
        # The citation is what a reviewer verifies against, and it is still here.
        assert item.metadata["citation"]["content_hash"] == "a" * 64


def test_the_model_visible_boundary_strips_query_bookkeeping() -> None:
    """The guarantee: a provider that reattaches it cannot leak it anyway."""
    evidence = Evidence.create(
        tenant_id=TENANT,
        source_type=EvidenceSourceType.KNOWLEDGE,
        source_ref="pagerduty://docs/incident-commander.md",
        resource_type="knowledge_parent_chunk",
        resource_id="incident-commander",
        content=DOCUMENT,
        provider="pagerduty",
        retrieval_method="hybrid+rerank",
        metadata={
            "synthetic": False,
            "license": "CC-BY-4.0",
            "query": {"raw_query": FEEDBACK, "rewritten_queries": [FEEDBACK]},
            "rewritten_queries": [FEEDBACK],
            "model_query": FEEDBACK,
        },
    )

    content = _model_visible_evidence(evidence)
    rendered = json.loads(content)

    assert FEEDBACK not in content
    # What the model is supposed to reason over survives intact.
    assert rendered["content"] == DOCUMENT
    assert rendered["metadata"]["license"] == "CC-BY-4.0"
    assert rendered["metadata"]["synthetic"] is False
    # Everything else about the evidence is still there; only the echo is dropped.
    assert rendered["evidence_id"] == evidence.evidence_id
    assert rendered["provenance"]["content_hash"] == evidence.provenance.content_hash


def test_metadata_that_is_not_query_derived_is_never_dropped() -> None:
    """GLPI ticket facts are the documented overflow past the content ceiling."""
    evidence = Evidence.create(
        tenant_id=TENANT,
        source_type=EvidenceSourceType.GLPI,
        source_ref="glpi://tickets/17",
        resource_type="ticket",
        resource_id="17",
        content="VPN 网关证书剩余有效期 12 天。",
        provider="glpi",
        retrieval_method="api",
        metadata={"ticket_facts": {"id": 17, "urgency": 4, "impact": 3}},
    )

    rendered = json.loads(_model_visible_evidence(evidence))

    assert rendered["metadata"]["ticket_facts"] == {"id": 17, "urgency": 4, "impact": 3}
