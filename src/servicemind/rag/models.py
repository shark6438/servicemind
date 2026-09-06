from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from typing import Protocol

import httpx
import numpy as np


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
    ) -> None:
        self.model_revision = model_revision
        self.device = device
        self._model = None
        self._lock = asyncio.Lock()

    async def _load(self):
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    self._model = await asyncio.to_thread(
                        SentenceTransformer,
                        self.model_name,
                        revision=self.model_revision,
                        device=self.device,
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
    ) -> None:
        self.model_revision = model_revision
        self.device = device
        self._model = None
        self._lock = asyncio.Lock()

    async def _load(self):
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    from sentence_transformers import CrossEncoder

                    self._model = await asyncio.to_thread(
                        CrossEncoder,
                        self.model_name,
                        revision=self.model_revision,
                        device=self.device,
                        trust_remote_code=False,
                    )
        return self._model

    async def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        model = await self._load()
        pairs = [(query, document) for document in documents]
        values = await asyncio.to_thread(model.predict, pairs, batch_size=8)
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
                for item in response.json():
                    scores[start + int(item["index"])] = float(item["score"])
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
