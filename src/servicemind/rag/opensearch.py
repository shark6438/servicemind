from __future__ import annotations

from typing import Any
from uuid import UUID

from opensearchpy import AsyncOpenSearch
from opensearchpy.helpers import async_bulk

from servicemind.domain.knowledge import (
    ChildChunk,
    KnowledgeDocument,
    KnowledgeQuery,
    ParentChunk,
    RetrievalHit,
    RetrievalPrincipal,
)
from servicemind.rag.models import EmbeddingProvider


class OpenSearchKnowledgeIndex:
    PIPELINE = "servicemind-rag-rrf-v1"

    def __init__(
        self, client: AsyncOpenSearch, *, prefix: str = "sm-knowledge", dimension: int = 1024
    ) -> None:
        self.client, self.prefix, self.dimension = client, prefix, dimension

    def child_index(self, tenant_id: UUID) -> str:
        return f"{self.prefix}-tenant-{tenant_id}-children-v1"

    def parent_index(self, tenant_id: UUID) -> str:
        return f"{self.prefix}-tenant-{tenant_id}-parents-v1"

    async def ensure(self, tenant_id: UUID) -> None:
        child = self.child_index(tenant_id)
        parent = self.parent_index(tenant_id)
        if not await self.client.indices.exists(index=child):
            await self.client.indices.create(
                index=child,
                body={
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
                },
            )
        else:
            await self.client.indices.put_mapping(
                index=child,
                body={"properties": {"acl_tenant_id": {"type": "keyword"}}},
            )
        if not await self.client.indices.exists(index=parent):
            await self.client.indices.create(
                index=parent,
                body={
                    "mappings": {
                        "dynamic": "strict",
                        "properties": {
                            "parent_chunk_id": {"type": "keyword"},
                            "document_id": {"type": "keyword"},
                            "content": {"type": "text", "index": False},
                            "token_count": {"type": "integer"},
                        },
                    }
                },
            )
        try:
            await self.client.transport.perform_request(
                "PUT",
                f"/_search/pipeline/{self.PIPELINE}",
                body={
                    "phase_results_processors": [
                        {
                            "score-ranker-processor": {
                                "combination": {"technique": "rrf", "rank_constant": 60}
                            }
                        }
                    ]
                },
            )
        except Exception:
            # Some OpenSearch minor versions already have an immutable identical pipeline.
            pass

    async def replace_document(
        self,
        tenant_id: UUID,
        document: KnowledgeDocument,
        parents: list[ParentChunk],
        children: list[ChildChunk],
        embedding: EmbeddingProvider,
    ) -> None:
        await self.ensure(tenant_id)
        previous = await self.client.search(
            index=self.child_index(tenant_id),
            body={
                "size": 1000,
                "_source": ["parent_chunk_id"],
                "query": {"term": {"source_record_id": document.provenance.source_record_id}},
            },
        )
        old_parent_ids = {hit["_source"]["parent_chunk_id"] for hit in previous["hits"]["hits"]}
        await self.client.delete_by_query(
            index=self.child_index(tenant_id),
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
                        "_index": self.parent_index(tenant_id),
                        "_id": parent_id,
                    }
                    for parent_id in old_parent_ids
                ],
                refresh=False,
                raise_on_error=True,
            )
        vectors = await embedding.embed_documents([x.content for x in children])
        acl = document.acl
        actions = []
        for child, vector in zip(children, vectors, strict=True):
            actions.append(
                {
                    "_op_type": "index",
                    "_index": self.child_index(tenant_id),
                    "_id": str(child.child_chunk_id),
                    "_source": {
                        "child_chunk_id": str(child.child_chunk_id),
                        "parent_chunk_id": str(child.parent_chunk_id),
                        "document_id": str(document.document_id),
                        "text": child.content,
                        "embedding": vector,
                        "title": document.title,
                        "source": document.provenance.source,
                        "source_uri": document.provenance.source_uri,
                        "source_record_id": document.provenance.source_record_id,
                        "source_version": document.provenance.source_version,
                        "license": document.provenance.license,
                        "authority_level": int(document.provenance.authority_level),
                        "synthetic": document.provenance.synthetic,
                        "content_hash": child.content_hash,
                        "corpus_scope": acl.corpus_scope.value,
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
                "_index": self.parent_index(tenant_id),
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

    async def refresh(self, tenant_id: UUID) -> None:
        await self.client.indices.refresh(
            index=f"{self.child_index(tenant_id)},{self.parent_index(tenant_id)}"
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
        dense_k: int = 40,
        sparse_k: int = 40,
        candidate_k: int = 30,
    ) -> list[RetrievalHit]:
        vector = await embedding.embed_query(query.normalized_query)
        filters = self._acl_filter(principal)
        body = {
            "size": candidate_k,
            "_source": {"excludes": ["embedding"]},
            "query": {
                "hybrid": {
                    "queries": [
                        {
                            "bool": {
                                "must": [{"knn": {"embedding": {"vector": vector, "k": dense_k}}}],
                                "filter": filters,
                            }
                        },
                        {
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
                    ]
                }
            },
        }
        response = await self.client.search(
            index=self.child_index(principal.tenant_id),
            body=body,
            params={"search_pipeline": self.PIPELINE},
        )
        return [
            self._hit(x) for x in response["hits"]["hits"][: max(dense_k, sparse_k, candidate_k)]
        ]

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
        response = await self.client.mget(
            index=self.parent_index(tenant_id), body={"ids": [str(x) for x in ids]}
        )
        return {UUID(x["_id"]): x["_source"]["content"] for x in response["docs"] if x.get("found")}

    async def indexed_document_ids(self, tenant_id: UUID) -> set[UUID]:
        response = await self.client.search(
            index=self.child_index(tenant_id),
            body={
                "size": 10000,
                "_source": ["document_id"],
                "query": {"match_all": {}},
                "collapse": {"field": "document_id"},
            },
        )
        return {UUID(hit["_source"]["document_id"]) for hit in response["hits"]["hits"]}
