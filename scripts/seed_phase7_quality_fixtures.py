"""Seed the Phase 7.6.6 quality corpus and its tickets, then verify what landed.

Two things, in one entry point because the 200 cases need both and a report has to be able
to say when they were laid down:

* **Knowledge.** The 44 documents under ``evaluation/quality/corpus/`` are ingested with the
  ACL their manifest declares. Three of the four case classes depend on the ACL being real
  rather than declared: the ten group-restricted documents are what makes the twenty
  must-refuse-access cases assert something, and the eleven superseded documents are what
  makes the twenty version-conflict cases possible at all. A corpus where everything is
  public and nothing is retired would let both classes pass -- vacuously, by never having
  created the situation they test.

  Retirement goes through ``set_document_active`` rather than through ``is_active=False`` at
  ingest, because the ingest path would make the document never exist, and ACC-02's sibling
  question here is "does the platform prefer the *current* revision" -- which is only asked
  when both revisions were indexed and one was later retired. Each retirement asserts
  ``report.found``: a retirement that matched nothing reads later as "the superseded
  document was not cited", which is the same observation obtained by never having seeded it.

* **Tickets.** ``deploy/glpi/bootstrap_phase7_quality_tickets.php`` creates the eight
  read-only tickets the case list names, and prints their ids, recorded in ``tickets.json``
  for the driver to resolve ``ticket_ref`` against. The refs come from the case list, not
  from a list kept here, so a case repointed to a new ticket cannot be resolved against a
  seeder that never heard of it.

Idempotent, and checked in three steps because "idempotent" is a claim about the second run
rather than the first:

    uv run python scripts/seed_phase7_quality_fixtures.py --check   # expect exit 1
    uv run python scripts/seed_phase7_quality_fixtures.py           # apply
    uv run python scripts/seed_phase7_quality_fixtures.py --check   # expect exit 0

``--check`` writes nothing. An unseeded ``--check`` exiting 1 is the correct answer, not a
failure.

**This seeder does not touch the acceptance corpus.** The two live in the same tenant and
the same serving index generation, and they must not collide: the acceptance fixtures use
``KB-*``-style record ids and this corpus uses ``KB-Q-*``. A clash would make one batch's
retrieval results depend on whether the other batch had been seeded, which is exactly the
kind of coupling that turns two measurements into one.
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

from servicemind.agents.knowledge import KnowledgeAgent
from servicemind.domain.knowledge import (
    AuthorityLevel,
    CorpusScope,
    KnowledgeACL,
    KnowledgeDocument,
)
from servicemind.evaluation.quality import load_quality_cases
from servicemind.persistence.database import close_database
from servicemind.rag.sources import make_document

REPO_ROOT = Path(__file__).resolve().parents[1]
QUALITY = REPO_ROOT / "evaluation" / "quality"
CORPUS = QUALITY / "corpus"
MANIFEST = QUALITY / "manifest.json"
CASES = QUALITY / "cases.v1.json"
TICKETS = QUALITY / "tickets.json"

DEFAULT_CONTAINER = "servicemind-glpi-glpi-1"
BOOTSTRAP_SOURCE = REPO_ROOT / "deploy" / "glpi" / "bootstrap_phase7_quality_tickets.php"
BOOTSTRAP_TARGET = "/var/www/glpi/bootstrap_phase7_quality_tickets.php"

#: A fixed instant in the past, so the fixtures are inside their effective window whatever
#: the clock says -- the same reason ``evaluation/gold.py`` and the acceptance seeder pin
#: their own. A timestamp read from the clock would make "ingested but not yet effective" a
#: state a case could hit.
EFFECTIVE_FROM = datetime(2024, 1, 1, tzinfo=UTC)


def _tail(text: str) -> str:
    """The last non-blank line, which is where PHP puts the fatal error's own sentence."""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def documents(manifest: dict[str, Any]) -> list[KnowledgeDocument]:
    """The manifest as ingestible documents, ACL included."""
    tenant_id = UUID(manifest["tenant_id"])
    built: list[KnowledgeDocument] = []
    for entry in manifest["documents"]:
        path = CORPUS / entry["file"]
        content = path.read_text(encoding="utf-8").strip()
        title = next(
            (line[2:].strip() for line in content.splitlines() if line.startswith("# ")),
            path.stem,
        )
        built.append(
            make_document(
                title=title,
                content=content,
                document_type="quality_fixture",
                source="servicemind_quality_fixture",
                source_version=manifest["schema_version"],
                source_uri=f"quality://globex/{entry['source_record_id']}",
                source_record_id=entry["source_record_id"],
                license_name="project-owned",
                authority=AuthorityLevel.INTERNAL_KNOWLEDGE,
                acl=KnowledgeACL(
                    corpus_scope=CorpusScope.TENANT,
                    tenant_id=tenant_id,
                    # Only the group axis is restricted, and deliberately so: the
                    # must-refuse-access class asserts on that axis alone, and a document
                    # restricted on several axes at once could be excluded for a reason that
                    # is not the one under test.
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


def corpus_is_disjoint_from_acceptance() -> list[str]:
    """Refuse to seed a record id the acceptance corpus already owns.

    The two corpora share a tenant and a serving index, so a reused record id would make
    both batches' citations resolve to one row and neither batch's result attributable to
    its own corpus. Checked here rather than assumed, because the ids are hand-written.
    """
    acceptance = REPO_ROOT / "evaluation" / "acceptance" / "fixtures" / "globex" / "manifest.json"
    if not acceptance.exists():
        return []
    theirs = {
        str(entry["source_record_id"])
        for entry in json.loads(acceptance.read_text(encoding="utf-8"))["documents"]
    }
    ours = {str(entry["source_record_id"]) for entry in load_manifest()["documents"]}
    return [
        f"{item}: claimed by both the acceptance and the quality corpus"
        for item in sorted(theirs & ours)
    ]


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


def case_corpus_problems() -> list[str]:
    """Every document a case names must exist in the manifest, and vice versa is not required.

    Two directions, and only one of them is a problem. A case naming a record id the manifest
    never seeds is a case whose failure would be reported as a retrieval miss when it is
    really a fixture gap -- so that fails. A manifest document no case names is not a
    problem: those are the untargeted halves of version pairs, present so the retrieval step
    has a competing document to be tempted by, which is the whole point of the class.
    """
    manifest_ids = {str(entry["source_record_id"]) for entry in load_manifest()["documents"]}
    case_set = load_quality_cases(CASES)
    named = set(case_set.corpus_record_ids())
    return [
        f"{item}: named by a quality case but not declared in {MANIFEST.name}, so the case "
        "would fail against a document that was never seeded"
        for item in sorted(named - manifest_ids)
    ]


def manifest_orphans() -> list[str]:
    """Manifest entries with no file, and corpus files with no manifest entry."""
    declared = {str(entry["file"]) for entry in load_manifest()["documents"]}
    on_disk = {path.name for path in CORPUS.glob("*.md")}
    return [
        f"{item}: {'declared in the manifest but not on disk' if item in declared else 'on disk but declared by no manifest entry'}"
        for item in sorted(declared ^ on_disk)
    ]


def recorded_ticket_ids() -> dict[str, int]:
    if not TICKETS.exists():
        return {}
    recorded = json.loads(TICKETS.read_text(encoding="utf-8"))
    return {str(name): int(value) for name, value in recorded.get("tickets", {}).items()}


def declared_ticket_refs() -> set[str]:
    """Every ``ticket_ref`` the case list uses, read from the cases rather than listed here."""
    return set(load_quality_cases(CASES).ticket_refs())


def ticket_problems(manifest: dict[str, Any]) -> list[str]:
    if not TICKETS.exists():
        return [f"{TICKETS.relative_to(REPO_ROOT)} does not exist; run without --check first"]
    recorded = json.loads(TICKETS.read_text(encoding="utf-8"))
    missing = sorted(declared_ticket_refs() - set(recorded.get("tickets", {})))
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
    whatever version of the file it was created with, and the ids in ``tickets.json`` would
    describe a ticket nobody wrote.
    """
    subprocess.run(
        ["docker", "cp", str(BOOTSTRAP_SOURCE), f"{container}:{BOOTSTRAP_TARGET}"],
        check=True,
        capture_output=True,
        text=True,
    )
    completed = subprocess.run(
        ["docker", "exec", "-i", container, "php", BOOTSTRAP_TARGET],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        # The child's own stderr, raised rather than left inside a ``CalledProcessError`` the
        # traceback truncates. A PHP fatal error in the bootstrap is the one failure here that
        # has nothing to do with the platform -- a missing autoloader, a renamed GLPI class --
        # and it is exactly the one whose diagnosis is the child's one-line message. Without
        # this the operator sees "exit status 255" and re-runs the container command by hand
        # to find out what it said.
        raise RuntimeError(
            f"the GLPI ticket bootstrap exited {completed.returncode}: "
            f"{_tail(completed.stderr) or _tail(completed.stdout) or 'no output'}"
        )
    lines = [line for line in completed.stdout.strip().splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("the GLPI ticket bootstrap printed nothing on stdout")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        # The contract is one JSON object on the last line. Saying so here is the difference
        # between "Expecting value: line 1 column 1" and "the bootstrap printed its object
        # across several lines", which is what the first version of the PHP did.
        raise RuntimeError(
            f"the last line of the ticket bootstrap's output is not JSON ({exc}): "
            f"{lines[-1][:200]!r}. The bootstrap must print its object on a single line."
        ) from exc


async def run(*, check_only: bool, container: str) -> int:
    manifest = load_manifest()
    tenant_id = UUID(manifest["tenant_id"])
    agent = KnowledgeAgent()
    rag = agent.rag
    try:
        if not check_only:
            totals = await rag.ingest(tenant_id, documents(manifest))
            retired = [
                entry["source_record_id"]
                for entry in manifest["documents"]
                if not entry["is_active"]
            ]
            for record_id in retired:
                report = await rag.set_document_active(tenant_id, record_id, is_active=False)
                if not report.found:
                    raise RuntimeError(
                        f"{record_id}: declared retired in the manifest but no stored "
                        "document carries that source_record_id, so the retirement matched "
                        "nothing"
                    )
            # ``set_document_active`` refreshes the projection itself, so the read below is
            # not racing it.

        ticket_ids = recorded_ticket_ids()
        if not check_only:
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

        indexed = await indexed_projection(agent, manifest)
        problems = (
            problems_for(manifest, indexed)
            + ticket_problems(manifest)
            + case_corpus_problems()
            + manifest_orphans()
            + corpus_is_disjoint_from_acceptance()
        )

        # The retired documents are read back separately: the projection above is keyed by
        # source_record_id over the whole manifest, and a retirement that silently did not
        # take would show up there -- but it is the single most load-bearing fixture state in
        # this corpus, so the report names it rather than leaving it one row among 44.
        superseded = sorted(
            entry["source_record_id"] for entry in manifest["documents"] if not entry["is_active"]
        )
        restricted = {
            str(entry["source_record_id"]): sorted(int(value) for value in entry["group_ids"])
            for entry in manifest["documents"]
            if entry["group_ids"]
        }

        report: dict[str, Any] = {
            "mode": "check" if check_only else "apply",
            "manifest": str(MANIFEST.relative_to(REPO_ROOT)),
            "tenant_id": manifest["tenant_id"],
            "documents": {
                "declared": len(manifest["documents"]),
                "indexed": len(indexed),
                "superseded": superseded,
                "restricted_by_group": restricted,
            },
            "cases": {
                "list": str(CASES.relative_to(REPO_ROOT)),
                "counts": load_quality_cases(CASES).counts(),
                "subjects": load_quality_cases(CASES).subject_usernames(),
                "ticket_refs": sorted(declared_ticket_refs()),
            },
            "tickets": {"entity_id": int(manifest["glpi_entity_id"]), "resolved": ticket_ids},
            "problems": problems,
        }

        if not check_only:
            report["ingest_totals"] = totals

        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 1 if problems else 0
    finally:
        if rag.index.client:
            await rag.index.client.close()
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
