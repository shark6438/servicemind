"""Seed the Phase 7.6 acceptance corpus, its tickets, and its graph.

Three halves of the same fixture, in one entry point because the cases need all three and
a report has to be able to say when they were laid down:

* **Knowledge.** The documents under ``evaluation/acceptance/fixtures/globex/`` are
  ingested with the ACL their manifest declares -- two group-restricted, one retired, one
  the decoy, one bulk. The ACL lives in the manifest rather than in the prose so one
  machine-readable place states what is restricted, and the group-restricted documents are
  real: a corpus where everything is public would let ACC-04's isolation assertions pass
  without the isolation existing. The bulk document exists to make ACC-06's payload
  genuinely over budget -- see its entry in the manifest for why that case needs the room
  the envelope used to waste.
* **Tickets.** ``deploy/glpi/bootstrap_phase7_tickets.php`` creates one ticket per case
  that writes, plus the read-only pair, and prints their ids, which are recorded in
  ``tickets.json`` for the driver to resolve ``ticket_ref`` against. GLPI has no
  create-ticket method on the product's own client, so this is the only way the cases have
  real tickets on a fresh volume. The refs are read from the case list rather than listed
  here, so a case that starts using a new ticket cannot be resolved against a seeder that
  never heard of it.
* **Graph.** ``evaluation/graph_probe.py`` projects one CI with two group-restricted
  runbooks hanging off it, reached from the same anchors as the tickets just created. It
  is seeded last because its anchors are the ticket ids, and checked at seed time by
  running ACC-13's own probe: a fixture that no longer gives the case two different
  readings would otherwise be reported as a platform failure by the next run. See that
  module for why the topology is shaped the way it is.

Idempotent, and checked in three steps because "idempotent" is a claim about the second
run rather than the first:

    uv run python scripts/seed_phase7_acceptance_fixtures.py --check   # expect exit 1
    uv run python scripts/seed_phase7_acceptance_fixtures.py           # apply
    uv run python scripts/seed_phase7_acceptance_fixtures.py --check   # expect exit 0

``--check`` writes nothing. An unseeded ``--check`` exiting 1 is the correct answer, not a
failure, and the gate distinguishes the two by re-running it after the apply.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import selectors
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from servicemind.agents.knowledge import KnowledgeAgent
from servicemind.domain.knowledge import (
    AuthorityLevel,
    CorpusScope,
    KnowledgeACL,
    KnowledgeDocument,
)
from servicemind.evaluation.graph_probe import (
    GRAPH_RUNBOOK_GROUPS,
    graph_fixture_batch,
    graph_fixture_problems,
)
from servicemind.graphrag.build import build_graph_store
from servicemind.persistence.database import close_database, tenant_session
from servicemind.persistence.models import MemoryRecordRow
from servicemind.rag.sources import make_document

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "evaluation" / "acceptance" / "fixtures" / "globex"
MANIFEST = FIXTURES / "manifest.json"
TICKETS = FIXTURES / "tickets.json"

#: The frozen case list. Read here for the ticket refs the fixtures have to provide, so the
#: seeder and the driver agree about which tickets exist because they read the same file.
CASES = REPO_ROOT / "evaluation" / "acceptance" / "cases.v1.json"

#: Where the bootstrap lands inside the GLPI container, and how to reach it. Both are
#: overridable so the script is not welded to one composed stack.
DEFAULT_CONTAINER = "servicemind-glpi-glpi-1"
BOOTSTRAP_SOURCE = REPO_ROOT / "deploy" / "glpi" / "bootstrap_phase7_tickets.php"
BOOTSTRAP_TARGET = "/var/www/glpi/bootstrap_phase7_tickets.php"

#: A fixed instant in the past, so the fixtures are inside their effective window whatever
#: the clock says -- the same reason ``evaluation/gold.py`` pins its own. A timestamp read
#: from the clock would make "ingested but not yet effective" a state the cases could hit.
EFFECTIVE_FROM = datetime(2024, 1, 1, tzinfo=UTC)


def load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def documents(manifest: dict[str, Any]) -> list[KnowledgeDocument]:
    """The manifest as ingestible documents, ACL included."""
    tenant_id = UUID(manifest["tenant_id"])
    built: list[KnowledgeDocument] = []
    for entry in manifest["documents"]:
        path = FIXTURES / entry["file"]
        content = path.read_text(encoding="utf-8").strip()
        title = next(
            (line[2:].strip() for line in content.splitlines() if line.startswith("# ")),
            path.stem,
        )
        built.append(
            make_document(
                title=title,
                content=content,
                document_type="acceptance_fixture",
                source="servicemind_acceptance_fixture",
                source_version=manifest["schema_version"],
                source_uri=f"acceptance://globex/{entry['source_record_id']}",
                source_record_id=entry["source_record_id"],
                license_name="project-owned",
                authority=AuthorityLevel.INTERNAL_KNOWLEDGE,
                acl=KnowledgeACL(
                    corpus_scope=CorpusScope.TENANT,
                    tenant_id=tenant_id,
                    # Entity and profile are deliberately left unrestricted: ACC-04 is about
                    # the group axis, and a document restricted on several axes at once
                    # could be excluded for a reason that is not the one under test.
                    group_ids=frozenset(entry["group_ids"]),
                    effective_from=EFFECTIVE_FROM,
                ),
                metadata={"fixture_role": entry["role"]},
            )
        )
    return built


async def indexed_projection(agent: KnowledgeAgent, manifest: dict[str, Any]) -> dict[str, Any]:
    tenant_id = UUID(manifest["tenant_id"])
    ids = [entry["source_record_id"] for entry in manifest["documents"]]
    return await agent.rag.index.documents_by_source_record_id(tenant_id, ids)


def problems_for(manifest: dict[str, Any], indexed: dict[str, Any]) -> list[str]:
    """Compare what the retrieval filter will see against what the manifest declares."""
    problems: list[str] = []
    for entry in manifest["documents"]:
        record_id = entry["source_record_id"]
        row = indexed.get(record_id)
        if row is None:
            problems.append(f"{record_id}: not in the serving index generation")
            continue
        indexed_groups = sorted(int(value) for value in row.get("group_ids", []))
        if indexed_groups != sorted(entry["group_ids"]):
            problems.append(
                f"{record_id}: indexed group_ids {indexed_groups}, "
                f"manifest declares {sorted(entry['group_ids'])}"
            )
        if bool(row.get("is_active")) is not entry["is_active"]:
            problems.append(
                f"{record_id}: indexed is_active={row.get('is_active')}, "
                f"manifest declares {entry['is_active']}"
            )
        if str(row.get("tenant_id")) != manifest["tenant_id"]:
            problems.append(
                f"{record_id}: indexed tenant {row.get('tenant_id')}, "
                f"manifest declares {manifest['tenant_id']}"
            )
    return problems


def recorded_ticket_ids() -> dict[str, int]:
    """The tickets the last apply resolved, or nothing when it never ran."""
    if not TICKETS.exists():
        return {}
    recorded = json.loads(TICKETS.read_text(encoding="utf-8"))
    return {str(name): int(value) for name, value in recorded.get("tickets", {}).items()}


def declared_ticket_refs() -> tuple[set[str], set[str]]:
    """Every ``ticket_ref`` the case list uses, split into the ones a case writes to.

    Read from the cases rather than written down here: a list of refs kept beside the case
    list is a list that goes stale the first time a case is repointed, and the failure it
    produces is a fixture that looks seeded and a driver that cannot resolve a ticket.
    ``writes_followups`` is the case's own declaration that it appends to its ticket, which
    is what makes the ticket its own.
    """
    case_set = json.loads(CASES.read_text(encoding="utf-8"))
    cases = case_set["cases"] if isinstance(case_set, dict) else case_set
    read: set[str] = set()
    written: set[str] = set()
    for case in cases:
        refs = {case.get("ticket_ref")} | {step.get("ticket_ref") for step in case.get("steps", [])}
        refs.discard(None)
        if case.get("writes_followups"):
            written |= refs
        else:
            read |= refs
    return read, written


def ticket_problems(manifest: dict[str, Any]) -> list[str]:
    if not TICKETS.exists():
        return [f"{TICKETS.relative_to(REPO_ROOT)} does not exist; run without --check first"]
    recorded = json.loads(TICKETS.read_text(encoding="utf-8"))
    read, written = declared_ticket_refs()
    referenced = read | written
    missing = sorted(referenced - set(recorded.get("tickets", {})))
    if missing:
        return [f"no resolved ticket id recorded for {missing}"]
    if int(recorded.get("entity_id", -1)) != int(manifest["glpi_entity_id"]):
        return [
            f"tickets.json claims entity {recorded.get('entity_id')}, "
            f"manifest declares {manifest['glpi_entity_id']}"
        ]
    return []


def bootstrap_tickets(container: str) -> dict[str, Any]:
    """Copy the bootstrap in and run it, returning what it resolved.

    The copy is not incidental: a container built from an older image would otherwise run
    whatever version of the file it was created with, and the ids in ``tickets.json``
    would describe a ticket nobody wrote.
    """
    subprocess.run(
        ["docker", "cp", str(BOOTSTRAP_SOURCE), f"{container}:{BOOTSTRAP_TARGET}"],
        check=True,
        capture_output=True,
        text=True,
    )
    completed = subprocess.run(
        ["docker", "exec", "-i", container, "php", BOOTSTRAP_TARGET],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


async def memory_baseline(tenant_id: UUID) -> dict[str, Any]:
    """What the tenant's memory table already held. The cases assert increments.

    Recorded rather than assumed absent: Globex is not a blank tenant, and a run that
    happened to retrieve a pre-existing memory would otherwise be reported as having
    produced one. The ``source_run_id`` on each record is what makes the increment
    checkable, and this baseline is what makes the *growth* visible in the report.
    """
    async with tenant_session(tenant_id) as session:
        total = await session.scalar(
            select(func.count())
            .select_from(MemoryRecordRow)
            .where(MemoryRecordRow.tenant_id == tenant_id)
        )
        by_type = (
            await session.execute(
                select(MemoryRecordRow.memory_type, func.count())
                .where(MemoryRecordRow.tenant_id == tenant_id)
                .group_by(MemoryRecordRow.memory_type)
            )
        ).all()
    return {
        "recorded_at": datetime.now(UTC).isoformat(),
        "total": int(total or 0),
        "by_type": {str(name): int(count) for name, count in by_type},
    }


async def run(*, check_only: bool, container: str) -> int:
    manifest = load_manifest()
    tenant_id = UUID(manifest["tenant_id"])
    agent = KnowledgeAgent()
    rag = agent.rag
    graph_store = build_graph_store()
    try:
        if not check_only:
            totals = await rag.ingest(tenant_id, documents(manifest))
            retired = [
                entry["source_record_id"]
                for entry in manifest["documents"]
                if not entry["is_active"]
            ]
            for record_id in retired:
                # A retirement that matched nothing is the fixture failing to exist, and
                # it would read later as "the retired runbook was not cited" -- which is
                # the same observation ACC-02 wants, obtained by never having seeded the
                # document. Assert the effect instead of inferring it from its absence.
                report = await rag.set_document_active(tenant_id, record_id, is_active=False)
                if not report.found:
                    raise RuntimeError(
                        f"{record_id}: declared retired in the manifest but no stored "
                        "document carries that source_record_id, so the retirement "
                        "matched nothing"
                    )
            # ``set_document_active`` refreshes the projection itself, so the read below
            # is not racing it.

        ticket_ids = recorded_ticket_ids()
        if not check_only:
            # Tickets first: the graph fixture hangs off their ids, so the anchors can
            # only be named once GLPI has resolved them.
            report_tickets = bootstrap_tickets(container)
            ticket_ids = {
                str(name): int(value) for name, value in report_tickets["tickets"].items()
            }
            TICKETS.write_text(
                json.dumps(
                    {
                        **report_tickets,
                        "resolved_at": datetime.now(UTC).isoformat(),
                        "bootstrap": str(BOOTSTRAP_SOURCE.relative_to(REPO_ROOT)),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

        graph_problems: list[str] = []
        if graph_store is None:
            graph_problems.append(
                "SERVICEMIND_GRAPH_RAG_ENABLED is off or NEO4J_PASSWORD is unset, so ACC-13's "
                "graph fixture cannot be laid down and its positive control cannot be observed"
            )
        elif not ticket_ids:
            graph_problems.append("no ticket ids are resolved, so no graph anchor can be named")
        else:
            anchor = ticket_ids["globex-vpn-mfa-a"]
            # Only the read-only tickets become graph anchors. A writing case's ticket
            # would otherwise project a node every read case can reach -- the traversal
            # goes ticket -> CI -> sibling tickets -- so a ticket added for one case would
            # change what every other case's evidence says about the topology. The writers
            # do not assert on graph evidence; the readers do.
            read_refs, _ = declared_ticket_refs()
            if not check_only:
                await graph_store.apply_batch(
                    graph_fixture_batch(
                        tenant_id,
                        entity_id=int(manifest["glpi_entity_id"]),
                        ticket_ids=sorted(
                            ticket_ids[ref] for ref in read_refs if ref in ticket_ids
                        ),
                    )
                )
            graph_problems.extend(
                await graph_fixture_problems(
                    graph_store,
                    tenant_id,
                    entity_id=int(manifest["glpi_entity_id"]),
                    ticket_id=anchor,
                )
            )

        indexed = await indexed_projection(agent, manifest)
        problems = problems_for(manifest, indexed) + ticket_problems(manifest) + graph_problems

        report: dict[str, Any] = {
            "mode": "check" if check_only else "apply",
            "manifest": str(MANIFEST.relative_to(REPO_ROOT)),
            "tenant_id": manifest["tenant_id"],
            "documents": {
                entry["source_record_id"]: {
                    "group_ids": sorted(
                        int(value)
                        for value in indexed.get(entry["source_record_id"], {}).get("group_ids", [])
                    ),
                    "is_active": bool(indexed.get(entry["source_record_id"], {}).get("is_active")),
                    "declared_group_ids": sorted(entry["group_ids"]),
                    "declared_is_active": entry["is_active"],
                }
                for entry in manifest["documents"]
            },
            "graph": {
                "store": getattr(graph_store, "label", None),
                "anchors": sorted(ticket_ids.values()),
                "restricted_runbooks": GRAPH_RUNBOOK_GROUPS,
                "problems": graph_problems,
            },
            "problems": problems,
        }

        if not check_only:
            report["ingest_totals"] = totals
            report["tickets"] = {
                "entity_id": int(manifest["glpi_entity_id"]),
                "tickets": ticket_ids,
            }
            report["memory_baseline"] = await memory_baseline(tenant_id)

        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 1 if problems else 0
    finally:
        if rag.index.client:
            await rag.index.client.close()
        if graph_store is not None:
            await graph_store.close()
        await close_database()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the fixtures are in place and write nothing; exit 1 when they are not",
    )
    parser.add_argument("--container", default=DEFAULT_CONTAINER, help="the GLPI container")
    args = parser.parse_args()
    return asyncio.run(
        run(check_only=args.check, container=args.container),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )


if __name__ == "__main__":
    sys.exit(main())
