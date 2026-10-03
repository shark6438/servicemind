"""Revert each decision the per-source ceiling rests on, and require the tests to notice.

The ceiling was applied unconditionally by ``hit.source``, so a tenant whose documents all
carry one source -- which is what every ingester in ``rag/sources.py`` produces when it is
the tenant's only connector -- was capped at ``SERVICEMIND_RAG_MAX_PARENTS_PER_SOURCE``
parents however much of the token budget was left over. The guard's own comment says what
it is for ("no single source may drown every other source"), and with one source there is
no other source to drown, so the rule now lives in ``source_ceiling_applies`` and is applied
only when the candidate pool names more than one.

Two behaviours have to hold at once, and each has a wrong version that still packs
*something*: enforcing the bound always (the old behaviour) and enforcing it never (no bound
at all). The tests are written as a pair over the same inputs -- same ceilings, same scores,
only the number of sources differs -- so a mutant that flattens the pair to one behaviour
leaves exactly one of them red.

The exemption must also not have loosened the guard it sits next to: the per-document
ceiling is what the anti-crowding half of that comment asks for, and two mutations here
revert *it* to prove the fix did not quietly take over its job.
"""

import pathlib
from pathlib import Path

from mutation_harness import Mutation, run_mutations

ROOT = pathlib.Path(__file__).resolve().parent.parent
#: Stable across runs: an interrupted run journals under this name, and a rename would
#: orphan the journal at a path the next run never looks at.
EXPERIMENT = "phase4_source_ceiling"
SERVICE = ROOT / "src/servicemind/rag/service.py"
TEST = "tests/servicemind/test_phase4_rag.py"

SINGLE_SOURCE = f"{TEST}::test_the_per_source_ceiling_does_not_cap_a_single_source_tenant"
COMPETING = f"{TEST}::test_context_packer_enforces_per_source_ceiling_between_competing_sources"
PER_DOCUMENT = f"{TEST}::test_context_packer_enforces_per_document_ceiling"

APPLY_CALL = (
    "        enforce_source_ceiling = source_ceiling_applies(len({hit.source for hit in ranked}))"
)
PREDICATE = "    return distinct_sources > 1"

MUTATIONS = [
    # ---- the rule the packer consults --------------------------------------------------
    (
        "M01 the ceiling is enforced unconditionally, which is the behaviour being fixed",
        SERVICE,
        "            if enforce_source_ceiling and per_source[hit.source] >= per_source_cap:",
        "            if per_source[hit.source] >= per_source_cap:",
        SINGLE_SOURCE,
    ),
    (
        "M02 the predicate admits a pool of one source, so the ceiling fires on a corpus "
        "with nothing to balance",
        SERVICE,
        PREDICATE,
        "    return distinct_sources > 0",
        SINGLE_SOURCE,
    ),
    (
        "M03 the predicate is inverted, so only a pool of one source is ever capped",
        SERVICE,
        PREDICATE,
        "    return distinct_sources < 1",
        COMPETING,
    ),
    # ---- what the packer feeds the rule ------------------------------------------------
    (
        "M04 the ceiling is never enforced, leaving one source free to take the whole prompt",
        SERVICE,
        APPLY_CALL,
        "        enforce_source_ceiling = False",
        COMPETING,
    ),
    (
        "M05 the pool is counted over documents instead of sources, so many documents from "
        "one source read as competition",
        SERVICE,
        APPLY_CALL,
        "        enforce_source_ceiling = source_ceiling_applies("
        "len({hit.document_id for hit in ranked}))",
        SINGLE_SOURCE,
    ),
    # ---- the ceiling the exemption must not have displaced ------------------------------
    (
        "M06 the per-document ceiling is dropped, so one document may crowd the context",
        SERVICE,
        "            if per_document[hit.document_id] >= per_document_cap:",
        "            if False:",
        PER_DOCUMENT,
    ),
    (
        "M07 the per-document ceiling goes off by one, admitting the parent it exists to stop",
        SERVICE,
        "            if per_document[hit.document_id] >= per_document_cap:",
        "            if per_document[hit.document_id] > per_document_cap:",
        PER_DOCUMENT,
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
