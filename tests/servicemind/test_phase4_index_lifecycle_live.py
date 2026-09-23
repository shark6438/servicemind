"""The index lifecycle against a real OpenSearch cluster.

``test_phase4_index_lifecycle.py`` asserts the *mechanics* -- which requests the client
is handed, in which order -- against a recording stand-in. The stand-in answers from its
own bookkeeping, so it agrees with the code by construction on everything the code does
not actually send: whether the strict mapping accepts a row, whether an alias really
moves without a gap in service, whether the ACL pre-filter leaves a suspended row out of
a *ranked* result. None of that is observable anywhere but on a cluster, and nothing in
this repository had ever observed it -- the offline module's docstring claimed this suite
existed while it did not, which is worse than saying nothing at all, because a reader
stops looking for it.

Gated by ``--run-docker`` and skipped without cluster credentials. Both tests work under
a throwaway tenant id and delete their own indices in ``finally``, so a run against the
shared cluster never reads or disturbs acme/globex. The RRF search pipeline the hybrid
mode needs is cluster-wide and shared with production; the code's own ``PUT`` is
idempotent and refuses to overwrite a definition that differs from this build's.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from core import settings
from servicemind.domain.knowledge import (
    AuthorityLevel,
    CorpusScope,
    KnowledgeACL,
    KnowledgeDocument,
    KnowledgeProvenance,
    KnowledgeQuery,
    RetrievalMode,
    RetrievalPrincipal,
)
from servicemind.rag.chunking import semantic_chunker
from servicemind.rag.models import CallableReranker, DeterministicEmbeddingProvider
from servicemind.rag.opensearch import OpenSearchKnowledgeIndex
from servicemind.rag.parsing import structure_parser
from servicemind.rag.service import EnterpriseRAG, build_opensearch_client

#: The acceptance tenants are ``1111…``/``2222…``. A lifecycle probe that shares a
#: tenant with them would be a probe that damages what it is used to verify.
PROBE_TENANT = UUID("77777777-7777-4777-8777-777777777777")
DIMENSION = 16
QUESTION = "VPN authentication device rebind"

#: Fixed and in the past, for the reason ``gold.py`` gives for the same choice: a row
#: that ages out of its own ACL window mid-test would be a flake carrying no signal.
PROBE_EFFECTIVE_FROM = datetime(2026, 1, 1, tzinfo=UTC)

ALPHA = "Rebind the VPN authentication device after a phone change."
BETA = "The VPN authentication device must be re-enrolled by the service desk."


class _RevisionV2Embedding(DeterministicEmbeddingProvider):
    """A second generation that differs from the first only in its revision."""

    model_revision = "v2-live-probe"


def _client():
    if settings.SERVICEMIND_OPENSEARCH_PASSWORD is None:
        pytest.skip("SERVICEMIND_OPENSEARCH_PASSWORD is not configured; no cluster to probe")
    return build_opensearch_client()


def _index(client) -> OpenSearchKnowledgeIndex:
    return OpenSearchKnowledgeIndex(client, dimension=DIMENSION)


def _rag(index: OpenSearchKnowledgeIndex, embedding) -> EnterpriseRAG:
    """A repository-less RAG: no PostgreSQL, so ingest publishes as soon as it lands.

    The repository is the authority for "is this generation fully written"; a probe
    that cannot create authority rows must not pretend it has them, and passing none
    is the harness's own supported mode rather than a shortcut invented here.
    """
    reranker = CallableReranker(lambda _query, _document: 0.5)
    return EnterpriseRAG(index=index, embedding=embedding, reranker=reranker)


def _principal() -> RetrievalPrincipal:
    return RetrievalPrincipal(
        tenant_id=PROBE_TENANT,
        user_id="index-lifecycle-probe",
        entity_ids=frozenset({1}),
    )


def _document(record_id: str, content: str, *, is_active: bool = True) -> KnowledgeDocument:
    return KnowledgeDocument(
        title=f"VPN MFA device rebind ({record_id})",
        content=content,
        document_type="internal_sop",
        metadata={"live_probe": True},
        acl=KnowledgeACL(
            corpus_scope=CorpusScope.TENANT,
            tenant_id=PROBE_TENANT,
            # Declared, not left empty: an empty coordinate means "this row states no
            # entity restriction", which is a different code path from the one a real
            # tenant document takes. The probe takes the real one.
            entity_ids=frozenset({1}),
            effective_from=PROBE_EFFECTIVE_FROM,
            is_active=is_active,
        ),
        provenance=KnowledgeProvenance(
            source="live-probe",
            source_version="v1",
            source_uri=f"probe://{record_id}",
            source_record_id=record_id,
            license="project-owned",
            authority_level=AuthorityLevel.INTERNAL_KNOWLEDGE,
            content_hash=KnowledgeDocument.content_digest(content),
        ),
    )


async def _register(
    index: OpenSearchKnowledgeIndex, document: KnowledgeDocument, embedding
) -> None:
    """Run the real parse/chunk/embed path and write it into the document's generation.

    ``ingest`` does exactly this and then publishes; this is it with the eager publish
    removed, so a generation can be filled *while* the alias is still elsewhere. That
    is the only way to observe blue-green at all -- through ``ingest`` the alias flips
    the moment the first row lands and the window closes before it can be inspected.
    """
    blocks = structure_parser.parse_markdown(document)
    parents = semantic_chunker.build_parents(blocks)
    children = await semantic_chunker.build_children(document, parents, embedding)
    await index.replace_document(PROBE_TENANT, document, parents, children, embedding)
    await index.refresh(PROBE_TENANT)


async def _search(index: OpenSearchKnowledgeIndex, embedding, mode: RetrievalMode) -> set[str]:
    hits = await index.search(
        KnowledgeQuery(raw_query=QUESTION, normalized_query=QUESTION),
        _principal(),
        embedding,
        mode=mode,
        candidate_k=10,
        dense_k=10,
        bm25_k=10,
    )
    return {hit.source_record_id for hit in hits}


async def _drop_probe_indices(client) -> None:
    """Delete every concrete index this suite could have created, whatever it is.

    A wildcard rather than a list of expected names: a run that died part-way through
    the migration leaves generations behind that no in-memory list would know about,
    and a leftover generation under a live alias is precisely what would make the
    *next* run start from a state this suite does not describe.
    """
    pattern = f"*tenant-{PROBE_TENANT}-*"
    try:
        found = await client.indices.get(index=pattern, allow_no_indices=True)
    except Exception:  # noqa: BLE001 - ``get`` reports "nothing matched" as an error
        return
    for name in found:
        await client.indices.delete(index=name, ignore_unavailable=True)


@pytest.mark.docker
@pytest.mark.asyncio
async def test_a_generation_is_built_aliased_and_then_retired_on_the_cluster() -> None:
    """The whole lifecycle in one pass, because the parts are only meaningful in order.

    A migration writes into a generation the alias does not point at yet. Readers must
    keep seeing the old generation throughout -- not an empty one, and not a half-built
    one -- and the switch must be a single moment after which the superseded concretes
    are actually gone. A stand-in cannot show any of the three: it has no aliases, no
    mapping to reject a row, and nothing to leave behind.
    """
    client = _client()
    index = _index(client)
    v1 = DeterministicEmbeddingProvider()
    v2 = _RevisionV2Embedding()
    gen_v1, gen_v2 = index.generation(v1), index.generation(v2)
    assert gen_v1 != gen_v2, "the revision must land in a different generation"
    try:
        await _drop_probe_indices(client)

        # --- the first generation: written, then aliased --------------------------
        await _rag(index, v1).ingest(PROBE_TENANT, [_document("SRC-ALPHA", ALPHA)])
        child_v1 = index._child_concrete(PROBE_TENANT, gen_v1)
        parent_v1 = index._parent_concrete(PROBE_TENANT, gen_v1)
        assert await index.active_generation(PROBE_TENANT) == gen_v1
        assert await index._resolve_alias(index.child_alias(PROBE_TENANT)) == {child_v1}
        assert await index._resolve_alias(index.parent_alias(PROBE_TENANT)) == {parent_v1}
        assert await _search(index, v1, RetrievalMode.BM25) == {"SRC-ALPHA"}

        # Re-publishing the generation already serving is a no-op, not a second switch.
        assert (await index.publish(PROBE_TENANT, v1)).status == "ready"

        # --- an empty new generation must not take the alias ----------------------
        deferred = await index.publish(PROBE_TENANT, v2)
        assert deferred.status == "stale", "the alias must not move onto an empty generation"
        assert deferred.previous_generation == gen_v1
        assert await index.active_generation(PROBE_TENANT) == gen_v1

        # --- half a migration: written, but still invisible -----------------------
        await _register(index, _document("SRC-BETA", BETA), v2)
        assert await index.active_generation(PROBE_TENANT) == gen_v1
        assert await _search(index, v2, RetrievalMode.BM25) == {"SRC-ALPHA"}, (
            "a row in the un-published generation must not be reachable through the alias"
        )

        # --- the switch retires the superseded concretes --------------------------
        switched = await index.publish(PROBE_TENANT, v2)
        assert switched.status == "switched"
        assert switched.previous_generation == gen_v1
        assert await index.active_generation(PROBE_TENANT) == gen_v2
        assert not await client.indices.exists(index=child_v1), (
            "the superseded concrete must be gone"
        )
        assert not await client.indices.exists(index=parent_v1)
        assert await _search(index, v2, RetrievalMode.BM25) == {"SRC-BETA"}
    finally:
        await _drop_probe_indices(client)
        await client.close()


@pytest.mark.docker
@pytest.mark.asyncio
async def test_a_suspended_row_is_unreachable_through_a_real_filtered_search() -> None:
    """``is_active`` is a pre-filter, and a pre-filter is only real when a cluster runs it.

    The offline suite asserts the filter is *sent*. This asserts that the row it names
    is the row the cluster leaves out -- in every mode, including the ones whose filter
    rides inside the ``knn`` clause and whose result is ranked rather than enumerated,
    where "absent" and "never returned" are not the same thing. Empty is asserted as
    well as non-empty: "the suspended row is absent" says nothing if nothing came back
    in the first place.
    """
    client = _client()
    index = _index(client)
    embedding = DeterministicEmbeddingProvider()
    try:
        await _drop_probe_indices(client)
        await _rag(index, embedding).ingest(
            PROBE_TENANT,
            [
                _document("SRC-LIVE", ALPHA),
                _document("SRC-RETIRED", BETA, is_active=False),
            ],
        )
        assert await index.active_generation(PROBE_TENANT) is not None

        for mode in (RetrievalMode.BM25, RetrievalMode.DENSE, RetrievalMode.HYBRID):
            found = await _search(index, embedding, mode)
            assert "SRC-LIVE" in found, f"{mode} must find the active row"
            assert "SRC-RETIRED" not in found, f"{mode} must not surface a suspended row"

        # The other direction of the same flag: a row written active, then suspended.
        await _rag(index, embedding).set_document_active(PROBE_TENANT, "SRC-LIVE", is_active=False)
        await index.refresh(PROBE_TENANT)
        assert await _search(index, embedding, RetrievalMode.BM25) == set(), (
            "suspending the only active row must leave nothing retrievable"
        )
    finally:
        await _drop_probe_indices(client)
        await client.close()
