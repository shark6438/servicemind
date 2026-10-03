"""Token-level late interaction (MaxSim) as a stand-alone retrieval signal.

Why this lives outside the OpenSearch index stack
-------------------------------------------------
The three production arms (dense / BM25 / hybrid) are all expressible as
``knn_vector`` or ``multi_match`` queries and are fused cluster-side by the
``servicemind-rag-rrf-v1`` pipeline. Late interaction is neither: it keeps one
vector per *token* and scores a document by :func:`maxsim`, which no OpenSearch
mapping can evaluate. So the signal gets its own index on disk and its own
scoring path, and it reaches the platform only through an explicit union hook.
It must never be written to a ``sm-knowledge-*`` alias.

Why the vectors come from the model we already pin
--------------------------------------------------
``BAAI/bge-m3`` ships ``colbert_linear.pt`` -- the same checkpoint that produces
the production dense vectors also produces the token-level ones. Reusing it
means the late-interaction arm introduces no new model download, no new
licence, and no ``trust_remote_code``; the plan's "pin revision, licence and
file hashes" rule is satisfied by construction. Projecting to ColBERTv2's 128
dimensions would be a *different* model and would break that property.

This module is deliberately numpy-only at import time so that both the repo
virtualenv (no CUDA: torch 2.13.0+cu130 against a 12.2 driver) and the
GPU-capable interpreter that does the encoding can import it through
``PYTHONPATH=src``. Torch and transformers are imported lazily, inside the
methods that need them.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("servicemind.rag.late_interaction")

#: Bumping this invalidates every index built by an older revision: the on-disk
#: layout and the scoring rule are part of the index's identity, not just the
#: model that produced the vectors.
LI_SCHEMA_VERSION = "v1"

#: ColBERT's own conventions. Storage and scan cost are linear in document
#: tokens, so an uncapped collection is a latency decision made by accident.
#: These caps are frozen hyperparameters and enter the generation fingerprint.
DEFAULT_MAX_DOC_TOKENS = 512
DEFAULT_MAX_QUERY_TOKENS = 32

#: Rows scored per block during a scan. Fixed on purpose: the accumulation order
#: is part of the determinism contract, not a tuning knob.
DEFAULT_BLOCK_ROWS = 4096

HEADER_NAME = "header.json"
VECTORS_NAME = "vectors.f16.npy"
OFFSETS_NAME = "offsets.npy"
KEYS_NAME = "keys.json"


def generation_fingerprint(
    *,
    model_name: str,
    model_revision: str,
    dimension: int,
    max_doc_tokens: int = DEFAULT_MAX_DOC_TOKENS,
    schema_version: str = LI_SCHEMA_VERSION,
) -> str:
    """Identify the vector space an index holds.

    Mirrors ``OpenSearchKnowledgeIndex.generation_fingerprint``: two indices may
    share a name only if every input that defines their vectors agrees. The
    truncation cap is included because it changes the stored vectors.
    """
    material = "|".join(
        [
            model_name,
            model_revision,
            str(dimension),
            str(max_doc_tokens),
            schema_version,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def maxsim(query: np.ndarray, document: np.ndarray) -> float:
    """Score one document against one query.

    ``sum_i max_j (q_i . d_j)`` over L2-normalised query tokens. Rows of both
    inputs must already be normalised; normalising here would hide the encoder
    forgetting to.
    """
    if query.size == 0 or document.size == 0:
        return 0.0
    sims = np.asarray(document, dtype=np.float32) @ np.asarray(query, dtype=np.float32).T
    return float(sims.max(axis=1).sum())


def maxsim_block(
    query: np.ndarray,
    flat: np.ndarray,
    offsets: np.ndarray,
    block_start: int,
    block_stop: int,
    *,
    dtype: Any = np.float32,
) -> np.ndarray:
    """Score documents ``[block_start, block_stop)`` against ``query``.

    ``flat`` holds every token vector of the index end to end and ``offsets``
    holds ``documents + 1`` cumulative token counts, so a block's tokens are one
    contiguous slice. Scoring a block at a time is what keeps peak memory
    bounded regardless of index size.

    Accumulation is float32 and the block boundaries are caller-supplied, so the
    result does not depend on how the scan was chunked -- the determinism
    requirement in the plan is a property of this function.
    """
    query = np.asarray(query, dtype=dtype)
    if block_stop <= block_start:
        return np.zeros(0, dtype=np.float32)
    start = int(offsets[block_start])
    stop = int(offsets[block_stop])
    block = np.asarray(flat[start:stop], dtype=dtype)
    local_offsets = np.asarray(offsets[block_start:block_stop], dtype=np.int64) - start
    lengths = np.diff(np.asarray(offsets[block_start : block_stop + 1], dtype=np.int64))
    if np.any(lengths <= 0):
        raise ValueError("every indexed document must contribute at least one token")
    sims = block @ query.T  # (tokens, query_tokens)
    segment_max = np.maximum.reduceat(sims, local_offsets)  # (documents, query_tokens)
    return np.asarray(segment_max.sum(axis=1), dtype=np.float32)


def search(
    query: np.ndarray,
    flat: np.ndarray,
    offsets: np.ndarray,
    keys: Sequence[str],
    *,
    top_k: int,
    block_rows: int = DEFAULT_BLOCK_ROWS,
) -> list[tuple[str, float]]:
    """Return the ``top_k`` ``(key, score)`` pairs, best first.

    Ties break on the key, ascending. Without an explicit tie-break the order of
    equally scored documents would depend on the scan implementation, and the
    plan requires a repeated identical query to reproduce the candidate set
    *and its order*.
    """
    if top_k <= 0:
        return []
    documents = len(keys)
    scores = np.empty(documents, dtype=np.float32)
    for start in range(0, documents, block_rows):
        stop = min(start + block_rows, documents)
        scores[start:stop] = maxsim_block(query, flat, offsets, start, stop)
    # Deterministic total order: score descending, then key ascending.
    order = sorted(range(documents), key=lambda index: (-float(scores[index]), keys[index]))
    return [(keys[index], float(scores[index])) for index in order[:top_k]]


def union_pool(first: Iterable[str], second: Iterable[str]) -> list[str]:
    """Ordered union of two candidate pools, each key appearing once.

    The admission question is whether late interaction finds gold the existing
    arms miss, so the measured pool has to be the *union*; taking the larger of
    the two coverage rates would answer a different question.
    """
    seen: dict[str, None] = {}
    for key in first:
        seen.setdefault(key, None)
    for key in second:
        seen.setdefault(key, None)
    return list(seen)


def contains_gold(pool: Iterable[str], gold: Iterable[str]) -> bool:
    """Whether any gold key is in the pool, at whatever depth the pool was cut."""
    return not set(gold).isdisjoint(pool)


def coverage_rate(rows: Iterable[tuple[Iterable[str], Iterable[str]]]) -> float:
    """Share of (pool, gold) rows whose pool contains a gold key."""
    pairs = list(rows)
    if not pairs:
        return 0.0
    return sum(1 for pool, gold in pairs if contains_gold(pool, gold)) / len(pairs)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class IndexHeader:
    schema_version: str
    generation_fingerprint: str
    model_name: str
    model_revision: str
    dimension: int
    max_doc_tokens: int
    documents: int
    total_tokens: int
    dtype: str
    keys_sha256: str
    vectors_sha256: str
    offsets_sha256: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> IndexHeader:
        return cls(**{field: payload[field] for field in cls.__slots__})

    def to_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in self.__slots__}


class LateInteractionIndex:
    """Read-side access to an on-disk late-interaction index.

    The vectors are memory-mapped: opening an index must not depend on its size,
    and the operating system's page cache is free to hold whatever fits without
    this process reserving it.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        header_path = self.directory / HEADER_NAME
        if not header_path.is_file():
            raise FileNotFoundError(f"{header_path} is missing; the index is not built")
        self.header = IndexHeader.from_dict(json.loads(header_path.read_text(encoding="utf-8")))
        self.keys: list[str] = json.loads((self.directory / KEYS_NAME).read_text(encoding="utf-8"))
        self.offsets = np.load(self.directory / OFFSETS_NAME)
        self.vectors = np.load(self.directory / VECTORS_NAME, mmap_mode="r")
        if len(self.keys) != self.header.documents:
            raise ValueError(
                f"index holds {len(self.keys)} keys but its header declares "
                f"{self.header.documents} documents"
            )
        if len(self.offsets) != self.header.documents + 1:
            raise ValueError("offsets do not describe exactly one segment per document")

    @property
    def size_bytes(self) -> int:
        return sum(
            (self.directory / name).stat().st_size
            for name in (HEADER_NAME, VECTORS_NAME, OFFSETS_NAME, KEYS_NAME)
        )

    def verify_digests(self) -> list[str]:
        """Re-hash the payload and return the names of any files that moved."""
        problems = [
            name
            for name, expected in (
                (KEYS_NAME, self.header.keys_sha256),
                (VECTORS_NAME, self.header.vectors_sha256),
                (OFFSETS_NAME, self.header.offsets_sha256),
            )
            if _sha256(self.directory / name) != expected
        ]
        return problems

    def search(self, query: np.ndarray, *, top_k: int) -> list[tuple[str, float]]:
        return search(query, self.vectors, self.offsets, self.keys, top_k=top_k)


def write_index(
    directory: Path,
    *,
    keys: Sequence[str],
    vectors: np.ndarray,
    offsets: np.ndarray,
    model_name: str,
    model_revision: str,
    dimension: int,
    max_doc_tokens: int,
) -> IndexHeader:
    """Write the four index files and return the header that describes them."""
    directory = Path(directory)
    if len(offsets) != len(keys) + 1:
        raise ValueError("offsets must carry one cumulative boundary per document, plus one")
    if int(offsets[-1]) != len(vectors):
        raise ValueError("offsets must account for every stored token vector")
    stored = np.asarray(vectors, dtype=np.float16)
    directory.mkdir(parents=True, exist_ok=True)
    np.save(directory / VECTORS_NAME, stored)
    np.save(directory / OFFSETS_NAME, np.asarray(offsets, dtype=np.int64))
    (directory / KEYS_NAME).write_text(json.dumps(list(keys), ensure_ascii=False), encoding="utf-8")
    header = IndexHeader(
        schema_version=LI_SCHEMA_VERSION,
        generation_fingerprint=generation_fingerprint(
            model_name=model_name,
            model_revision=model_revision,
            dimension=dimension,
            max_doc_tokens=max_doc_tokens,
        ),
        model_name=model_name,
        model_revision=model_revision,
        dimension=dimension,
        max_doc_tokens=max_doc_tokens,
        documents=len(keys),
        total_tokens=int(len(stored)),
        dtype="float16",
        keys_sha256=_sha256(directory / KEYS_NAME),
        vectors_sha256=_sha256(directory / VECTORS_NAME),
        offsets_sha256=_sha256(directory / OFFSETS_NAME),
    )
    (directory / HEADER_NAME).write_text(
        json.dumps(header.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return header
