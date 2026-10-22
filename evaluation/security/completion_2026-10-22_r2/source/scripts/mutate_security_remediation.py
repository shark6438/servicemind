"""Remove the repaired boundaries and require their regression tests to fail."""

from pathlib import Path

from mutation_harness import Mutation, run_mutations

ROOT = Path(__file__).resolve().parents[1]
TESTS = "tests/servicemind/test_security_remediation.py"

MUTATIONS = [
    (
        "M01 cross-tenant run access is accepted",
        "src/servicemind/security/run_access.py",
        "if run.tenant_id != context.tenant_id:",
        "if False:",
        "test_run_read_scope_rejects_cross_subject_and_revoked_grants",
    ),
    (
        "M02 cross-subject run access is accepted",
        "src/servicemind/security/run_access.py",
        'or (run.user_id != context.user_id and "approver" not in context.roles)',
        "or False",
        "test_run_read_scope_rejects_cross_subject_and_revoked_grants",
    ),
    (
        "M03 cancelled runs can be resurrected",
        "src/servicemind/persistence/repository.py",
        "if run.status == RunStatus.CANCELLED.value and status != RunStatus.CANCELLED:",
        "if False:",
        "test_real_database_cancel_is_a_barrier_and_cannot_be_resurrected",
    ),
    (
        "M04 cancelled runs can submit new writes",
        "src/servicemind/persistence/repository.py",
        "                RunStatus.CANCELLED.value,\n",
        "",
        "test_real_database_cancel_is_a_barrier_and_cannot_be_resurrected",
    ),
    (
        "M05 timestamp integer conversion is unbounded",
        "src/servicemind/harness/webhooks.py",
        "1 <= len(timestamp) <= 12",
        "1 <= len(timestamp) <= 6000",
        "test_timestamp_is_bounded_before_integer_conversion",
    ),
    (
        "M06 webhook body size is unbounded",
        "src/servicemind/api.py",
        "if len(chunks) + len(chunk) > 65_536:",
        "if False:",
        "test_webhook_limits_apply_before_database_or_crypto",
    ),
    (
        "M07 cancel response loses server-generated timestamp",
        "src/servicemind/persistence/repository.py",
        "                await session.refresh(run)\n                return run\n\n    @asynccontextmanager\n    async def run_submission",
        "                return run\n\n    @asynccontextmanager\n    async def run_submission",
        "test_real_database_cancel_http_materializes_updated_at",
    ),
]

if __name__ == "__main__":
    raise SystemExit(
        run_mutations(
            root=ROOT,
            experiment="security_remediation",
            mutations=[
                Mutation(name, ROOT / path, old, new, (f"{TESTS}::{test}",))
                for name, path, old, new, test in MUTATIONS
            ],
            pytest_args=("--run-docker",),
        )
    )
