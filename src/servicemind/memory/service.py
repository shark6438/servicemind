from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Any, Protocol
from uuid import UUID

from servicemind.memory.contracts import (
    MemoryCandidate,
    MemoryQuery,
    MemoryRecord,
    MemorySelection,
)
from servicemind.memory.policy import MemoryGovernancePolicy, contains_injection_marker
from servicemind.memory.repository import MemoryRepository

logger = logging.getLogger("servicemind.memory.service")

TOKEN = re.compile(r"[a-z0-9_\-]+|[\u4e00-\u9fff]", re.I)


def _carries_injection(content: str) -> bool:
    return contains_injection_marker(content)


class MemoryEmbeddingProvider(Protocol):
    async def embed_query(self, text: str) -> list[float]: ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class CachedMemoryEmbeddingProvider:
    """Bounded process cache over a pinned embedding provider's derived vectors."""

    def __init__(self, provider: MemoryEmbeddingProvider, *, max_entries: int = 512) -> None:
        if max_entries < 1:
            raise ValueError("memory embedding cache must contain at least one entry")
        self.provider = provider
        self.max_entries = max_entries
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(text: str) -> str:
        return sha256(text.encode()).hexdigest()

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        async with self._lock:
            missing: dict[str, str] = {}
            existing = dict(self._cache)
            resolved: dict[str, list[float]] = {}
            for value in texts:
                key = self._key(value)
                if key not in self._cache:
                    missing.setdefault(key, value)
            if missing:
                vectors = await self.provider.embed_documents(list(missing.values()))
                if len(vectors) != len(missing):
                    raise ValueError("embedding provider returned an invalid vector count")
                resolved = dict(zip(missing, vectors, strict=True))
                for key, vector in resolved.items():
                    self._cache[key] = vector
                    self._cache.move_to_end(key)
                    while len(self._cache) > self.max_entries:
                        self._cache.popitem(last=False)
            result: list[list[float]] = []
            for value in texts:
                key = self._key(value)
                # A batch can exceed cache capacity. Its earlier vectors may have
                # already been evicted; retain request-local results independently.
                vector = resolved[key] if key in missing else existing[key]
                if key in self._cache:
                    self._cache.move_to_end(key)
                result.append(list(vector))
            return result

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]


class MemoryWriter:
    def __init__(self, repository: MemoryRepository, policy: MemoryGovernancePolicy | None = None):
        self.repository = repository
        self.policy = policy or MemoryGovernancePolicy()

    async def write(self, candidate: MemoryCandidate) -> MemoryRecord | None:
        return await self.repository.persist(candidate, self.policy.assess(candidate))


def _lexical_similarity(left: str, right: str) -> float:
    a = set(TOKEN.findall(left.casefold()))
    b = set(TOKEN.findall(right.casefold()))
    return len(a & b) / len(a | b) if a and b else 0.0


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or not all(math.isfinite(v) for v in [*left, *right]):
        raise ValueError("embedding provider returned empty or non-finite vectors")
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    lnorm = math.sqrt(sum(value * value for value in left))
    rnorm = math.sqrt(sum(value * value for value in right))
    if not lnorm or not rnorm:
        return 0.0
    return max(0.0, min(1.0, numerator / (lnorm * rnorm)))


#: Metadata may only break ties between comparably relevant memories; it must
#: never manufacture relevance. A flat sum spent 0.55 of the budget on metadata
#: and only 0.45 on relevance, so a record sitting exactly on
#: ``min_semantic_similarity`` with full metadata scored 0.7075 and outranked a
#: perfectly relevant but stale, low-confidence one (0.4500) -- the opposite of
#: what the relevance guard below claims to guarantee. Relevance therefore keeps
#: the dominant share and metadata is capped at ``METADATA_WEIGHT``.
METADATA_WEIGHT = 0.15

#: Composition of the metadata tie-breaker itself (sums to one).
_METADATA_MIX = {"recency": 0.4, "confidence": 0.3, "importance": 0.2, "provenance": 0.1}


def metadata_quality(
    *, recency: float, confidence: float, importance: float, provenance: float
) -> float:
    """Collapse the metadata signals into a single bounded tie-breaker in [0, 1]."""
    return (
        _METADATA_MIX["recency"] * recency
        + _METADATA_MIX["confidence"] * confidence
        + _METADATA_MIX["importance"] * importance
        + _METADATA_MIX["provenance"] * provenance
    )


def memory_relevance_score(semantic: float, metadata: float) -> float:
    """Blend relevance with a bounded metadata tie-breaker, relevance dominant.

    Invariant (asserted by the tests): a record at the similarity floor carrying
    maximal metadata scores ``(1 - METADATA_WEIGHT) * floor + METADATA_WEIGHT``,
    which stays below ``1 - METADATA_WEIGHT`` -- the score of a fully relevant
    record carrying no metadata -- for every ``floor`` below
    ``1 - METADATA_WEIGHT / (1 - METADATA_WEIGHT)`` (0.394 here). Metadata can
    therefore reorder comparably relevant memories but can never promote an
    irrelevant one above a relevant one.
    """
    return (1.0 - METADATA_WEIGHT) * semantic + METADATA_WEIGHT * metadata


class MemoryRetriever:
    """Scope-first retrieval followed by ranking and authority revalidation."""

    def __init__(
        self,
        repository: MemoryRepository,
        embedding: MemoryEmbeddingProvider | None = None,
        candidate_ceiling: int = 100,
        min_semantic_similarity: float = 0.35,
        min_lexical_similarity: float = 0.05,
    ) -> None:
        if not 1 <= candidate_ceiling <= 500:
            raise ValueError("memory candidate ceiling must be between 1 and 500")
        self.repository = repository
        self.embedding = embedding
        self.candidate_ceiling = candidate_ceiling
        if not 0 <= min_semantic_similarity <= 1:
            raise ValueError("minimum semantic similarity must be between zero and one")
        if not 0 <= min_lexical_similarity <= 1:
            raise ValueError("minimum lexical similarity must be between zero and one")
        self.min_semantic_similarity = min_semantic_similarity
        #: The lexical fallback is a different scale from cosine similarity, so it
        #: gets its own floor instead of silently reusing the semantic one. It used
        #: to be an inline ``0.000001``, i.e. no floor at all: any shared token
        #: admitted a memory into the prompt.
        self.min_lexical_similarity = min_lexical_similarity

    async def retrieve(self, query: MemoryQuery) -> list[MemorySelection]:
        candidates = await self.repository.candidates(query, ceiling=self.candidate_ceiling)
        candidates = [record for record in candidates if query.allows_record(record)]
        query_vector: list[float] | None = None
        document_vectors: list[list[float]] | None = None
        if self.embedding and candidates:
            query_vector = await self.embedding.embed_query(query.text)
            document_vectors = await self.embedding.embed_documents(
                [record.content for record in candidates]
            )
            if len(document_vectors) != len(candidates):
                raise ValueError("embedding provider returned an invalid vector count")
        ranked: list[MemorySelection] = []
        for index, record in enumerate(candidates):
            if (
                record.tenant_id != query.tenant_id
                or not query.allows_scope(record.scope)
                or not record.visible_at(query.at)
                or record.taint_labels
            ):
                continue
            if _carries_injection(record.content):
                # Hard block at the model boundary: a stored memory that trips the
                # read-side injection scan must never enter a prompt as trusted.
                logger.warning(
                    "memory record blocked at read boundary by injection tripwire; memory_id=%s",
                    record.memory_id,
                )
                continue
            semantic = (
                _cosine(query_vector, document_vectors[index])
                if query_vector is not None and document_vectors is not None
                else _lexical_similarity(query.text, record.content)
            )
            # Metadata quality must never manufacture query relevance. The floor
            # is what enforces it: relevance is the admission test, and after
            # admission metadata is capped to a tie-breaker of its own scale.
            floor = self.min_semantic_similarity if self.embedding else self.min_lexical_similarity
            if semantic < floor:
                continue
            age_days = max((query.at - record.updated_at).total_seconds(), 0) / 86400
            recency = math.exp(-age_days / 180)
            provenance = 1.0 if record.evidence_refs or record.consent_ref else 0.0
            metadata = metadata_quality(
                recency=recency,
                confidence=record.confidence,
                importance=record.importance,
                provenance=provenance,
            )
            score = memory_relevance_score(semantic, metadata)
            ranked.append(
                MemorySelection(
                    memory=record,
                    score=max(0.0, min(1.0, score)),
                    score_breakdown={
                        "semantic": semantic,
                        "recency": recency,
                        "confidence": record.confidence,
                        "importance": record.importance,
                        "provenance": provenance,
                        # The tie-breaker and its weight are published so a reader
                        # can tell how much of a score came from relevance alone.
                        "metadata": metadata,
                        "metadata_weight": METADATA_WEIGHT,
                    },
                )
            )
        # UUID4 is a storage identity, not a ranking signal. Using it here made
        # exact-score ties change order when the same corpus was rebuilt. The
        # content digest gives a stable first tie-breaker; the deterministic
        # idempotency key completes the ordering when different subjects/scopes
        # intentionally carry identical content.
        ranked.sort(
            key=lambda item: (
                -item.score,
                item.memory.content_hash,
                item.memory.idempotency_key,
            )
        )
        # Embedding is an external await: a source may be revoked while it runs.
        visible = await self.repository.revalidate(
            query, [item.memory.memory_id for item in ranked]
        )
        selected: list[MemorySelection] = []
        subjects: set[str] = set()
        for item in ranked:
            if item.memory.memory_id not in visible:
                continue
            subject = str(item.memory.lineage_id)
            if subject in subjects:
                continue
            subjects.add(subject)
            selected.append(item)
            if len(selected) >= query.limit:
                break
        return selected


class PostRunMemoryMiddleware:
    """The only run-lifecycle entry point into long-term memory writes."""

    def __init__(
        self,
        writer_factory: Callable[[UUID], MemoryWriter],
        extractor: Callable[[dict[str, Any]], Awaitable[list[MemoryCandidate]]],
    ) -> None:
        self.writer_factory = writer_factory
        self.extractor = extractor

    async def process(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        status: str,
        result: dict[str, Any],
    ) -> list[MemoryRecord]:
        if status != "succeeded" or not result.get("final_state_verified", False):
            return []
        writer = self.writer_factory(tenant_id)
        stored: list[MemoryRecord] = []
        for candidate in await self.extractor(result):
            if candidate.tenant_id != tenant_id or candidate.source_run_id != run_id:
                raise PermissionError("post-run memory candidate escaped its run or tenant")
            record = await writer.write(candidate)
            if record is not None:
                stored.append(record)
        return stored
