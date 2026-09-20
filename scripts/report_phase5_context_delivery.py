"""Report observed Context/Memory delivery from privacy-safe production manifests.

The report is tenant-scoped and never exports prompt content, content hashes, or
provenance references. Exit codes with ``--check`` are: 0 PASS, 1 FAIL, 2 insufficient
or absent data. This keeps "no traffic" distinct from a passing production gate.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from servicemind.observability.context_delivery import (
    DeliveryStatus,
    build_context_delivery_report,
)
from servicemind.persistence.database import close_database, tenant_session
from servicemind.persistence.models import AgentRun, ContextArtifactRecord

DEFAULT_SYNTHETIC_USER_IDS = (
    "phase5-live-verifier",
    "phase5-model-verifier",
    "phase5-workflow-verifier",
    "phase6-http-live",
    "phase6-live-verifier",
)


async def run(args: argparse.Namespace) -> int:
    tenant_id = UUID(args.tenant_id)
    since = datetime.now(UTC) - timedelta(hours=args.since_hours)
    async with tenant_session(tenant_id) as session:
        rows = (
            await session.execute(
                select(ContextArtifactRecord, AgentRun.user_id)
                .join(AgentRun, AgentRun.id == ContextArtifactRecord.run_id)
                .where(
                    ContextArtifactRecord.agent_role == "analysis",
                    ContextArtifactRecord.created_at >= since,
                )
                .order_by(ContextArtifactRecord.created_at)
            )
        ).all()
    excluded_ids = set() if args.include_synthetic else set(args.exclude_user_id)
    artifacts = [artifact for artifact, user_id in rows if user_id not in excluded_ids]
    excluded_artifacts = len(rows) - len(artifacts)
    observed_excluded_ids = tuple(
        sorted({user_id for _, user_id in rows if user_id in excluded_ids})
    )
    report = build_context_delivery_report(
        artifacts,
        min_analysis_envelopes=args.min_analysis_envelopes,
        min_memory_candidate_envelopes=args.min_memory_candidate_envelopes,
        max_memory_starvation_rate=args.max_memory_starvation_rate,
        excluded_artifacts=excluded_artifacts,
        excluded_user_ids=observed_excluded_ids,
    )
    payload = report.model_dump_json(indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if not args.check:
        return 0
    if report.status is DeliveryStatus.PASS:
        return 0
    if report.status is DeliveryStatus.FAIL:
        return 1
    return 2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--since-hours", type=int, default=24 * 30)
    parser.add_argument("--min-analysis-envelopes", type=int, default=30)
    parser.add_argument("--min-memory-candidate-envelopes", type=int, default=30)
    parser.add_argument("--max-memory-starvation-rate", type=float, default=0.0)
    parser.add_argument(
        "--exclude-user-id",
        action="append",
        default=list(DEFAULT_SYNTHETIC_USER_IDS),
        help="Exclude a synthetic verifier identity; repeat for additional identities.",
    )
    parser.add_argument(
        "--include-synthetic",
        action="store_true",
        help="Include verifier-generated artifacts (never use for a production gate).",
    )
    parser.add_argument("--output")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.since_hours < 1:
        parser.error("--since-hours must be positive")
    try:
        code = asyncio.run(run(args))
    finally:
        asyncio.run(close_database())
    sys.exit(code)


if __name__ == "__main__":
    main()
