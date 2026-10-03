"""Contract tests for the multi-query A/B script's two seams.

``docs/PHASE4_ENTERPRISE_ACCEPTANCE.md`` cites ``phase4_multiquery_latest.{json,md}``
as the evidence that the fan-out wiring is connected. That script had no test, and
both of its seams had drifted from the pipeline they stand in for:

* its replay did not accept the ``model_query`` keyword that ``EnterpriseRAG.retrieve``
  always passes, so every arm raised ``TypeError`` before producing a row -- the
  committed report predates that call site and no longer reproduces;
* its deterministic reranker returned a raw token-overlap *count*, which the
  ``[0, 1]`` guard in ``EnterpriseRAG.retrieve`` rejects as soon as a candidate
  shares more than one term with the query.

Both are the same defect shape: a stand-in that no longer satisfies the contract of
the thing it replaces, which surfaces as a plausible report rather than a failure.
The tests below re-derive the contract from the production call site, so they fail
if either side drifts again in either direction.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

from servicemind.domain.knowledge import KnowledgeQuery, RetrievalIntent

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "compare_phase4_multiquery", ROOT / "scripts/compare_phase4_multiquery.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _query() -> KnowledgeQuery:
    return KnowledgeQuery(
        raw_query="how do I rebind my MFA device",
        normalized_query="rebind MFA device",
        rewritten_queries=["reset multi-factor authentication device"],
        intent=RetrievalIntent.PROCEDURE,
    )


def test_the_replay_accepts_every_keyword_production_calls_it_with() -> None:
    """``rag/service.py`` calls ``process(query, use_model=..., model_query=...)``.

    A stand-in that drops a keyword the real pipeline passes is not a stand-in: it
    fails on the first call, after the script has already announced the arm.
    """
    source = (ROOT / "src/servicemind/rag/service.py").read_text(encoding="utf-8")
    assert "model_query=model_query" in source, "the production call site moved; re-pin this test"

    signature = inspect.signature(MODULE.CannedQueryProcessor.process)
    assert {"query", "use_model", "model_query"} <= set(signature.parameters)
    # Both are optional at the call site, so a caller that omits them still works.
    for name in ("use_model", "model_query"):
        default = signature.parameters[name].default
        assert default is not inspect.Parameter.empty, f"{name} must have a default"
    assert signature.parameters["model_query"].default is None


@pytest.mark.asyncio
async def test_the_replay_answers_the_question_the_harness_asks_with() -> None:
    """The mapping is keyed by the question text, not by the gold query id."""
    question = "how do I rebind my MFA device"
    processor = MODULE.CannedQueryProcessor({question: _query()})
    processed = await processor.process(question, use_model=True, model_query='{"q": "x"}')
    assert processed.normalized_query == "rebind MFA device"


def test_the_deterministic_reranker_returns_a_normalized_score() -> None:
    """Two shared tokens used to yield ``2``, which ``EnterpriseRAG.retrieve`` rejects."""
    reranker = MODULE._reranker()
    scores = _scores(reranker, "rebind MFA device", "rebind the MFA device now")
    assert scores, "the stand-in reranker produced no score"
    assert all(0.0 <= score <= 1.0 for score in scores), scores


def _scores(reranker, query: str, text: str) -> list[float]:
    import asyncio

    return asyncio.run(reranker.score(query, [text]))


def test_the_pool_probe_runs_at_the_funnel_production_runs() -> None:
    """``index.search`` defaults to 60/60/40; the deployment runs 100/100/100.

    The candidate-pool column is the mechanism surface the keep/delete verdict is drawn
    from, so measuring it at a narrower funnel than production reports the breadth of a
    candidate set production never hands the reranker. Pinned textually against the
    settings names rather than their values, so a config change cannot silently
    separate the two.
    """
    source = (ROOT / "scripts/compare_phase4_multiquery.py").read_text(encoding="utf-8")
    for name in (
        "SERVICEMIND_RAG_DENSE_K",
        "SERVICEMIND_RAG_BM25_K",
        "SERVICEMIND_RAG_CANDIDATE_K",
    ):
        assert f"{name.lower().replace('servicemind_rag_', '')}=" in source and name in source, (
            f"the pool probe no longer sizes {name} from settings; re-pin this test"
        )
