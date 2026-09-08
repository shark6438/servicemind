from __future__ import annotations

import hashlib
import logging
import re
from typing import Any
from uuid import UUID

from opensearchpy import AsyncOpenSearch, NotFoundError
from opensearchpy.helpers import async_bulk
from pydantic import BaseModel, ConfigDict

from servicemind.domain.knowledge import (
    ChildChunk,
    KnowledgeDocument,
    KnowledgeQuery,
    ParentChunk,
    RetrievalHit,
    RetrievalMode,
    RetrievalPrincipal,
)
from servicemind.rag.models import EmbeddingProvider

logger = logging.getLogger("servicemind.rag.opensearch")

#: A search index may only ever mix vectors produced by the same embedding model +
#: revision + dimension. Changing any of them silently changes the vector space and
#: destroys Recall of previously indexed chunks (Phase 4 baseline §5/§6), so the
#: identity of a generation is a digest of exactly those inputs plus the mapping
#: schema. Concrete indices embed this digest; a fixed "active" alias per tenant
#: points at the current generation and is flipped atomically (blue-green publish).
SCHEMA_VERSION = "v1"

_GENERATION_SUFFIX = re.compile(r"-(?:children|parents)-([^/]+)$")


class GenerationStatus(BaseModel):
    """Result of an alias publish / generation query.

    ``status``:
      - ``created``  -- first generation; active alias(es) written.
      - ``ready``    -- desired generation is already what the alias points to.
      - ``switched`` -- alias atomically moved from a previous generation.
      - ``stale``    -- no desired-generation data yet; publish deferred so readers
                        keep hitting the previous (still non-empty) generation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    generation: str
    previous_generation: str | None = None
    reindexed_documents: int = 0


def _generation_of(index_name: str) -> str | None:
    match = _GENERATION_SUFFIX.search(index_name)
    return match.group(1) if match else None


#: OpenSearch ``hybrid`` refuses more than this many sub-queries in one request.
#: The legacy shape is 2 (one dense + one BM25); the fan-out budget below therefore
#: allows at most 3 extra BM25 arms, which is exactly the query rewrite budget.
HYBRID_MAX_SUBQUERIES = 5


def _fan_out_texts(normalized: str, rewrites: list[str], *, use_rewrites: bool) -> list[str]:
    """Distinct query texts whose lexical arms the hybrid fan-out executes.

    Whitespace-collapses each rewrite and drops variants that are empty or identical
    (case-insensitively) to the normalized query, so a query with no useful rewrites
    degrades to exactly today's two-arm request. The dense channel is never fanned
    out (a single embedding anchors the semantics); rewrites only add BM25 sub-queries
    because OpenSearch caps ``hybrid`` at ``HYBRID_MAX_SUBQUERIES`` arms and each
    paraphrase is a term-level variant, not a second semantic point.
    """
    if not use_rewrites or not rewrites:
        return [normalized]
    texts = [normalized]
    seen = {normalized.casefold()}
    for rewrite in rewrites:
        text = " ".join(rewrite.split())
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        texts.append(text)
    # One BM25 arm per text on top of the single dense arm must fit the platform cap.
    return texts[: HYBRID_MAX_SUBQUERIES - 1]


class OpenSearchKnowledgeIndex:
    #: Cluster-wide search pipeline fusing dense + BM25 with reciprocal rank fusion.
    #: PUT is idempotent (replaces the definition), so we no longer swallow failures.
    PIPELINE = "servicemind-rag-rrf-v1"
    ACTIVE_ALIAS = "active"

    def __init__(
        self, client: AsyncOpenSearch, *, prefix: str = "sm-knowledge", dimension: int = 1024
    ) -> None:
        self.client, self.prefix, self.dimension = client, prefix, dimension
        self._pipeline_ready = False

    # ------------------------------------------------------------------ identity
    @classmethod
    def generation_fingerprint(cls, *, model_name: str, model_revision: str, dimension: int) -> str:
        material = f"{model_name}|{model_revision}|{dimension}|{SCHEMA_VERSION}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]

    def generation(self, embedding: EmbeddingProvider) -> str:
        return self.generation_fingerprint(
            model_name=embedding.model_name,
            model_revision=embedding.model_revision,
            dimension=int(getattr(embedding, "dimension", self.dimension)),
        )

    # ------------------------------------------------------------------- naming
    def child_alias(self, tenant_id: UUID) -> str:
        return f"{self.prefix}-tenant-{tenant_id}-children-{self.ACTIVE_ALIAS}"

    def parent_alias(self, tenant_id: UUID) -> str:
        return f"{self.prefix}-tenant-{tenant_id}-parents-{self.ACTIVE_ALIAS}"

    def _child_concrete(self, tenant_id: UUID, generation: str) -> str:
        return f"{self.prefix}-tenant-{tenant_id}-children-{generation}"

    def _parent_concrete(self, tenant_id: UUID, generation: str) -> str:
        return f"{self.prefix}-tenant-{tenant_id}-parents-{generation}"

    @staticmethod
    def _parent_of(child_concrete: str) -> str:
        return child_concrete.replace("-children-", "-parents-")

    @staticmethod
    def _child_of(parent_concrete: str) -> str:
        return parent_concrete.replace("-parents-", "-children-")

    def _child_pattern(self, tenant_id: UUID) -> str:
        return f"{self.prefix}-tenant-{tenant_id}-children-*"

    def _parent_pattern(self, tenant_id: UUID) -> str:
        return f"{self.prefix}-tenant-{tenant_id}-parents-*"

    # ----------------------------------------------------------------- mappings
    def _child_index_body(self) -> dict[str, Any]:
        return {
            "settings": {"index": {"knn": True}},
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "child_chunk_id": {"type": "keyword"},
                    "parent_chunk_id": {"type": "keyword"},
                    "document_id": {"type": "keyword"},
                    "text": {"type": "text"},
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": self.dimension,
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": "lucene",
                        },
                    },
                    "title": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                    "source": {"type": "keyword"},
                    "source_uri": {"type": "keyword", "index": False},
                    "source_record_id": {"type": "keyword"},
                    "source_version": {"type": "keyword"},
                    "license": {"type": "keyword"},
                    "authority_level": {"type": "integer"},
                    "synthetic": {"type": "boolean"},
                    "content_hash": {"type": "keyword"},
                    "corpus_scope": {"type": "keyword"},
                    #: Generation identity of the vectors in this row. Because reads
                    #: resolve the "active" alias, ``index_version`` is redundant for
                    #: pre-filtering (Phase 4 baseline §6) but is stored per row for
                    #: telemetry, lineage audits and cross-generation reconciliation.
                    "index_version": {"type": "keyword"},
                    "tenant_id": {"type": "keyword"},
                    "acl_tenant_id": {"type": "keyword"},
                    "entity_ids": {"type": "integer"},
                    "group_ids": {"type": "integer"},
                    "profile_ids": {"type": "integer"},
                    "user_ids": {"type": "keyword"},
                    "effective_from": {"type": "date"},
                    "effective_to": {"type": "date"},
                    "is_active": {"type": "boolean"},
                },
            },
        }

    @staticmethod
    def _parent_index_body() -> dict[str, Any]:
        return {
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "parent_chunk_id": {"type": "keyword"},
                    "document_id": {"type": "keyword"},
                    "content": {"type": "text", "index": False},
                    "token_count": {"type": "integer"},
                },
            }
        }

    def _pipeline_body(self) -> dict[str, Any]:
        return {
            "phase_results_processors": [
                {
                    "score-ranker-processor": {
                        "combination": {"technique": "rrf", "rank_constant": 60}
                    }
                }
            ]
        }

    async def _ensure_pipeline(self) -> None:
        if self._pipeline_ready:
            return
        try:
            await self.client.transport.perform_request(
                "PUT", f"/_search/pipeline/{self.PIPELINE}", body=self._pipeline_body()
            )
            self._pipeline_ready = True
            return
        except Exception as put_error:  # noqa: BLE001 - transport may raise anything
            # PUT is idempotent, so an immutable/cluster-wide conflict is acceptable
            # only when the stored definition already matches ours.
            try:
                existing = await self.client.transport.perform_request(
                    "GET", f"/_search/pipeline/{self.PIPELINE}"
                )
            except Exception:
                raise put_error
            definition = existing.get(self.PIPELINE)
            if (
                definition
                and definition.get("phase_results_processors")
                == self._pipeline_body()["phase_results_processors"]
            ):
                self._pipeline_ready = True
                return
            raise RuntimeError(
                f"search pipeline {self.PIPELINE} exists but differs from the RRF "
                "definition this build requires; rename or update it manually"
            ) from put_error

    async def _ensure_concrete(
        self, tenant_id: UUID, generation: str, *, embedding: EmbeddingProvider
    ) -> tuple[str, str]:
        child = self._child_concrete(tenant_id, generation)
        parent = self._parent_concrete(tenant_id, generation)
        # ``ignore=400`` keeps concurrent creates idempotent (race-safe under the
        # ingest semaphore). A pre-existing index without the index_version column
        # is backfilled by the additive put_mapping below (legacy migration).
        await self.client.indices.create(index=child, body=self._child_index_body(), ignore=400)
        await self.client.indices.put_mapping(
            index=child, body={"properties": {"index_version": {"type": "keyword"}}}
        )
        await self.client.indices.create(index=parent, body=self._parent_index_body(), ignore=400)
        return child, parent

    async def _ensure_pipeline_then_concrete(
        self, tenant_id: UUID, embedding: EmbeddingProvider
    ) -> tuple[str, str]:
        generation = self.generation(embedding)
        await self._ensure_pipeline()
        return await self._ensure_concrete(tenant_id, generation, embedding=embedding)

    # ----------------------------------------------------------------- lifecycle
    async def ensure(self, tenant_id: UUID, embedding: EmbeddingProvider) -> str:
        """Make sure the concrete indices for the embedding's generation exist.

        Never touches aliases: readers keep resolving the previously published
        generation until ``publish`` flips them (blue-green). Returns the desired
        generation fingerprint.
        """
        child, _ = await self._ensure_pipeline_then_concrete(tenant_id, embedding)
        return _generation_of(child) or self.generation(embedding)

    async def _resolve_alias(self, alias: str) -> set[str]:
        try:
            if not await self.client.indices.exists_alias(name=alias):
                return set()
            raw = await self.client.indices.get_alias(name=alias)
        except NotFoundError:
            return set()
        return set(raw)

    async def active_generation(self, tenant_id: UUID) -> str | None:
        """Fingerprint of the generation currently behind the tenant's active alias."""
        concrete = await self._resolve_alias(self.child_alias(tenant_id))
        for name in sorted(concrete):
            generation = _generation_of(name)
            if generation:
                return generation
        return None

    async def publish(self, tenant_id: UUID, embedding: EmbeddingProvider) -> GenerationStatus:
        """Atomically point the active aliases at the desired generation.

        Ordering matters for a non-empty previous corpus: we only switch when the
        desired generation already holds documents (or nothing was published before),
        so readers never observe an empty window. Superseded concrete indices are
        deleted only after the alias has moved off them.
        """
        generation = self.generation(embedding)
        await self._ensure_pipeline_then_concrete(tenant_id, embedding)
        child = self._child_concrete(tenant_id, generation)
        parent = self._parent_concrete(tenant_id, generation)

        previous = await self.active_generation(tenant_id)
        if previous == generation:
            return GenerationStatus(status="ready", generation=generation)

        if previous is not None:
            # Prefer not to switch onto an empty index when a previous (non-empty)
            # generation is still serving. An interrupted migration is finished by
            # re-running ingestion; the alias only flips once data has landed.
            counts = await self.client.count(index=child)
            if int(counts.get("count", 0)) == 0:
                return GenerationStatus(
                    status="stale",
                    generation=generation,
                    previous_generation=previous,
                )

        actions: list[dict[str, Any]] = []
        for alias, index in (
            (self.child_alias(tenant_id), child),
            (self.parent_alias(tenant_id), parent),
        ):
            active = await self._resolve_alias(alias)
            for old in active:
                actions.append({"remove": {"index": old, "alias": alias}})
            actions.append({"add": {"index": index, "alias": alias}})
        await self.client.indices.update_aliases(body={"actions": actions})
        await self._retire_superseded(tenant_id, keep_generation=generation)
        return GenerationStatus(
            status="created" if previous is None else "switched",
            generation=generation,
            previous_generation=previous,
        )

    async def _list_concrete(self, pattern: str) -> list[str]:
        try:
            raw = await self.client.indices.get(index=pattern, allow_no_indices=True)
        except NotFoundError:
            return []
        return sorted(raw)

    async def _retire_superseded(self, tenant_id: UUID, *, keep_generation: str) -> None:
        """Delete concrete generations the active alias no longer points at."""
        doomed: list[str] = []
        for pattern, sibling in (
            (self._child_pattern(tenant_id), self._parent_of),
            (self._parent_pattern(tenant_id), self._child_of),
        ):
            for name in await self._list_concrete(pattern):
                generation = _generation_of(name)
                if generation and generation != keep_generation:
                    doomed.append(name)
                    doomed.append(sibling(name))
        doomed = sorted(set(doomed))
        if not doomed:
            return
        for index in doomed:
            await self.client.indices.delete(index=index, ignore_unavailable=True)
        logger.info(
            "retired superseded RAG generations for tenant=%s: %d indices",
            tenant_id,
            len(doomed),
        )

    async def drop_tenant_indices(self, tenant_id: UUID) -> None:
        """Delete every concrete index this object's ``prefix`` owns for a tenant.

        Scoped strictly to ``self.prefix`` (e.g. the isolated ``sm-rag-eval-*``
        prefixes used by the evaluation harness); production tenants live under a
        different prefix and are never matched. Aliases are removed implicitly when
        their last target index is deleted.
        """
        for pattern in (self._child_pattern(tenant_id), self._parent_pattern(tenant_id)):
            await self.client.indices.delete(
                index=pattern, ignore_unavailable=True, allow_no_indices=True
            )

    # -------------------------------------------------------------------- writes
    async def replace_document(
        self,
        tenant_id: UUID,
        document: KnowledgeDocument,
        parents: list[ParentChunk],
        children: list[ChildChunk],
        embedding: EmbeddingProvider,
    ) -> None:
        """Write one document's chunks into its embedding's concrete generation.

        Vectors produced by the current embedding are never written into an older
        generation (vector spaces must not mix); writes therefore target the desired
        generation, which may differ from the active one during a model upgrade.
        """
        generation = await self.ensure(tenant_id, embedding)
        child = self._child_concrete(tenant_id, generation)
        parent = self._parent_concrete(tenant_id, generation)

        previous = await self.client.search(
            index=child,
            body={
                "size": 1000,
                "_source": ["parent_chunk_id"],
                "query": {"term": {"source_record_id": document.provenance.source_record_id}},
            },
        )
        old_parent_ids = {hit["_source"]["parent_chunk_id"] for hit in previous["hits"]["hits"]}
        await self.client.delete_by_query(
            index=child,
            body={"query": {"term": {"source_record_id": document.provenance.source_record_id}}},
            conflicts="proceed",
            refresh=False,
        )
        if old_parent_ids:
            await async_bulk(
                self.client,
                [
                    {
                        "_op_type": "delete",
                        "_index": parent,
                        "_id": parent_id,
                    }
                    for parent_id in old_parent_ids
                ],
                refresh=False,
                raise_on_error=True,
            )
        vectors = await embedding.embed_documents([x.content for x in children])
        acl = document.acl
        actions: list[dict[str, Any]] = []
        for chunk, vector in zip(children, vectors, strict=True):
            actions.append(
                {
                    "_op_type": "index",
                    "_index": child,
                    "_id": str(chunk.child_chunk_id),
                    "_source": {
                        "child_chunk_id": str(chunk.child_chunk_id),
                        "parent_chunk_id": str(chunk.parent_chunk_id),
                        "document_id": str(document.document_id),
                        "text": chunk.content,
                        "embedding": vector,
                        "title": document.title,
                        "source": document.provenance.source,
                        "source_uri": document.provenance.source_uri,
                        "source_record_id": document.provenance.source_record_id,
                        "source_version": document.provenance.source_version,
                        "license": document.provenance.license,
                        "authority_level": int(document.provenance.authority_level),
                        "synthetic": document.provenance.synthetic,
                        "content_hash": chunk.content_hash,
                        "corpus_scope": acl.corpus_scope.value,
                        "index_version": generation,
                        "tenant_id": str(tenant_id),
                        "acl_tenant_id": str(acl.tenant_id) if acl.tenant_id else None,
                        "entity_ids": list(acl.entity_ids),
                        "group_ids": list(acl.group_ids),
                        "profile_ids": list(acl.profile_ids),
                        "user_ids": list(acl.user_ids),
                        "effective_from": acl.effective_from.isoformat(),
                        "effective_to": acl.effective_to.isoformat() if acl.effective_to else None,
                        "is_active": acl.is_active,
                    },
                }
            )
        actions += [
            {
                "_op_type": "index",
                "_index": parent,
                "_id": str(x.parent_chunk_id),
                "_source": {
                    "parent_chunk_id": str(x.parent_chunk_id),
                    "document_id": str(x.document_id),
                    "content": x.content,
                    "token_count": x.token_count,
                },
            }
            for x in parents
        ]
        if actions:
            await async_bulk(self.client, actions, refresh=False, raise_on_error=True)

    async def _active_child_concrete(self, tenant_id: UUID) -> str | None:
        concrete = await self._resolve_alias(self.child_alias(tenant_id))
        for name in sorted(concrete):
            if _generation_of(name):
                return name
        return None

    async def _remove_source_records(self, child_concrete: str, source_record_ids: set[str]) -> int:
        if not source_record_ids:
            return 0
        terms: list[dict[str, Any]] = [
            {"term": {"source_record_id": value}} for value in source_record_ids
        ]
        parent_lookup = await self.client.search(
            index=child_concrete,
            body={
                "size": 10000,
                "_source": ["parent_chunk_id"],
                "query": {"bool": {"should": terms, "minimum_should_match": 1}},
            },
        )
        parent_ids = {hit["_source"]["parent_chunk_id"] for hit in parent_lookup["hits"]["hits"]}
        removed = await self.client.delete_by_query(
            index=child_concrete,
            body={"query": {"bool": {"should": terms, "minimum_should_match": 1}}},
            conflicts="proceed",
            refresh=False,
        )
        if parent_ids:
            await async_bulk(
                self.client,
                [
                    {"_op_type": "delete", "_index": self._parent_of(child_concrete), "_id": pid}
                    for pid in parent_ids
                ],
                refresh=False,
                raise_on_error=True,
            )
        return int(removed.get("deleted", 0))

    async def delete_documents(self, tenant_id: UUID, source_record_ids: set[str]) -> int:
        """Remove documents (unpublish) from every retained generation.

        PostgreSQL remains the authority; this only keeps the search projection in
        lock-step with repository-level deletion. Applying the retraction to a
        blue-green generation under construction prevents a deleted document from
        resurfacing when that generation is published.
        """
        removed = 0
        for child in await self._list_concrete(self._child_pattern(tenant_id)):
            removed += await self._remove_source_records(child, source_record_ids)
        return removed

    async def set_document_active(
        self, tenant_id: UUID, source_record_id: str, *, is_active: bool
    ) -> None:
        """Suspend/activate a document in every retained search generation.

        The document-level ``is_active`` flag is ACL, so PostgreSQL is updated by the
        repository first. Patching all blue-green generations prevents a suspended
        document from becoming visible again after an alias switch.
        """
        for child in await self._list_concrete(self._child_pattern(tenant_id)):
            await self.client.update_by_query(
                index=child,
                conflicts="proceed",
                refresh=False,
                body={
                    "query": {"term": {"source_record_id": source_record_id}},
                    "script": {
                        "source": "ctx._source['is_active'] = params.active",
                        "lang": "painless",
                        "params": {"active": is_active},
                    },
                },
            )

    async def prune_to(self, tenant_id: UUID, active_source_record_ids: set[str]) -> int:
        """Drop index rows whose source record no longer exists in PostgreSQL.

        Used by reconciliation so a corpus retraction (source delete / cancellation)
        propagates to both the serving generation and any blue-green generation being
        built. Returns the number of distinct stale source records removed.
        """
        all_stale: set[str] = set()
        for child in await self._list_concrete(self._child_pattern(tenant_id)):
            present: set[str] = set()
            after: dict[str, Any] | None = None
            while True:
                aggregation: dict[str, Any] = {
                    "sources": {
                        "composite": {
                            "size": 1000,
                            "sources": [
                                {
                                    "source_record_id": {
                                        "terms": {
                                            "field": "source_record_id",
                                            "order": "asc",
                                        }
                                    }
                                }
                            ],
                        }
                    }
                }
                if after is not None:
                    aggregation["sources"]["composite"]["after"] = after  # type: ignore[index]
                response = await self.client.search(
                    index=child, body={"size": 0, "aggs": aggregation}
                )
                composite = response["aggregations"]["sources"]
                for bucket in composite["buckets"]:
                    present.add(bucket["key"]["source_record_id"])
                if "after_key" not in composite:
                    break
                after = composite["after_key"]
            stale = present - active_source_record_ids
            if stale:
                await self._remove_source_records(child, stale)
                all_stale.update(stale)
        return len(all_stale)

    # --------------------------------------------------------------------- reads
    async def refresh(self, tenant_id: UUID) -> None:
        """Refresh every concrete generation of the tenant (aliases stay untouched)."""
        await self.client.indices.refresh(
            index=f"{self._child_pattern(tenant_id)},{self._parent_pattern(tenant_id)}",
            ignore_unavailable=True,
            allow_no_indices=True,
        )

    def _acl_filter(self, principal: RetrievalPrincipal) -> list[dict[str, Any]]:
        now = principal.query_time.isoformat()

        def unrestricted_or(field: str, values: list[Any]):
            options = [{"bool": {"must_not": {"exists": {"field": field}}}}]
            if values:
                dynamic_terms: dict[str, Any] = {field: values}
                options.append({"terms": dynamic_terms})
            return {"bool": {"should": options, "minimum_should_match": 1}}

        return [
            {"term": {"tenant_id": str(principal.tenant_id)}},
            {"term": {"is_active": True}},
            {"range": {"effective_from": {"lte": now}}},
            {
                "bool": {
                    "should": [
                        {"bool": {"must_not": {"exists": {"field": "effective_to"}}}},
                        {"range": {"effective_to": {"gt": now}}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            unrestricted_or("entity_ids", list(principal.entity_ids)),
            unrestricted_or("group_ids", list(principal.group_ids)),
            unrestricted_or("profile_ids", list(principal.profile_ids)),
            unrestricted_or("user_ids", [principal.user_id]),
        ]

    async def search(
        self,
        query: KnowledgeQuery,
        principal: RetrievalPrincipal,
        embedding: EmbeddingProvider,
        *,
        mode: RetrievalMode = RetrievalMode.HYBRID,
        use_rewrites: bool = False,
        dense_k: int = 40,
        bm25_k: int = 40,
        candidate_k: int = 30,
    ) -> list[RetrievalHit]:
        """Search the tenant's active generation under the compiled ACL pre-filter.

        ``mode`` selects the candidate channel (dense / bm25 / hybrid+RRF) -- the
        evaluation harness and agent both need it; ACL filtering is identical in all
        three, so mode is never a way to widen visibility.

        ``use_rewrites`` (hybrid only) fans the query out at the lexical channel: one
        BM25 sub-query per distinct ``rewritten_queries`` paraphrase, on top of the
        unchanged single dense anchor, all inside one OpenSearch ``hybrid`` query so
        the cluster's RRF pipeline merges them. OpenSearch caps ``hybrid`` at 5
        sub-queries, which is why rewrites expand BM25 (term-level) coverage instead
        of spawning duplicate dense arms; the rerank step still scores against the
        normalized query. With no rewrites it builds exactly the legacy two-arm
        request.
        """
        alias = self.child_alias(principal.tenant_id)
        if not await self._resolve_alias(alias):
            logger.warning("no active RAG generation for tenant=%s", principal.tenant_id)
            return []
        filters = self._acl_filter(principal)
        params: dict[str, str] = {}
        if mode is RetrievalMode.HYBRID:
            texts = _fan_out_texts(
                query.normalized_query, query.rewritten_queries, use_rewrites=use_rewrites
            )
            vector = await embedding.embed_query(query.normalized_query)
            queries: list[dict[str, Any]] = [
                {
                    "bool": {
                        "must": [{"knn": {"embedding": {"vector": vector, "k": dense_k,
                                  "filter": {"bool": {"filter": filters}}}}}],
                        "filter": filters,
                    }
                }
            ]
            queries.extend(
                {
                    "bool": {
                        "must": [
                            {
                                "multi_match": {
                                    "query": text,
                                    "fields": ["text^2", "title"],
                                }
                            }
                        ],
                        "filter": filters,
                    }
                }
                for text in texts
            )
            body = {
                "size": candidate_k,
                "_source": {"excludes": ["embedding"]},
                "query": {"hybrid": {"queries": queries}},
            }
            params = {"search_pipeline": self.PIPELINE}
        elif mode is RetrievalMode.DENSE:
            vector = await embedding.embed_query(query.normalized_query)
            body = {
                "size": max(candidate_k, dense_k),
                "_source": {"excludes": ["embedding"]},
                "query": {
                    "bool": {
                        "must": [{"knn": {"embedding": {"vector": vector, "k": dense_k,
                                  "filter": {"bool": {"filter": filters}}}}}],
                        "filter": filters,
                    }
                },
            }
        elif mode is RetrievalMode.BM25:
            body = {
                "size": max(candidate_k, bm25_k),
                "query": {
                    "bool": {
                        "must": [
                            {
                                "multi_match": {
                                    "query": query.normalized_query,
                                    "fields": ["text^2", "title"],
                                }
                            }
                        ],
                        "filter": filters,
                    }
                },
            }
        else:  # pragma: no cover - guarded by the enum
            raise ValueError(f"unsupported retrieval mode: {mode}")

        response = await self.client.search(index=alias, body=body, params=params or None)
        return [self._hit(value) for value in response["hits"]["hits"]]

    def _hit(self, value: dict[str, Any]) -> RetrievalHit:
        x = value["_source"]
        acl_tenant_id = x.get("acl_tenant_id")
        if x["corpus_scope"] == "tenant" and acl_tenant_id is None:
            acl_tenant_id = x["tenant_id"]
        return RetrievalHit(
            child_chunk_id=x["child_chunk_id"],
            parent_chunk_id=x["parent_chunk_id"],
            document_id=x["document_id"],
            child_content=x["text"],
            score=value["_score"],
            title=x["title"],
            source=x["source"],
            source_uri=x["source_uri"],
            source_record_id=x["source_record_id"],
            source_version=x["source_version"],
            license=x["license"],
            authority_level=x["authority_level"],
            synthetic=x["synthetic"],
            content_hash=x["content_hash"],
            index_version=x.get("index_version"),
            acl={
                "corpus_scope": x["corpus_scope"],
                "tenant_id": acl_tenant_id,
                "entity_ids": x.get("entity_ids", []),
                "group_ids": x.get("group_ids", []),
                "profile_ids": x.get("profile_ids", []),
                "user_ids": x.get("user_ids", []),
                "effective_from": x["effective_from"],
                "effective_to": x.get("effective_to"),
                "is_active": x["is_active"],
            },
        )

    async def parents(self, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, str]:
        if not ids:
            return {}
        alias = self.parent_alias(tenant_id)
        if not await self._resolve_alias(alias):
            return {}
        response = await self.client.mget(index=alias, body={"ids": [str(value) for value in ids]})
        return {
            UUID(doc["_id"]): doc["_source"]["content"]
            for doc in response["docs"]
            if doc.get("found")
        }

    async def indexed_document_ids(self, tenant_id: UUID) -> set[UUID]:
        """Document ids currently retrievable via the tenant's active generation."""
        alias = self.child_alias(tenant_id)
        if not await self._resolve_alias(alias):
            return set()
        response = await self.client.search(
            index=alias,
            body={
                "size": 10000,
                "_source": ["document_id"],
                "query": {"match_all": {}},
                "collapse": {"field": "document_id"},
            },
        )
        return {UUID(hit["_source"]["document_id"]) for hit in response["hits"]["hits"]}
