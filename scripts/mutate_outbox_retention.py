"""Revert each part of the outbox's bound, and require the tests to notice.

The defect this locks is a queue that never empties. `tool_outbox` guaranteed that an
event raised in a transaction would be published, retried and dead-lettered -- everything
except stopping: published rows accumulated forever, and the Redis stream they were
published to had no `MAXLEN` either, so the identical unboundedness existed on both sides
of the publish. Nothing in a suite that runs for a minute can see either one, which is how
both survived a full acceptance pass.

Every mutation below removes one of the things that make the bound real: the stream's
retention, the age predicate, the status predicate, the clock the age is measured from,
the dead-letter exemption, the ceiling on one sweep, and the sweep's existence in the
loop at all. Each must be caught.
"""

import pathlib
from pathlib import Path

from mutation_harness import Mutation, run_mutations

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXPERIMENT = "outbox_retention"
RELIABILITY = ROOT / "src/servicemind/reliability/outbox.py"
WORKER = ROOT / "src/servicemind/reliability/worker.py"
PERSISTENCE = ROOT / "src/servicemind/persistence/outbox.py"

RETENTION_TEST = "tests/servicemind/test_phase7_outbox_retention.py"
PLATFORM_TEST = "tests/servicemind/test_phase6_tool_platform.py"

#: The docker-gated assertions live in the same file; the harness runs the whole file so
#: the `--run-docker` pair is included rather than skipped.
PRUNE_KEEPS_ONLY_AGED_ROWS = (
    f"{RETENTION_TEST}::test_the_sweep_retires_delivered_rows_and_keeps_everything_else"
)


def _test(name: str) -> str:
    return f"{RETENTION_TEST}::{name}"


MUTATIONS = [
    (
        "M01 the stream is published to without a bound",
        RELIABILITY,
        "            maxlen=STREAM_MAXLEN,\n            approximate=True,\n",
        "",
        f"{PLATFORM_TEST}::test_redis_outbox_message_contains_references_only",
    ),
    (
        "M02 the stream bound is exact, so every publish pays to find the cut",
        RELIABILITY,
        "            approximate=True,\n",
        "            approximate=False,\n",
        f"{PLATFORM_TEST}::test_redis_outbox_message_contains_references_only",
    ),
    (
        "M03 retention forgets which rows it has delivered",
        PERSISTENCE,
        '                            ToolOutboxRecord.status == "published",\n',
        "",
        PRUNE_KEEPS_ONLY_AGED_ROWS,
    ),
    (
        "M04 retention forgets to wait for the delivery record to age",
        PERSISTENCE,
        "                            ToolOutboxRecord.updated_at < cutoff,\n",
        "",
        PRUNE_KEEPS_ONLY_AGED_ROWS,
    ),
    (
        "M05 retention measures the wait, not the delivery",
        PERSISTENCE,
        "                            ToolOutboxRecord.updated_at < cutoff,\n",
        "                            ToolOutboxRecord.created_at < cutoff,\n",
        PRUNE_KEEPS_ONLY_AGED_ROWS,
    ),
    (
        "M06 retention also retires the rows that were never delivered",
        PERSISTENCE,
        '                            ToolOutboxRecord.status == "published",\n',
        '                            ToolOutboxRecord.status.in_(("published", "dead")),\n',
        PRUNE_KEEPS_ONLY_AGED_ROWS,
    ),
    (
        "M07 one sweep takes a single page however much has aged out",
        WORKER,
        "                if pruned < PRUNE_BATCH_LIMIT:\n                    break\n",
        "                break\n",
        _test("test_the_sweep_repeats_until_a_batch_comes_back_short"),
    ),
    (
        "M08 one sweep has no ceiling, so it can outlast the publishing it interrupts",
        WORKER,
        "            for _ in range(PRUNE_BATCHES_PER_SWEEP):\n",
        "            for _ in range(10**9):\n",
        _test("test_the_sweep_gives_up_between_batches_rather_than_inside_one"),
    ),
    (
        "M09 one tenant's retention failure abandons the others",
        WORKER,
        '            logger.exception("Outbox retention failed for tenant_id=%s", tenant_id)\n',
        '            logger.exception("Outbox retention failed for tenant_id=%s", tenant_id)\n'
        "            raise\n",
        _test("test_one_tenants_retention_failure_does_not_stop_the_others"),
    ),
    (
        "M10 shutting the worker down is treated as a retention failure to stop on",
        WORKER,
        "        except asyncio.CancelledError:\n            raise\n",
        "        except asyncio.CancelledError:\n            return\n",
        _test("test_a_sweep_that_is_cancelled_propagates_the_cancellation"),
    ),
    (
        "M11 the sweep is never reached from the delivery loop",
        WORKER,
        "                await _prune_expired(relay, tenant_ids)\n",
        "",
        _test("test_the_sweep_does_not_run_on_every_delivery"),
    ),
    (
        "M12 the sweep is un-scheduled and would run on every delivery",
        WORKER,
        "            if loop.time() >= next_prune:\n",
        "            if True:\n",
        _test("test_the_sweep_does_not_run_on_every_delivery"),
    ),
]


if __name__ == "__main__":
    raise SystemExit(
        run_mutations(
            root=ROOT,
            experiment=EXPERIMENT,
            mutations=[
                Mutation(name=name, path=Path(path), old=old, new=new, tests=(tests,))
                for name, path, old, new, tests in MUTATIONS
            ],
            pytest_args=("--run-docker",),
        )
    )
