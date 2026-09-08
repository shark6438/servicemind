from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import httpx
import numpy as np

logger = logging.getLogger("servicemind.rag.models")


def _local_snapshot(model_name: str, revision: str, cache_folder: str | None) -> str | None:
    """Resolve a pinned revision to its on-disk HF snapshot directory, if present.

    ``sentence_transformers.SentenceTransformer("BAAI/bge-m3", cache_folder=...)`` does
    not forward ``cache_folder`` into transformers' ``AutoConfig``; config resolution
    falls back to the default HF cache (``~/.cache/huggingface``) and, offline, can hit a
    partial/poisoned entry -- surfacing ``Unrecognized model ... config.json`` even though
    our ``cache_folder`` snapshot is complete. When a pinned snapshot exists under
    ``cache_folder`` we hand the *path* to sentence-transformers instead of the repo id, so
    model, tokenizer and config all load from local weights with no hub contact at all.
    Returns ``None`` when no matching snapshot directory exists (unpinned revisions,
    cache folder absent, or a snapshot still being fetched).
    """
    if not cache_folder or not revision or revision in {"", "main"}:
        return None
    snapshot = (
        Path(cache_folder) / f"models--{model_name.replace('/', '--')}" / "snapshots" / revision
    )
    if (snapshot / "config.json").is_file():
        return str(snapshot)
    return None


#: A new model revision means an incompatible vector space. Indices must never mix
#: vectors produced by two revisions, and production config MUST pin a concrete
#: commit hash (e.g. ``git ls-remote https://huggingface.co/BAAI/bge-m3 refs/heads/main``)
#: instead of floating "main".
_UNPINNED_REVISION_WARNING = (
    "Embedding/Reranker revision is '%s' (unpinned). Production RAG config must pin a "
    "concrete commit hash; floating 'main' silently changes the vector space on upstream "
    "releases and destroys Recall of previously indexed chunks."
)


class EmbeddingProvider(Protocol):
    dimension: int
    model_name: str
    model_revision: str

    async def embed_query(self, text: str) -> list[float]: ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class Reranker(Protocol):
    model_name: str
    model_revision: str

    async def score(self, query: str, documents: list[str]) -> list[float]: ...


class BgeM3EmbeddingProvider:
    dimension = 1024
    model_name = "BAAI/bge-m3"

    def __init__(
        self,
        *,
        model_revision: str = "main",
        device: str | None = None,
        cache_folder: str | None = None,
        local_files_only: bool = False,
    ) -> None:
        self.model_revision = model_revision
        self.device = device
        self.cache_folder = cache_folder
        self.local_files_only = local_files_only
        self._model: Any = None
        self._lock = asyncio.Lock()

    async def _load(self) -> Any:
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    if self.model_revision in {"", "main"}:
                        logger.warning(_UNPINNED_REVISION_WARNING, self.model_revision)
                    if self.cache_folder and not self.local_files_only:
                        logger.warning(
                            "In-process BGE provider given cache_folder=%s without "
                            "local_files_only; offline datacenters must pin it to avoid "
                            "hub HEAD requests.",
                            self.cache_folder,
                        )
                    from sentence_transformers import SentenceTransformer

                    # Prefer an absolute local snapshot path so offline datacenters never
                    # touch the hub during AutoConfig resolution (see _local_snapshot).
                    local_path = _local_snapshot(
                        self.model_name, self.model_revision, self.cache_folder
                    )
                    if local_path:
                        self._model = await asyncio.to_thread(
                            SentenceTransformer,
                            local_path,
                            device=self.device,
                            trust_remote_code=False,
                        )
                    else:
                        self._model = await asyncio.to_thread(
                            SentenceTransformer,
                            self.model_name,
                            revision=self.model_revision,
                            device=self.device,
                            cache_folder=self.cache_folder,
                            local_files_only=self.local_files_only,
                            trust_remote_code=False,
                        )
        return self._model

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = await self._load()
        values = await asyncio.to_thread(
            model.encode,
            texts,
            batch_size=16,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        array = np.asarray(values, dtype=np.float32)
        return array.tolist()

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]


class BgeM3Reranker:
    model_name = "BAAI/bge-reranker-v2-m3"

    def __init__(
        self,
        *,
        model_revision: str = "main",
        device: str | None = None,
        cache_folder: str | None = None,
        local_files_only: bool = False,
    ) -> None:
        self.model_revision = model_revision
        self.device = device
        self.cache_folder = cache_folder
        self.local_files_only = local_files_only
        self._model: Any = None
        self._lock = asyncio.Lock()

    async def _load(self) -> Any:
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    if self.model_revision in {"", "main"}:
                        logger.warning(_UNPINNED_REVISION_WARNING, self.model_revision)
                    from sentence_transformers import CrossEncoder

                    local_path = _local_snapshot(
                        self.model_name, self.model_revision, self.cache_folder
                    )
                    if local_path:
                        self._model = await asyncio.to_thread(
                            CrossEncoder,
                            local_path,
                            device=self.device,
                            trust_remote_code=False,
                        )
                    else:
                        self._model = await asyncio.to_thread(
                            CrossEncoder,
                            self.model_name,
                            revision=self.model_revision,
                            device=self.device,
                            cache_folder=self.cache_folder,
                            local_files_only=self.local_files_only,
                            trust_remote_code=False,
                        )
        return self._model

    async def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        model = await self._load()
        pairs = [(query, document) for document in documents]
        import torch

        values = await asyncio.to_thread(
            model.predict, pairs, batch_size=8, activation_fn=torch.nn.Sigmoid()
        )
        return np.asarray(values, dtype=np.float32).reshape(-1).tolist()


class TeiEmbeddingProvider:
    dimension = 1024
    model_name = "BAAI/bge-m3"

    def __init__(self, base_url: str, *, model_revision: str = "main") -> None:
        self.base_url = base_url.rstrip("/")
        self.model_revision = model_revision
        self._semaphore = asyncio.Semaphore(4)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        values: list[list[float]] = []
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            for start in range(0, len(texts), 8):
                async with self._semaphore:
                    response = await self._post_with_retry(
                        client,
                        "/embed",
                        {"inputs": texts[start : start + 8], "normalize": True},
                    )
                values.extend(response.json())
        return values

    async def _post_with_retry(
        self, client: httpx.AsyncClient, path: str, payload: dict
    ) -> httpx.Response:
        for attempt in range(6):
            response = await client.post(f"{self.base_url}{path}", json=payload)
            if response.status_code != 429:
                response.raise_for_status()
                return response
            await asyncio.sleep(0.25 * (2**attempt))
        response.raise_for_status()
        raise AssertionError("unreachable")

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]


class TeiReranker:
    model_name = "BAAI/bge-reranker-v2-m3"

    def __init__(self, base_url: str, *, model_revision: str = "main") -> None:
        self.base_url = base_url.rstrip("/")
        self.model_revision = model_revision
        self._semaphore = asyncio.Semaphore(2)

    async def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        scores = [0.0] * len(documents)
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            for start in range(0, len(documents), 4):
                async with self._semaphore:
                    response = await self._post_with_retry(
                        client,
                        {
                            "query": query,
                            "texts": documents[start : start + 4],
                            "return_text": False,
                        },
                    )
                batch = response.json()
                expected = len(documents[start : start + 4])
                if len(batch) != expected or {item["index"] for item in batch} != set(
                    range(expected)
                ):
                    raise ValueError("reranker returned missing, duplicate or invalid indices")
                for item in batch:
                    score = float(item["score"])
                    if not math.isfinite(score) or not 0 <= score <= 1:
                        raise ValueError("reranker must return normalized finite scores")
                    scores[start + int(item["index"])] = score
        return scores

    async def _post_with_retry(self, client: httpx.AsyncClient, payload: dict) -> httpx.Response:
        for attempt in range(6):
            response = await client.post(f"{self.base_url}/rerank", json=payload)
            if response.status_code != 429:
                response.raise_for_status()
                return response
            await asyncio.sleep(0.25 * (2**attempt))
        response.raise_for_status()
        raise AssertionError("unreachable")


class DeterministicEmbeddingProvider:
    """Stable test provider; application configuration never selects this backend."""

    dimension = 16
    model_name = "deterministic-test-embedding"
    model_revision = "v1"

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = np.zeros(self.dimension, dtype=np.float32)
            for token in text.casefold().split():
                bucket = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], "big")
                vector[bucket % self.dimension] += 1
            norm = np.linalg.norm(vector)
            vectors.append((vector / norm if norm else vector).tolist())
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]


class CallableReranker:
    model_name = "callable-test-reranker"
    model_revision = "v1"

    def __init__(self, function: Callable[[str, str], float]) -> None:
        self.function = function

    async def score(self, query: str, documents: list[str]) -> list[float]:
        return [self.function(query, document) for document in documents]
