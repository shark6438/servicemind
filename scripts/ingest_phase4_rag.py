"""Idempotently ingest approved Phase 4 sources into the tenant RAG indexes."""

from __future__ import annotations

import argparse
import asyncio
import json
import selectors
from pathlib import Path
from uuid import UUID

from servicemind.agents.knowledge import RUNBOOKS
from servicemind.rag.service import build_enterprise_rag
from servicemind.rag.sources import (
    InternalRunbookSource,
    MendeleyHistoricalCaseSource,
    PagerDutyMarkdownSource,
)

ROOT = Path(__file__).resolve().parents[1]
PAGERDUTY_REVISION = "464fc9d3e47e19e9d8da17cec1a41dc09624e95a"


async def run(tenant_id: UUID, *, source_names: set[str]) -> dict:
    rag = build_enterprise_rag()
    catalog = {
        "internal": InternalRunbookSource(tenant_id, RUNBOOKS),
        "pagerduty": PagerDutyMarkdownSource(
            ROOT / "data/phase4/raw/production/incident-response-docs-master", PAGERDUTY_REVISION
        ),
        "mendeley": MendeleyHistoricalCaseSource(
            ROOT / "data/phase4/raw/production/mendeley-helpdesk-v3"
        ),
    }
    sources = [catalog[name] for name in sorted(source_names)]
    total = {"documents": 0, "parents": 0, "children": 0, "skipped": 0}
    try:
        total["reconciled"] = await rag.reconcile(tenant_id)
        for source in sources:
            result = await rag.ingest(tenant_id, await source.load())
            for key, value in result.items():
                total[key] += value
    finally:
        await rag.index.client.close()
    return total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=["internal", "pagerduty", "mendeley"],
        default=["internal", "pagerduty"],
    )
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                run(args.tenant_id, source_names=set(args.sources)),
                loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
