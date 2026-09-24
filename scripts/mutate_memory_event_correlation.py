"""Revert each memory-event correlation decision in turn and require the ledger tests to notice.

The property under test is small and easy to half-implement: a ``memory_events`` row
should name the run that caused the transition, and should name *nothing* when no run
caused it. Both halves are load-bearing and they fail in opposite directions. Drop the
correlation and a revocation becomes unexplainable; invent one and the ledger states a
cause that never existed, which is worse than the gap it replaced, because a fabricated
correlation reads exactly like a real one.

Several mutations below therefore do not delete the run -- they *supply* one at a writer
that has none to supply. Those are the ones an implementation drifting toward
"everything has a run" would produce, and each must be caught by the test that asserts
``None``.
"""

import pathlib
from pathlib import Path

from mutation_harness import Mutation, run_mutations

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXPERIMENT = "memory_event_correlation"
CONTRACTS = ROOT / "src/servicemind/memory/contracts.py"
REPOSITORY = ROOT / "src/servicemind/memory/repository.py"

TEST = "tests/servicemind/test_memory_event_correlation.py"

MUTATIONS = [
    (
        "M01 the domain event stops carrying the run that caused the transition",
        CONTRACTS,
        '        "payload": dict(payload or {}),\n'
        '        "run_id": run_id,\n'
        '        "trace_id": trace_id,\n'
        "    }",
        '        "payload": dict(payload or {}),\n    }',
        f"{TEST}::test_the_ledger_and_the_double_record_the_same_fields",
    ),
    (
        "M02 a stored creation forgets which run authored it",
        REPOSITORY,
        "                        run_id=candidate.source_run_id,\n"
        "                        trace_id=candidate.source_trace_id,\n",
        "                        run_id=None,\n                        trace_id=None,\n",
        f"{TEST}::test_the_stored_rows_carry_the_run_and_nothing_where_there_was_none",
    ),
    (
        "M03 the in-memory creation event carries the run but loses the trace",
        REPOSITORY,
        "                run_id=candidate.source_run_id,\n"
        "                trace_id=candidate.source_trace_id,\n",
        "                run_id=candidate.source_run_id,\n                trace_id=None,\n",
        f"{TEST}::test_a_created_memory_records_the_run_that_authored_it",
    ),
    (
        "M03b the stored creation event carries the run but loses the trace",
        REPOSITORY,
        "                        run_id=candidate.source_run_id,\n"
        "                        trace_id=candidate.source_trace_id,\n",
        "                        run_id=candidate.source_run_id,\n"
        "                        trace_id=None,\n",
        f"{TEST}::test_the_stored_rows_carry_the_run_and_nothing_where_there_was_none",
    ),
    (
        "M04 a TTL lapse is blamed on whichever run happened to read first",
        REPOSITORY,
        '                        payload={"expired_at": now.isoformat()},\n'
        "                        # No ``run_id``: a lapse is the clock's doing. This runs at the top\n"
        "                        # of whichever read happens to be first after the deadline, so the\n"
        "                        # run that triggered it did not cause it, and attributing the\n"
        "                        # expiry to that run would make the ledger read as if a run had\n"
        "                        # decided to end the memory's life.\n",
        '                        payload={"expired_at": now.isoformat()},\n'
        "                        run_id=uuid4(),\n",
        f"{TEST}::test_the_stored_rows_carry_the_run_and_nothing_where_there_was_none",
    ),
    (
        "M05 the in-memory authority's expiry invents a run too",
        REPOSITORY,
        '                    payload={"expired_at": now.isoformat()},\n                )\n',
        '                    payload={"expired_at": now.isoformat()},\n'
        "                    run_id=uuid4(),\n"
        "                )\n",
        f"{TEST}::test_a_lapsed_ttl_records_an_expiry_with_no_run",
    ),
    (
        "M06 the procedure revalidator blames a run for support that lapsed",
        REPOSITORY,
        "                payload={\n"
        '                    "invalidated_at": now.isoformat(),\n'
        '                    "expected_support_count": len(set(current.supporting_episode_ids)),\n'
        "                },\n",
        "                payload={\n"
        '                    "invalidated_at": now.isoformat(),\n'
        '                    "expected_support_count": len(set(current.supporting_episode_ids)),\n'
        "                },\n"
        "                run_id=uuid4(),\n",
        f"{TEST}::test_a_procedure_that_lost_its_support_records_a_revocation_with_no_run",
    ),
    (
        "M07 the in-memory revocation drops the run that caused it",
        REPOSITORY,
        '                    payload={"evidence_id": evidence_id},\n'
        "                    run_id=run_id,\n"
        "                    trace_id=trace_id,\n",
        '                    payload={"evidence_id": evidence_id},\n'
        "                    run_id=None,\n"
        "                    trace_id=trace_id,\n",
        f"{TEST}::test_a_revocation_records_the_run_whose_evidence_caused_it",
    ),
    (
        "M08 the in-memory transition drops the run that drove it",
        REPOSITORY,
        '                    "reviewed_version": updated.version,\n'
        '                    "reviewed_content_hash": updated.content_hash,\n'
        "                },\n"
        "                run_id=run_id,\n"
        "                trace_id=trace_id,\n",
        '                    "reviewed_version": updated.version,\n'
        '                    "reviewed_content_hash": updated.content_hash,\n'
        "                },\n"
        "                run_id=None,\n"
        "                trace_id=trace_id,\n",
        f"{TEST}::test_a_run_caused_transition_records_that_run",
    ),
    (
        "M09 the stored transition drops the run that drove it",
        REPOSITORY,
        '                            "reviewed_version": row.version,\n'
        '                            "reviewed_content_hash": row.content_hash,\n'
        "                        },\n"
        "                        run_id=run_id,\n"
        "                        trace_id=trace_id,\n",
        '                            "reviewed_version": row.version,\n'
        '                            "reviewed_content_hash": row.content_hash,\n'
        "                        },\n"
        "                        run_id=None,\n"
        "                        trace_id=trace_id,\n",
        f"{TEST}::test_the_stored_rows_carry_the_run_and_nothing_where_there_was_none",
    ),
    (
        "M10 a human decision is given the run it never had",
        REPOSITORY,
        "                },\n"
        "                run_id=run_id,\n"
        "                trace_id=trace_id,\n"
        "            )\n"
        "            return updated",
        "                },\n"
        "                run_id=run_id or uuid4(),\n"
        "                trace_id=trace_id,\n"
        "            )\n"
        "            return updated",
        f"{TEST}::test_a_human_review_records_the_actor_it_used_to_discard",
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
            # Half of this property's teeth are in the docker-gated test that asserts what
            # PostgreSQL actually stored. Without this the run skips, exits 0, and every
            # stored-row mutation would be graded as undetected.
            pytest_args=("--run-docker",),
        )
    )
