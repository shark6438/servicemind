"""Pure ranking metrics for the Phase 4 retrieval harness.

Everything here is a deterministic function over (ranked ids, relevant ids) so the
numbers are unit-testable without any search backend. Relevance is binary (a
returned item is either one of the gold ids or it is not).
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def _gains(ranked: Sequence[str], relevant: set[str]) -> list[int]:
    return [1 if item in relevant else 0 for item in ranked]


def precision_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of the top-k that is relevant; 0 when k is zero."""
    if k <= 0:
        return 0.0
    hits = sum(_gains(ranked[:k], relevant))
    return hits / k


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Fraction of gold items recovered in the top-k.

    Defined only over queries that have gold; the harness skips empty-relevant rows
    (those measure abstention, not recall).
    """
    if not relevant or k <= 0:
        return 0.0
    hits = sum(_gains(ranked[:k], relevant))
    return min(1.0, hits / len(relevant))


def mrr_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Reciprocal rank of the first gold item inside the top-k; 0 if absent."""
    for position, item in enumerate(ranked[:k], start=1):
        if item in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Binary-gain NDCG@k with log_2 discount; 0 for an empty gold set."""
    if not relevant or k <= 0:
        return 0.0
    gains = _gains(ranked[:k], relevant)
    dcg = sum(gain / math.log2(position + 1) for position, gain in enumerate(gains, start=1))
    ideal = sum(1.0 / math.log2(position + 1) for position in range(1, min(k, len(relevant)) + 1))
    if ideal <= 0:
        return 0.0
    return dcg / ideal


def dedupe_rate(ids: Sequence[str]) -> float:
    """Fraction of the candidate list that is a duplicate (context-packer remover)."""
    if not ids:
        return 0.0
    return 1.0 - (len(set(ids)) / len(ids))
