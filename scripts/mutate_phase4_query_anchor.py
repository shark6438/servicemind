"""Revert each decision the query anchor rests on, and require the tests to notice.

``QueryProcessor`` used to hand the model's normalization back as ``normalized_query``
-- the one text the dense channel embeds -- while ``raw_query`` was read by no search
path. Every successful model call therefore replaced the question the user typed with a
paraphrase, and the funnel could only lose retrieval.

The fix has two halves, and both need pinning. The processor records the model's output
*beside* the question (``model_normalized_query`` + ``rewritten_queries``, verbatim), and
the search boundary spends the lexical budget on ``lexical_variants()``. Splitting it that
way is what keeps a capture of the model replayable under any arm; a mutation that moves
the truncation back into the processor, or that reads the rewrites raw at the fan-out,
destroys exactly that property without changing any single arm's output -- so each
decision below names the test that must go red if it is reverted.
"""

import pathlib
from pathlib import Path

from mutation_harness import Mutation, run_mutations

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXPERIMENT = "phase4_query_anchor"
QUERY = ROOT / "src/servicemind/rag/query.py"
KNOWLEDGE = ROOT / "src/servicemind/domain/knowledge.py"
OPENSEARCH = ROOT / "src/servicemind/rag/opensearch.py"
TESTS = "tests/servicemind/test_phase4_rag.py"


def _test(name: str) -> str:
    return f"{TESTS}::{name}"


MODEL_RETURN = """                    return KnowledgeQuery(
                        raw_query=query,
                        normalized_query=normalized,
                        model_normalized_query=proposal.normalized_query,
                        rewritten_queries=list(proposal.rewritten_queries),"""

FALLBACK_RETURN = """        return KnowledgeQuery(
            raw_query=query,
            normalized_query=normalized,
            identifiers=identifiers,"""

GUARD = """        if self.model_normalized_query is not None and (
            self.model_normalized_query.casefold() != self.normalized_query.casefold()
        ):
            variants.append(self.model_normalized_query)
        variants.extend(self.rewritten_queries)"""

ANCHOR_TEST = _test("test_a_model_paraphrase_never_displaces_the_users_own_words")
FALLBACK_TEST = _test("test_the_fallback_says_no_model_ran_rather_than_echoing_the_question")
ECHO_TEST = _test("test_a_model_that_echoes_the_question_adds_no_duplicate_arm")
BUDGET_TEST = _test("test_the_lexical_budget_spends_the_platform_cap_at_the_fan_out")
FUNNEL_TEST = _test("test_the_hybrid_request_anchors_on_the_question_and_arms_the_paraphrase")

MUTATIONS = [
    # ---- the processor stops anchoring -------------------------------------------------
    (
        "M01 the proposal replaces the searched text again, which is the original defect",
        QUERY,
        MODEL_RETURN,
        """                    return KnowledgeQuery(
                        raw_query=query,
                        normalized_query=proposal.normalized_query,
                        model_normalized_query=proposal.normalized_query,
                        rewritten_queries=list(proposal.rewritten_queries),""",
        ANCHOR_TEST,
    ),
    (
        "M02 the model's normalization is discarded, so the fix degrades to 'ignore the "
        "model' instead of 'add the model'",
        QUERY,
        MODEL_RETURN,
        """                    return KnowledgeQuery(
                        raw_query=query,
                        normalized_query=normalized,
                        model_normalized_query=None,
                        rewritten_queries=list(proposal.rewritten_queries),""",
        ANCHOR_TEST,
    ),
    (
        "M03 the model's paraphrases are discarded, so only the normalization reaches the "
        "funnel and the arms that keep rewrites measure nothing",
        QUERY,
        MODEL_RETURN,
        """                    return KnowledgeQuery(
                        raw_query=query,
                        normalized_query=normalized,
                        model_normalized_query=proposal.normalized_query,
                        rewritten_queries=[],""",
        ANCHOR_TEST,
    ),
    (
        "M04 the model path records the paraphrase as the raw query, so a caller reading "
        "raw_query back to report 'what the user asked' reports the model's words instead",
        QUERY,
        MODEL_RETURN,
        """                    return KnowledgeQuery(
                        raw_query=proposal.normalized_query,
                        normalized_query=normalized,
                        model_normalized_query=proposal.normalized_query,
                        rewritten_queries=list(proposal.rewritten_queries),""",
        ANCHOR_TEST,
    ),
    (
        "M05 the fallback fills the model's field with the question, so 'no model ran' "
        "becomes indistinguishable from 'the model echoed the question'",
        QUERY,
        FALLBACK_RETURN,
        """        return KnowledgeQuery(
            raw_query=query,
            normalized_query=normalized,
            model_normalized_query=normalized,
            identifiers=identifiers,""",
        FALLBACK_TEST,
    ),
    # ---- the fan-out stops spending the budget -----------------------------------------
    (
        "M06 the echo guard goes always-on, so a normalization identical to the question "
        "is offered as a variant and dedup becomes the only thing saving the arm",
        KNOWLEDGE,
        GUARD,
        """        if self.model_normalized_query is not None:
            variants.append(self.model_normalized_query)
        variants.extend(self.rewritten_queries)""",
        ECHO_TEST,
    ),
    (
        "M07 the normalization is never offered as a variant, so the model's best single "
        "answer is dropped from the funnel",
        KNOWLEDGE,
        GUARD,
        """        if False:
            variants.append(self.model_normalized_query)
        variants.extend(self.rewritten_queries)""",
        FUNNEL_TEST,
    ),
    (
        "M08 the variants are ordered rewrites-first, so the normalization is what the "
        "budget truncates away",
        KNOWLEDGE,
        GUARD,
        """        if False:
            variants.append(self.model_normalized_query)
        variants.extend(self.rewritten_queries)
        if self.model_normalized_query is not None and (
            self.model_normalized_query.casefold() != self.normalized_query.casefold()
        ):
            variants.insert(0, variants.pop())""",
        BUDGET_TEST,
    ),
    (
        "M09 the fan-out reads rewritten_queries raw, so the model's normalization is "
        "never searched even though the processor kept it",
        OPENSEARCH,
        "                query.normalized_query, query.lexical_variants(), use_rewrites=use_rewrites",
        "                query.normalized_query, query.rewritten_queries, use_rewrites=use_rewrites",
        FUNNEL_TEST,
    ),
    (
        "M10 the fan-out anchors on a variant rather than the normalized text, which is "
        "the same defect one layer down",
        OPENSEARCH,
        "    texts = [normalized]\n",
        "    texts = [rewrites[0]]\n",
        FUNNEL_TEST,
    ),
    (
        "M11 the hybrid cap widens past what the cluster accepts, so the request fails "
        "rather than the arm measuring anything",
        OPENSEARCH,
        "    return texts[: HYBRID_MAX_SUBQUERIES - 1]",
        "    return texts[: HYBRID_MAX_SUBQUERIES]",
        BUDGET_TEST,
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
