"""Enforce Phase 6 integrity values at the PostgreSQL boundary."""

from alembic import op

revision = "0013_phase6_hash_guards"
down_revision = "0012_mcp_task_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_tool_policy_hashes",
        "tool_policy_decisions",
        "tool_checksum ~ '^[0-9a-f]{64}$' AND argument_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_tool_invocation_status",
        "governed_tool_invocations",
        "status IN ('succeeded','failed','denied','cancelled')",
    )
    op.create_check_constraint(
        "ck_tool_invocation_hashes",
        "governed_tool_invocations",
        "tool_checksum ~ '^[0-9a-f]{64}$' AND argument_hash ~ '^[0-9a-f]{64}$' "
        "AND (output_hash IS NULL OR output_hash ~ '^[0-9a-f]{64}$')",
    )
    op.create_check_constraint(
        "ck_mcp_task_hashes",
        "mcp_tasks",
        "argument_hash ~ '^[0-9a-f]{64}$' "
        "AND (output_hash IS NULL OR output_hash ~ '^[0-9a-f]{64}$')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_mcp_task_hashes", "mcp_tasks", type_="check")
    op.drop_constraint("ck_tool_invocation_hashes", "governed_tool_invocations", type_="check")
    op.drop_constraint("ck_tool_invocation_status", "governed_tool_invocations", type_="check")
    op.drop_constraint("ck_tool_policy_hashes", "tool_policy_decisions", type_="check")
