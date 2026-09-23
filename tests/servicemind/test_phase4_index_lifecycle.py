"""Slice 1: versioned index generations, atomic alias lifecycle and retrieval modes.

All tests run offline against a recording stand-in for the OpenSearch client so the
mechanics (identity encoding, blue-green alias switch, superseded-generation
retirement, per-mode query bodies) are asserted without a live cluster.

A stand-in agrees with the code by construction about everything the code does not
actually send, so it cannot answer whether the cluster accepts a row, whether an alias
moves without a gap in service, or whether the ACL pre-filter leaves a suspended row out
of a ranked result. Those live in ``test_phase4_index_lifecycle_live.py``, which is
docker-gated -- this module proves the requests, that one proves their effect.
"""

import json
from fnmatch import fnmatch
from uuid import UUID, uuid4

import pytest

from servicemind.domain.knowledge import (
    KnowledgeQuery,
    RetrievalMode,
    RetrievalPrincipal,
)
from servicemind.rag import opensearch
from servicemind.rag.models import DeterministicEmbeddingProvider
from servicemind.rag.opensearch import OpenSearchKnowledgeIndex

TENANT = UUID("11111111-1111-4111-8111-111111111111")
PIPELINE = OpenSearchKnowledgeIndex.PIPELINE


class _RevisionV2Embedding(DeterministicEmbeddingProvider):
    model_revision = "v2"


def _generation(index: OpenSearchKnowledgeIndex, provider) -> str:
    return index.generation(provider)


def test_generation_identity_encodes_model_revision_and_dimension() -> None:
    """Two embeddings that differ only in revision must never share a generation."""
    index = OpenSearchKnowledgeIndex(object(), dimension=16)  # type: ignore[arg-type]
    v1 = _generation(index, DeterministicEmbeddingProvider())
    v2 = _generation(index, _RevisionV2Embedding())
    assert v1 != v2
    assert len(v1) == 12
    assert all(char in "0123456789abcdef" for char in v1)
    # dimension participates too: same model+revision at another width is a new space.
    wide = OpenSearchKnowledgeIndex.generation_fingerprint(
        model_name="deterministic-test-embedding", model_revision="v1", dimension=1024
    )
    assert wide != v1


class _FakeSerializer:
    """The one method ``opensearchpy.helpers`` calls to size a bulk request."""

    @staticmethod
    def dumps(value) -> str:
        # str, not bytes: the chunker sizes each line with ``.encode("utf-8")`` itself.
        return json.dumps(value, default=str)


class _FakeTransport:
    serializer = _FakeSerializer()

    def __init__(self, owner) -> None:
        self.owner = owner

    async def perform_request(self, method: str, path: str, body=None):
        self.owner.calls.append(("transport", method, path))
        if method == "GET" and "/_search/pipeline/" in path:
            return {
                self.owner.pipeline: {
                    "phase_results_processors": [
                        {
                            "score-ranker-processor": {
                                "combination": {"technique": "rrf", "rank_constant": 60}
                            }
                        }
                    ]
                }
            }
        return None


class _FakeIndices:
    def __init__(self, owner) -> None:
        self.owner = owner

    async def create(self, *, index, body=None, ignore=0):
        self.owner.calls.append(("create", index))
        self.owner.concretes.add(index)

    async def put_mapping(self, *, index, body=None):
        self.owner.calls.append(("put_mapping", index))

    async def exists(self, *, index):
        return index in self.owner.concretes

    async def exists_alias(self, *, name):
        return bool(self.owner.aliases.get(name))

    async def get_alias(self, *, name):
        return {
            index: {"aliases": {name: {}}} for index in sorted(self.owner.aliases.get(name, set()))
        }

    async def update_aliases(self, *, body):
        self.owner.calls.append(("update_aliases", body["actions"]))
        for action in body["actions"]:
            if "add" in action:
                self.owner.aliases.setdefault(action["add"]["alias"], set()).add(
                    action["add"]["index"]
                )
            if "remove" in action:
                remove = action["remove"]
                self.owner.aliases.get(remove["alias"], set()).discard(remove["index"])

    async def get(self, *, index, allow_no_indices=True):
        self.owner.calls.append(("get", index))
        return {name: {} for name in sorted(self.owner.concretes) if fnmatch(name, index)}

    async def delete(self, *, index, ignore_unavailable=True):
        self.owner.calls.append(("delete", index))
        self.owner.concretes.discard(index)
        for alias_set in self.owner.aliases.values():
            alias_set.discard(index)

    async def refresh(self, *, index, ignore_unavailable=True, allow_no_indices=True):
        self.owner.calls.append(("refresh", index))


def _bulk_deleted_ids(client) -> set[str]:
    """The ``_id`` of every document the recorded bulk requests asked to delete."""
    deleted: set[str] = set()
    for name, *rest in client.calls:
        if name != "bulk":
            continue
        for line in (rest[0] or "").splitlines():
            action = json.loads(line).get("delete")
            if action:
                deleted.add(action["_id"])
    return deleted


def _composite_request(body: dict | None) -> tuple[str, str, int, dict | None] | None:
    """``(agg name, field, page size, after cursor)`` if this is a composite aggregation."""
    for name, aggregation in (body or {}).get("aggs", {}).items():
        composite = aggregation.get("composite")
        if composite is None:
            continue
        field = next(iter(composite["sources"][0]))
        return name, field, composite["size"], composite.get("after")
    return None


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list = []
        self.concretes: set[str] = set()
        self.aliases: dict[str, set[str]] = {}
        self.index_count: dict[str, int] = {}
        self.search_response: dict | None = None
        self.source_ids: dict[str, set[str]] = {}
        #: ``(index, field) -> values`` for aggregations other than ``source_record_id``.
        self.field_values: dict[tuple[str, str], set[str]] = {}
        self.pipeline = PIPELINE
        self.transport = _FakeTransport(self)
        self.indices = _FakeIndices(self)

    async def search(self, *, index=None, body=None, params=None):
        self.calls.append(("search", index, body, params))
        request = _composite_request(body)
        if request is not None:
            return self._composite_page(index or "", request)
        if self.search_response is not None:
            return self.search_response
        return {"hits": {"hits": []}}

    def _composite_page(self, index: str, request: tuple[str, str, int, dict | None]) -> dict:
        """Answer a composite aggregation the way a real backend does -- page by page.

        Deriving the response from the request instead of hard-coding one shape means a
        caller that reads only the first page, or ignores ``after_key``, gets a short
        answer here exactly as it would in production. The old stub always returned every
        value in one response, so a bounded read that asked for the first thousand looked
        identical to a complete one.
        """
        name, field, size, after = request
        values = sorted(self._values(index, field))
        start = (
            0
            if after is None
            else next(
                (i + 1 for i, value in enumerate(values) if value == after[field]), len(values)
            )
        )
        page = values[start : start + size]
        aggregation: dict = {"buckets": [{"key": {field: value}} for value in page]}
        if page and start + size < len(values):
            aggregation["after_key"] = {field: page[-1]}
        return {"aggregations": {name: aggregation}}

    def _values(self, index: str, field: str) -> set[str]:
        """Values for ``field`` under ``index``, resolving an alias to its concretes.

        A search against an alias reads the indices behind it -- that indirection is the
        whole point of the blue-green switch -- so the stand-in resolves it too, rather
        than only answering for a concrete name it was told about directly.
        """
        targets = self.aliases.get(index) or {index}
        found: set[str] = set()
        for target in targets:
            found |= self.field_values.get((target, field), set())
            if field == "source_record_id":
                found |= self.source_ids.get(target, set())
        return found

    async def count(self, *, index):
        self.calls.append(("count", index))
        return {"count": self.index_count.get(index, 0)}

    async def delete_by_query(self, *, index, body=None, conflicts="proceed", refresh=False):
        self.calls.append(("delete_by_query", index, body))
        return {"deleted": 1}

    async def update_by_query(self, *, index, body=None, conflicts="proceed", refresh=False):
        self.calls.append(("update_by_query", index, body))
        return {"updated": 1}

    async def bulk(self, body=None, *args, **kwargs):
        # ``helpers.async_bulk`` sends newline-joined JSON action lines; recording the
        # text as received lets a test read back exactly which ids were addressed.
        self.calls.append(("bulk", body))
        return {"errors": False, "items": []}

    async def close(self):
        return None


def _make_index(client) -> OpenSearchKnowledgeIndex:
    return OpenSearchKnowledgeIndex(client, dimension=16)  # type: ignore[arg-type]


def _principals() -> RetrievalPrincipal:
    return RetrievalPrincipal(tenant_id=TENANT, user_id="u1", entity_ids=frozenset({1}))


@pytest.mark.asyncio
async def test_publish_first_generation_creates_concrete_and_active_alias() -> None:
    client = _FakeClient()
    index = _make_index(client)
    gen = index.generation(DeterministicEmbeddingProvider())
    child = index._child_concrete(TENANT, gen)
    parent = index._parent_concrete(TENANT, gen)

    published = await index.publish(TENANT, DeterministicEmbeddingProvider())

    assert published.status == "created"
    assert published.generation == gen
    assert {child, parent} <= client.concretes
    assert client.aliases[index.child_alias(TENANT)] == {child}
    assert client.aliases[index.parent_alias(TENANT)] == {parent}
    # Pipeline is registered exactly once per process.
    puts = [call for call in client.calls if call[:2] == ("transport", "PUT")]
    assert len(puts) == 1


@pytest.mark.asyncio
async def test_publish_second_call_is_a_ready_noop() -> None:
    client = _FakeClient()
    index = _make_index(client)
    await index.publish(TENANT, DeterministicEmbeddingProvider())
    client.calls.clear()

    published = await index.publish(TENANT, DeterministicEmbeddingProvider())

    assert published.status == "ready"
    assert not any(op == "update_aliases" for op, *_ in client.calls)


@pytest.mark.asyncio
async def test_publish_switches_generation_atomically_and_retires_old() -> None:
    client = _FakeClient()
    index = _make_index(client)
    # Simulate an established previous generation (model revision v1) under the alias.
    old_gen = index.generation(DeterministicEmbeddingProvider())
    old_child = index._child_concrete(TENANT, old_gen)
    old_parent = index._parent_concrete(TENANT, old_gen)
    client.concretes.update({old_child, old_parent})
    client.aliases[index.child_alias(TENANT)] = {old_child}
    client.aliases[index.parent_alias(TENANT)] = {old_parent}

    # The new revision's generation already holds re-embedded data (blue-green build).
    new_gen = index.generation(_RevisionV2Embedding())
    new_child = index._child_concrete(TENANT, new_gen)
    new_parent = index._parent_concrete(TENANT, new_gen)
    client.index_count[new_child] = 42

    published = await index.publish(TENANT, _RevisionV2Embedding())

    assert published.status == "switched"
    assert published.previous_generation == old_gen
    # Alias moved off the old generation onto the new one.
    assert client.aliases[index.child_alias(TENANT)] == {new_child}
    assert client.aliases[index.parent_alias(TENANT)] == {new_parent}
    # Superseded concrete indices were retired (both child and parent).
    assert old_child not in client.concretes
    assert old_parent not in client.concretes
    assert new_child in client.concretes
    assert new_parent in client.concretes


@pytest.mark.asyncio
async def test_publish_defers_switch_onto_an_empty_generation() -> None:
    """A migration must not leave readers on an empty index: the alias only flips
    once the new generation holds documents (blue-green)."""
    client = _FakeClient()
    index = _make_index(client)
    old_gen = index.generation(DeterministicEmbeddingProvider())
    old_child = index._child_concrete(TENANT, old_gen)
    old_parent = index._parent_concrete(TENANT, old_gen)
    client.concretes.update({old_child, old_parent})
    client.aliases[index.child_alias(TENANT)] = {old_child}
    client.aliases[index.parent_alias(TENANT)] = {old_parent}

    new_gen = index.generation(_RevisionV2Embedding())
    new_child = index._child_concrete(TENANT, new_gen)
    client.concretes.add(new_child)  # created, but empty

    published = await index.publish(TENANT, _RevisionV2Embedding())

    assert published.status == "stale"
    assert client.aliases[index.child_alias(TENANT)] == {old_child}
    assert not any(op == "update_aliases" for op, *_ in client.calls)
    assert old_child in client.concretes
    assert new_child in client.concretes


def _publish_active_gen(client) -> str:
    index = _make_index(client)
    gen = index.generation(DeterministicEmbeddingProvider())
    child = index._child_concrete(TENANT, gen)
    parent = index._parent_concrete(TENANT, gen)
    client.concretes.update({child, parent})
    client.aliases[index.child_alias(TENANT)] = {child}
    client.aliases[index.parent_alias(TENANT)] = {parent}
    return child


@pytest.mark.asyncio
async def test_suspend_patches_is_active_on_every_generation() -> None:
    client = _FakeClient()
    index = _make_index(client)
    active_child = _publish_active_gen(client)
    pending_child = index._child_concrete(TENANT, "pending-generation")
    client.concretes.add(pending_child)
    client.calls.clear()

    await index.set_document_active(TENANT, "runbook://vpn", is_active=False)

    updates = [call for call in client.calls if call[0] == "update_by_query"]
    assert {call[1] for call in updates} == {active_child, pending_child}
    assert all(
        call[2]["query"] == {"term": {"source_record_id": "runbook://vpn"}} for call in updates
    )
    assert all(call[2]["script"]["params"] == {"active": False} for call in updates)


@pytest.mark.asyncio
async def test_delete_documents_purges_every_generation() -> None:
    client = _FakeClient()
    index = _make_index(client)
    active_child = _publish_active_gen(client)
    pending_child = index._child_concrete(TENANT, "pending-generation")
    client.concretes.add(pending_child)
    client.calls.clear()

    removed = await index.delete_documents(TENANT, {"runbook://vpn", "runbook://clock"})

    assert removed == 2
    ops = [call[0] for call in client.calls]
    assert "search" in ops  # resolve parents of the removed source records
    delete_calls = [call for call in client.calls if call[0] == "delete_by_query"]
    assert {call[1] for call in delete_calls} == {active_child, pending_child}
    # ACL pre-filter still applies: suspended / inactive rows never resurface.
    filters = index._acl_filter(_principals())
    assert {"term": {"is_active": True}} in filters


@pytest.mark.asyncio
async def test_prune_removes_orphans_from_serving_and_pending_generations() -> None:
    client = _FakeClient()
    index = _make_index(client)
    active_child = _publish_active_gen(client)
    pending_child = index._child_concrete(TENANT, "pending-generation")
    client.concretes.add(pending_child)
    client.source_ids = {
        active_child: {"keep", "removed"},
        pending_child: {"keep", "removed"},
    }
    client.calls.clear()

    removed = await index.prune_to(TENANT, {"keep"})

    assert removed == 1
    deletes = [call for call in client.calls if call[0] == "delete_by_query"]
    assert {call[1] for call in deletes} == {active_child, pending_child}


def _hit_index_body(mode: RetrievalMode) -> tuple[dict, dict | None]:
    client = _FakeClient()
    client.search_response = {"hits": {"hits": []}}
    index = _make_index(client)
    index._pipeline_ready = True  # skip transport noise; not under test
    # Searches resolve the tenant's active alias (already published in production).
    gen = index.generation(DeterministicEmbeddingProvider())
    active_child = index._child_concrete(TENANT, gen)
    client.concretes.add(active_child)
    client.aliases[index.child_alias(TENANT)] = {active_child}
    query = KnowledgeQuery(raw_query="VPN down", normalized_query="VPN down")
    import asyncio

    async def run() -> tuple[dict, dict | None]:
        await index.search(
            query,
            _principals(),
            DeterministicEmbeddingProvider(),
            mode=mode,
            candidate_k=30,
            dense_k=40,
            bm25_k=40,
        )
        _, _, body, params = client.calls[0]
        return body, params

    return asyncio.run(run())


def test_hybrid_search_builds_rrf_query_and_routes_via_pipeline() -> None:
    body, params = _hit_index_body(RetrievalMode.HYBRID)
    assert params == {"search_pipeline": PIPELINE}
    assert "hybrid" in body["query"]
    queries = body["query"]["hybrid"]["queries"]
    assert body["query"]["hybrid"]["pagination_depth"] == 40
    # Both sub-queries carry the compiled ACL pre-filter.
    assert "knn" in queries[0]["bool"]["must"][0]
    assert "multi_match" in queries[1]["bool"]["must"][0]
    assert queries[0]["bool"]["filter"]


def test_single_mode_searches_carry_acl_filter_but_no_pipeline() -> None:
    body, params = _hit_index_body(RetrievalMode.DENSE)
    assert params is None
    assert "hybrid" not in body["query"]
    must = body["query"]["bool"]["must"]
    assert "knn" in must[0]
    assert body["query"]["bool"]["filter"]

    bm25_body, bm25_params = _hit_index_body(RetrievalMode.BM25)
    assert bm25_params is None
    bm25_must = bm25_body["query"]["bool"]["must"]
    assert "multi_match" in bm25_must[0]
    assert bm25_body["query"]["bool"]["filter"]


def test_child_mapping_indexes_section_heading_for_lexical_matches() -> None:
    """The schema exposes the chunk's section path as its own analyzed field.

    A query that names a heading is often answered by a body that never repeats it
    verbatim; indexing the heading path lets the BM25 channel anchor on it while the
    dense channel gets the same context via ``child_embedding_text``.
    """
    index = OpenSearchKnowledgeIndex(object(), dimension=16)  # type: ignore[arg-type]
    props = index._child_index_body()["mappings"]["properties"]
    assert props["section_heading"]["type"] == "text"
    assert "text" in props
    assert "title" in props


def test_lexical_arms_score_section_heading_beside_body_and_title() -> None:
    """Both BM25-only and the hybrid lexical arms search body, title and section."""
    bm25_body, _ = _hit_index_body(RetrievalMode.BM25)
    fields = bm25_body["query"]["bool"]["must"][0]["multi_match"]["fields"]
    assert "text^2" in fields
    assert "title" in fields
    assert "section_heading^1.5" in fields
    hybrid_body, _ = _hit_index_body(RetrievalMode.HYBRID)
    arm_fields = hybrid_body["query"]["hybrid"]["queries"][1]["bool"]["must"][0]["multi_match"][
        "fields"
    ]
    assert "section_heading^1.5" in arm_fields


# ------------------------------------------------------------ multi-query fan-out


class _RecordingEmbedding(DeterministicEmbeddingProvider):
    """Records the last ``embed_query`` text so the dense anchor stays observable.

    The fan-out schema keeps ONE dense anchor on the normalized query (OpenSearch
    caps ``hybrid`` at 5 arms); rewrites only add lexical BM25 sub-queries, so the
    recording asserts rewrites are never embedded.
    """

    def __init__(self) -> None:
        self.last_query: str | None = None

    async def embed_query(self, text: str) -> list[float]:
        self.last_query = text
        return await super().embed_query(text)


def _fan_out_body(
    rewrites: list[str], *, use_rewrites: bool
) -> tuple[tuple[dict, dict | None], _RecordingEmbedding]:
    client = _FakeClient()
    client.search_response = {"hits": {"hits": []}}
    index = _make_index(client)
    index._pipeline_ready = True  # skip transport noise; not under test
    gen = index.generation(DeterministicEmbeddingProvider())
    active_child = index._child_concrete(TENANT, gen)
    client.concretes.add(active_child)
    client.aliases[index.child_alias(TENANT)] = {active_child}
    query = KnowledgeQuery(
        raw_query="VPN down",
        normalized_query="VPN down",
        rewritten_queries=rewrites,
    )
    embedding = _RecordingEmbedding()
    import asyncio

    async def run() -> tuple[dict, dict | None]:
        await index.search(
            query,
            _principals(),
            embedding,
            mode=RetrievalMode.HYBRID,
            use_rewrites=use_rewrites,
            candidate_k=30,
            dense_k=40,
            bm25_k=40,
        )
        _, _, body, params = client.calls[0]
        return body, params

    return asyncio.run(run()), embedding


def test_hybrid_fan_out_adds_one_lexical_arm_per_distinct_rewrite() -> None:
    rewrites = [
        "VPN connectivity is down across the gateway",
        "MFA token login keeps failing at the branch",
    ]
    (body, params), embedding = _fan_out_body(rewrites, use_rewrites=True)
    assert params == {"search_pipeline": PIPELINE}
    queries = body["query"]["hybrid"]["queries"]
    # One dense anchor + one BM25 arm per fanned-out text: normalized + 2 rewrites.
    assert len(queries) == 1 + 1 + len(rewrites)
    assert "knn" in queries[0]["bool"]["must"][0]
    # Rewrites broaden the LEXICAL channel only; the dense anchor stays on normalized
    # (OpenSearch caps hybrid at 5 arms, so we never duplicate the semantic vector).
    bm25_texts = [arm["bool"]["must"][0]["multi_match"]["query"] for arm in queries[1:]]
    assert bm25_texts == ["VPN down", *rewrites]
    assert embedding.last_query == "VPN down"  # a single anchor embedding, no batch
    # Every arm, dense or lexical, still carries the compiled ACL pre-filter.
    assert all(arm["bool"]["filter"] for arm in queries)


def test_hybrid_fan_out_stays_under_the_platform_subquery_cap() -> None:
    from servicemind.rag.opensearch import HYBRID_MAX_SUBQUERIES

    # The rewrite budget is 3; normalized + 3 rewrites + 1 dense anchor == 5 arms,
    # exactly the platform cap. A pathological query must never exceed it.
    (body, _), _ = _fan_out_body(["r1", "r2", "r3"], use_rewrites=True)
    assert len(body["query"]["hybrid"]["queries"]) <= HYBRID_MAX_SUBQUERIES


def test_hybrid_fan_out_off_builds_legacy_single_query_request() -> None:
    (body, params), embedding = _fan_out_body(
        ["VPN connectivity is down across the gateway"], use_rewrites=False
    )
    assert params == {"search_pipeline": PIPELINE}
    queries = body["query"]["hybrid"]["queries"]
    assert len(queries) == 2  # exact legacy two-arm shape
    assert embedding.last_query == "VPN down"  # rewrites never embedded
    assert queries[1]["bool"]["must"][0]["multi_match"]["query"] == "VPN down"


def test_hybrid_fan_out_with_no_rewrites_degrades_to_two_arms() -> None:
    (body, _), embedding = _fan_out_body([], use_rewrites=True)
    assert len(body["query"]["hybrid"]["queries"]) == 2
    assert embedding.last_query == "VPN down"


def test_hybrid_fan_out_drops_case_and_whitespace_duplicates() -> None:
    (body, _), embedding = _fan_out_body(
        ["VPN down", "  vpn  down  ", "connectivity issue"], use_rewrites=True
    )
    # "VPN down" (== normalized) and the case/whitespace twin collapse; two distinct
    # texts remain: the dense anchor + a single added lexical arm.
    assert embedding.last_query == "VPN down"
    assert len(body["query"]["hybrid"]["queries"]) == 1 + 2


#: Comfortably past ``opensearch._DISTINCT_PAGE``, so every test below crosses the page
#: boundary the reads used to stop at.
_BEYOND_ONE_PAGE = 1200


@pytest.mark.asyncio
async def test_indexed_document_ids_reports_the_whole_corpus_not_its_first_page() -> None:
    """A partial answer here is a generation that can never be published.

    ``reconcile`` confirms ``pending_index`` rows from this set and ``publish`` only flips
    the active alias once nothing is pending -- so when this read stopped at 10,000 the
    surplus documents stayed pending permanently and the corpus never went live, with no
    error recorded anywhere to say why.
    """
    client = _FakeClient()
    index = _make_index(client)
    child = _publish_active_gen(client)
    document_ids = {str(uuid4()) for _ in range(_BEYOND_ONE_PAGE)}
    client.field_values[(child, "document_id")] = document_ids

    assert await index.indexed_document_ids(TENANT) == {UUID(value) for value in document_ids}


@pytest.mark.asyncio
async def test_a_retraction_deletes_every_parent_of_the_records_it_removes() -> None:
    """``delete_by_query`` is unconditional; whatever this read misses is orphaned.

    The parent chunks of a source record are found by a read, and the parents themselves
    are deleted by id from that read's result -- so a parent past the first page keeps its
    row in the parent index with no child left pointing at it.
    """
    client = _FakeClient()
    index = _make_index(client)
    child = _publish_active_gen(client)
    parent_ids = {str(uuid4()) for _ in range(_BEYOND_ONE_PAGE)}
    client.field_values[(child, "parent_chunk_id")] = parent_ids

    await index._remove_source_records(child, {"PAGERDUTY-1"})

    assert _bulk_deleted_ids(client) == parent_ids


@pytest.mark.asyncio
async def test_a_filtered_distinct_read_pages_to_the_end_within_its_filter() -> None:
    """``_distinct_values`` reports the set its caller asked for, however many pages.

    The same helper answers "every document", "every parent of these records" and "every
    source record", which is why the pagination is asserted once here rather than relying
    on each caller to be right about it.
    """
    client = _FakeClient()
    child = _publish_active_gen(client)
    client.field_values[(child, "parent_chunk_id")] = {
        str(uuid4()) for _ in range(_BEYOND_ONE_PAGE)
    }

    found = await opensearch._distinct_values(
        client, child, "parent_chunk_id", query={"term": {"source_record_id": "PAGERDUTY-1"}}
    )

    assert found == client.field_values[(child, "parent_chunk_id")]
    searches = [call for call in client.calls if call[0] == "search"]
    assert len(searches) > 1, "a set larger than one page must take more than one request"
    assert all(
        call[2]["query"] == {"term": {"source_record_id": "PAGERDUTY-1"}} for call in searches
    )


@pytest.mark.asyncio
async def test_a_large_retraction_stays_one_query_clause() -> None:
    """The query a retraction sends must not grow with the size of the retraction.

    Source records are asked about by membership, and the ``bool`` shape this used to
    build spent one clause per record against ``indices.query.bool.max_clause_count``
    (1024 by default) -- so a corpus retraction of a few thousand records was a query the
    backend refuses outright, from a code path whose whole job is cleanup.
    """
    client = _FakeClient()
    index = _make_index(client)
    child = _publish_active_gen(client)
    retracted = {f"SRC-{i}" for i in range(2048)}
    client.field_values[(child, "parent_chunk_id")] = {str(uuid4())}

    await index._remove_source_records(child, retracted)

    search = next(body for name, _index, body, _params in client.calls if name == "search")
    assert search["query"] == {"terms": {"source_record_id": sorted(retracted)}}


@pytest.mark.asyncio
async def test_a_retraction_of_nothing_sends_no_query_at_all() -> None:
    client = _FakeClient()
    index = _make_index(client)
    child = _publish_active_gen(client)
    client.calls.clear()

    assert await index._remove_source_records(child, set()) == 0
    assert [call for call in client.calls if call[0] in {"search", "delete_by_query", "bulk"}] == []
