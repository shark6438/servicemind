"""Revert each seam the multi-query A/B rests on, and require the tests to notice.

That script had no test at all, and both of its seams had drifted away from the
pipeline they stand in for while its committed report went on being cited as evidence:

* the replay stopped accepting ``model_query``, which production always passes, so
  every arm raised before it could measure anything;
* the deterministic reranker returned a token-overlap *count*, which the ``[0, 1]``
  guard in ``EnterpriseRAG.retrieve`` rejects on the first candidate that shares more
  than one term with the query.

Neither is visible in the committed JSON, which is exactly why the report outlived
both. The mutations below put each one back; the contract tests must go red.
"""

import pathlib
from pathlib import Path

from mutation_harness import Mutation, run_mutations

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXPERIMENT = "phase4_multiquery_script"
SCRIPT = ROOT / "scripts/compare_phase4_multiquery.py"
TEST = "tests/servicemind/test_phase4_multiquery_script.py"

SIGNATURE = f"{TEST}::test_the_replay_accepts_every_keyword_production_calls_it_with"
ASKED = f"{TEST}::test_the_replay_answers_the_question_the_harness_asks_with"
RANGE = f"{TEST}::test_the_deterministic_reranker_returns_a_normalized_score"
POOL = f"{TEST}::test_the_pool_probe_runs_at_the_funnel_production_runs"

MUTATIONS = [
    (
        "M01 the replay stops accepting the keyword production passes, so installing it "
        "makes every retrieve() raise before a single arm is measured",
        SCRIPT,
        "        use_model: bool = True,\n        model_query: str | None = None,\n    ) -> KnowledgeQuery:",
        "        use_model: bool = True,\n    ) -> KnowledgeQuery:",
        f"{SIGNATURE} {ASKED}",
    ),
    (
        "M02 the stand-in reranker returns a raw overlap count again, which the pipeline's "
        "range guard rejects on any candidate sharing more than one term",
        SCRIPT,
        "        lambda query, text: (\n"
        "            len(set(query.casefold().split()) & set(text.casefold().split()))\n"
        "            / max(\n"
        "                len(set(query.casefold().split()) | set(text.casefold().split())),\n"
        "                1,\n"
        "            )\n"
        "        )",
        "        lambda query, text: len(set(query.casefold().split()) & set(text.casefold().split()))",
        RANGE,
    ),
    (
        "M03 the pool probe falls back to the index method's own defaults, so the column "
        "reports the breadth of a funnel production never runs",
        SCRIPT,
        "                    candidate_k=settings.SERVICEMIND_RAG_CANDIDATE_K,\n",
        "",
        POOL,
    ),
]


if __name__ == "__main__":
    raise SystemExit(
        run_mutations(
            root=ROOT,
            experiment=EXPERIMENT,
            mutations=[
                Mutation(name=name, path=Path(path), old=old, new=new, tests=tuple(tests.split()))
                for name, path, old, new, tests in MUTATIONS
            ],
        )
    )
