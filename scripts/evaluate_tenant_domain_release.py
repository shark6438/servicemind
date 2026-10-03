"""Run a tenant-domain release set through production, and probe its ACL strata.

``evaluate_phase4_retrieval_techqa.py`` runs a public silver set through the production
path and reports the §4.1 count as zero -- correctly, and uninformatively: that corpus has
one tenant, no group restrictions and no clock strata, so the count had no way to be
anything else. This script runs a corpus that has them.

Two measurements, and the second is the one §4.1 is about.

* **The release set itself**, through the harness, with the corpus's ACLs handed to the
  audit. This is the ordinary retrieval evaluation, over strata rather than one topic.
* **A falsification probe per protected document.** The harness asks every query as the
  caller the queryset recorded, so on a correct pipeline it will report no violations --
  which is the same reading a broken checker would give. The probe asks instead as a caller
  who must *not* see the document, using a question that matches it closely, and asks a
  second time as a caller who *must*. The second is the control: without it, a pipeline
  that returned nothing at all would pass the first.

Neither measurement is a release verdict. The set is synthetic and says so, so the §4.1
thresholds stay inapplicable, and the blockers are reported rather than assumed. What the
script establishes is that the gate's inputs are computable and that the pipeline's ACL
filter is exercised in both directions -- which is what a tenant-domain set is for.

Usage:
    uv run python scripts/evaluate_tenant_domain_release.py
    uv run python scripts/evaluate_tenant_domain_release.py --keep-index
"""

from __future__ import annotations

import argparse
import asyncio
import json
import selectors
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from core import settings
from servicemind.domain.knowledge import KnowledgeDocument, RetrievalPrincipal
from servicemind.evaluation.gold import QueryCategory
from servicemind.evaluation.harness import BASELINES, evaluate, markdown_report, result_keys
from servicemind.evaluation.leakage import summarize, visible_evidence_violations
from servicemind.evaluation.tenant_domain import TENANTS, TenantDomainFixture
from servicemind.rag.models import TeiEmbeddingProvider, TeiReranker
from servicemind.rag.opensearch import OpenSearchKnowledgeIndex
from servicemind.rag.service import EnterpriseRAG, build_opensearch_client

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "evaluation" / "gold" / "tenant-domain-release"
OUT_JSON = ROOT / "evaluation" / "reports" / "tenant_domain_release_latest.json"
OUT_MD = ROOT / "evaluation" / "reports" / "tenant_domain_release_latest.md"

#: The documents the probe defends, and the two askers it defends them against.
#:
#: Each entry names a document that is protected on exactly one axis, the caller who must
#: not see it, and the caller who must. Both askers are written out rather than derived:
#: the probe's whole value is that it states in advance who is allowed to see what, so a
#: reader can check the expectation instead of watching the code agree with itself.
PROBES: tuple[dict[str, Any], ...] = (
    {
        "document": "KB-ACME-VPN-MFA-G3",
        "axis": "group",
        "query": "What does the Network Team VPN MFA break-glass runbook say about the sealed credential store?",
        "denied": {"tenant": "acme", "groups": frozenset()},
        "allowed": {"tenant": "acme", "groups": frozenset({3})},
        "why": "group 3's runbook; an acme caller holding no groups is inside the tenant and "
        "still outside the group",
    },
    {
        "document": "KB-ACME-VPN-MFA-G4",
        "axis": "group",
        "query": "What script does the Service Desk follow to complete a VPN MFA rebind?",
        "denied": {"tenant": "acme", "groups": frozenset({3})},
        "allowed": {"tenant": "acme", "groups": frozenset({4})},
        "why": "the counterpart probe: group 3 holds a group, just not this one, so a filter "
        "that let any group through would pass the first probe and fail this one",
    },
    {
        "document": "KB-GLOBEX-CITRIX-BREAKGLASS",
        "axis": "tenant",
        "query": "What does the Globex Citrix break-glass procedure require for its credential store?",
        # The two sides hold the *same* groups, so the tenant is the only thing that
        # differs between them and therefore the only filter that can be doing the work.
        # The document itself declares no group restriction, so those ids are inert either
        # way -- but the pair has to be one axis apart for the probe to be about an axis.
        "denied": {"tenant": "acme", "groups": frozenset({3, 4})},
        "allowed": {"tenant": "globex", "groups": frozenset({3, 4})},
        "why": "another tenant's document. Both callers hold the same groups, so nothing "
        "but the tenant filter can be what excludes the denied one",
    },
    {
        "document": "KB-ACME-VPN-MFA-LEGACY",
        "axis": "retirement flag",
        "query": "What is the supported way to bypass a failed multi-factor challenge?",
        "denied": {"tenant": "acme", "groups": frozenset()},
        "allowed": None,
        "why": "withdrawn, and unrestricted by group, so the group filter passes for every "
        "caller and only the retirement flag can exclude it. There is no allowed caller: "
        "the document must not be served to anyone",
    },
    {
        "document": "KB-ACME-VPN-MFA-V1",
        "axis": "effective window (superseded)",
        "query": "What is the VPN MFA token reissue procedure from the original single-region deployment?",
        "denied": {"tenant": "acme", "groups": frozenset()},
        "allowed": None,
        "why": "active in the directory but outside its own effective window at the query "
        "time -- a different exclusion from the retirement flag, and a filter that only "
        "reads is_active would serve it",
    },
    {
        "document": "KB-ACME-VPN-MFA-Q3",
        "axis": "effective window (not yet in force)",
        "query": "How does the phased VPN MFA rollout change enforcement from 2026-06-01?",
        "denied": {"tenant": "acme", "groups": frozenset()},
        "allowed": None,
        "why": "approved and not yet in force; the other end of the clock check, which a "
        "filter comparing only against effective_from would serve",
    },
)


def _principal(
    tenant: str, groups: frozenset[int], label: str, query_time: datetime
) -> RetrievalPrincipal:
    """A principal at the *fixture's* clock, never at wall-clock now.

    This is not a detail. The corpus has documents on both sides of its own effective
    windows, and ``RetrievalPrincipal`` defaults ``query_time`` to the moment it is
    constructed -- so a principal built here without one asks a question dated today, at
    which point a document that is "not yet in force" for the fixture is simply in force.
    The clock stratum then reports itself clean and the run reads as evidence that the
    window filter works. The first version of this script did exactly that, and the probe
    for the not-yet-in-force document reported ``surfaced: true, violations: 0`` -- both
    true, and jointly meaningless, because the two halves were being asked about different
    instants.
    """
    return RetrievalPrincipal(
        tenant_id=TENANTS[tenant],
        user_id=label,
        group_ids=groups,
        entity_ids=frozenset(),
        query_time=query_time,
    )


class MemoryRepository:
    """The production expansion call, served from memory instead of under RLS."""

    def __init__(self) -> None:
        self.parent_content: dict[UUID, str] = {}

    async def is_current(self, tenant_id: UUID, document: KnowledgeDocument) -> bool:
        return False

    async def replace(self, tenant_id: UUID, document: KnowledgeDocument, parents, children):
        for parent in parents:
            self.parent_content[parent.parent_chunk_id] = parent.content
        return document, parents, children

    async def mark_indexed(self, tenant_id: UUID, document_id: UUID) -> None:
        return None

    async def count_pending(self, tenant_id: UUID) -> int:
        return 0

    async def parents(self, tenant_id: UUID, ids) -> dict[UUID, str]:
        return {item: self.parent_content[item] for item in ids if item in self.parent_content}

    async def authorized_parents(self, principal, hits):
        return {
            hit.parent_chunk_id: self.parent_content[hit.parent_chunk_id]
            for hit in hits
            if hit.parent_chunk_id in self.parent_content
        }


async def _probe(rag: EnterpriseRAG, fixture: TenantDomainFixture, entry: dict[str, Any]):
    """Ask one protected document's question twice, as the caller who must not and must.

    Both sides run the same query text through the same arms, so the only thing that
    differs is the principal. That is what makes the pair a test of the filter rather than
    of the question.
    """
    coordinates = fixture.coordinates()
    arms: list[dict[str, Any]] = []
    for baseline in BASELINES:
        row: dict[str, Any] = {"baseline": baseline.name}
        for side, spec in (("denied", entry["denied"]), ("allowed", entry["allowed"])):
            if spec is None:
                row[side] = None
                continue
            principal = _principal(
                spec["tenant"], spec["groups"], f"probe-{side}", fixture.query_time
            )
            result = await rag.retrieve(
                principal=principal,
                query=entry["query"],
                mode=baseline.mode,
                run_rerank=baseline.run_rerank,
                final_k=24,
                use_query_model=False,
            )
            keys = result_keys(result)
            violations = visible_evidence_violations(
                query_id=f"probe:{entry['document']}:{side}",
                retrieved=keys,
                coordinates=coordinates,
                asker_tenant_id=principal.tenant_id,
                asker_group_ids=principal.group_ids,
                query_time=principal.query_time,
            )
            row[side] = {
                "returned": len(keys),
                "surfaced_the_protected_document": entry["document"] in keys,
                "violations": summarize(violations),
            }
        arms.append(row)
    return arms


def _verdict(entry: dict[str, Any], arms: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether the probe held, stated as the two claims it is testing separately.

    ``isolation`` is the security claim: the denied caller never saw it. ``control`` is the
    claim that makes the first one mean something: an allowed caller did. The retired and
    out-of-window probes have no control by construction -- nobody may see them -- so their
    verdict is the isolation claim alone, plus the requirement that the document was
    actually indexed, without which "returned nothing" would be indistinguishable from
    "was never there".
    """
    leaked = [
        arm["baseline"]
        for arm in arms
        if arm["denied"] and arm["denied"]["surfaced_the_protected_document"]
    ]
    violations = sum(arm["denied"]["violations"]["total"] for arm in arms if arm["denied"])
    result: dict[str, Any] = {
        "document": entry["document"],
        "axis": entry["axis"],
        "why": entry["why"],
        "isolation_held": not leaked,
        "bases_that_surfaced_it": leaked,
        "violations_across_arms": violations,
        "arms": arms,
    }
    if entry["allowed"] is None:
        result["control"] = None
        result["control_note"] = (
            "no caller may see this document; the isolation claim is the whole test"
        )
    else:
        surfaced = [
            arm["baseline"]
            for arm in arms
            if arm["allowed"] and arm["allowed"]["surfaced_the_protected_document"]
        ]
        result["control"] = bool(surfaced)
        result["bases_that_surfaced_it_for_the_allowed_caller"] = surfaced
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-k", default="5,10")
    parser.add_argument("--keep-index", action="store_true")
    args = parser.parse_args()

    top_ks = tuple(int(x) for x in args.top_k.split(",") if x.strip())
    fixture = TenantDomainFixture(FIXTURE)
    documents = fixture.documents()
    gold = fixture.gold_set()
    coordinates = fixture.coordinates()
    print(
        f"documents={len(documents)} queries={len(gold.queries)} strata={fixture.strata()} "
        f"blockers={len(gold.release_gate_blockers)}"
    )

    client = build_opensearch_client()
    embedding = TeiEmbeddingProvider(
        settings.SERVICEMIND_EMBEDDING_URL,
        model_revision=settings.SERVICEMIND_EMBEDDING_REVISION,
    )
    reranker = TeiReranker(
        settings.SERVICEMIND_RERANKER_URL,
        model_revision=settings.SERVICEMIND_RERANKER_REVISION,
    )
    index = OpenSearchKnowledgeIndex(
        client, prefix=f"sm-tdrel-{uuid4().hex[:8]}", dimension=embedding.dimension
    )
    rag = EnterpriseRAG(
        index=index, embedding=embedding, reranker=reranker, repository=MemoryRepository()
    )
    try:
        by_tenant: dict[UUID, list[KnowledgeDocument]] = {}
        for document in documents:
            by_tenant.setdefault(document.acl.tenant_id, []).append(document)
        started = time.perf_counter()
        totals = {}
        for tenant_id, batch in by_tenant.items():
            totals[str(tenant_id)] = await rag.ingest(tenant_id, batch)
        ingest_seconds = time.perf_counter() - started
        print(f"ingested in {ingest_seconds:.1f}s: {totals}")

        class HarnessProvider:
            async def retrieve(self, **kwargs: object):
                return await rag.retrieve(use_query_model=False, **kwargs)

        report = await evaluate(
            HarnessProvider(),
            gold,
            _principal("acme", frozenset(), "release-set-default", fixture.query_time),
            top_ks=top_ks,
            coordinates=coordinates,
        )
        results = {
            metrics.baseline: {
                "recall_at_5": metrics.recall_at(5),
                "recall_at_10": metrics.recall_at(10),
                "answered_unanswerable": metrics.answered_unanswerable(),
                "abstention_rate": round(metrics.abstention_rate(), 4),
                "visible_evidence_violations": summarize(metrics.visible_evidence_violations()),
            }
            for metrics in report.baselines
        }

        probes = []
        for entry in PROBES:
            arms = await _probe(rag, fixture, entry)
            probes.append(_verdict(entry, arms))
            print(
                f"probe {entry['document']} ({entry['axis']}): "
                f"isolation={probes[-1]['isolation_held']} control={probes[-1]['control']}"
            )

        payload = {
            "schema_version": "tenant-domain-release-v1",
            "status": "PIPELINE_COMPARISON_ONLY",
            "quality_certification": False,
            "status_semantics": (
                "the corpus has the strata the §4.1 gate is about and the labels are "
                "synthetic, so this run shows the gate's inputs are computable and the "
                "ACL filter holds in both directions. It certifies nothing about quality: "
                "`release_gate_blockers` below names the one clause no fixture can satisfy"
            ),
            "release_gate_blockers": gold.release_gate_blockers,
            "corpus": {
                "documents_indexed": len(documents),
                "strata": fixture.strata(),
                "ingest_seconds": round(ingest_seconds, 1),
                "totals": totals,
            },
            "queries": {
                "total": len(gold.queries),
                "answerable": len(gold.answerable),
                "unanswerable": len(gold.unanswerable),
                "by_category": {
                    category.value: len(gold.by_category(category)) for category in QueryCategory
                },
                "refusal_reasons": sorted(
                    {q.refusal_reason.value for q in gold.unanswerable if q.refusal_reason}
                ),
            },
            "results": results,
            "acl_probes": probes,
            "limitations": [
                "the labels are synthetic and the provenance says so; no human signed them",
                "the corpus is fixture-sized, so no stratum here is large enough to estimate "
                "a per-stratum rate from -- the strata are present, not powered",
                "the recall figures are saturated: each query was authored against a document "
                "whose title shares its vocabulary, over a corpus of fourteen. Recall@5 of "
                "1.000 is a property of the fixture, not a quality measurement, and nothing "
                "in this report should be read as one",
                "`abstention_rate` counts unanswerable queries that returned any context. It "
                "is not the Reviewer's semantic abstention and must not be reported as an "
                "answerable answer rate",
                "parents are held in memory rather than read under PostgreSQL row-level "
                "security, so this exercises the expansion call and not the RLS policy",
            ],
        }
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        OUT_MD.write_text(_markdown(payload, markdown_report(report)), encoding="utf-8")
        print(json.dumps({"results": results, "probes": probes}, indent=2))
    finally:
        if not args.keep_index:
            for tenant_id in {document.acl.tenant_id for document in documents}:
                try:
                    await index.drop_tenant_indices(tenant_id)
                except Exception as error:  # noqa: BLE001 - cleanup must not mask the result
                    print(f"cleanup warning for {tenant_id}: {error}")
        await client.close()


def _markdown(payload: dict[str, Any], harness_markdown: str) -> str:
    strata = payload["corpus"]["strata"]
    blockers = "\n".join(f"- {item}" for item in payload["release_gate_blockers"])
    limitations = "\n".join(f"- {item}" for item in payload["limitations"])

    probe_rows = []
    for probe in payload["acl_probes"]:
        leaked = ", ".join(probe["bases_that_surfaced_it"]) or "none"
        control = (
            "n/a (no caller may see it)"
            if probe["control"] is None
            else ("held" if probe["control"] else "**BROKEN**")
        )
        probe_rows.append(
            f"| {probe['document']} | {probe['axis']} | "
            f"{'held' if probe['isolation_held'] else '**BROKEN**'} | {leaked} | "
            f"{control} | {probe['violations_across_arms']} |"
        )

    result_rows = []
    for arm, row in payload["results"].items():
        counts = row["visible_evidence_violations"]
        result_rows.append(
            f"| {arm} | {row['recall_at_5']:.4f} | {row['recall_at_10']:.4f} | "
            f"{row['answered_unanswerable']} | {row['abstention_rate']:.3f} | "
            f"{counts['wrong_tenant']} | {counts['unauthorized_group']} | "
            f"{counts['expired_version']} | {counts['total']} |"
        )

    reasons = "\n".join(
        f"- **{probe['document']}** ({probe['axis']}): {probe['why']}"
        for probe in payload["acl_probes"]
    )

    return f"""# Tenant-domain release set through the production retrieval path

Status: **{payload["status"]}** — {payload["corpus"]["documents_indexed"]} documents, \
{payload["queries"]["total"]} queries, ingest {payload["corpus"]["ingest_seconds"]}s.

This corpus has what the public silver sets do not: {strata["tenants"]} tenants, \
{strata["group_restricted"]} group-restricted documents, {strata["inactive"]} withdrawn, \
{strata["with_an_effective_end"]} past their effective end and {strata["not_yet_in_force"]} \
not yet in force. The §4.1 clauses each have something here to be about.

## Retrieval over the strata

| arm | recall@5 | recall@10 | answered-unanswerable | abstention | wrong tenant | unauthorized group | expired version | total |
|---|---|---|---|---|---|---|---|---|
{chr(10).join(result_rows)}

The violation columns are the §4.1 count, computed against the ACLs the loader assigned. \
They are zero here for a different reason than they are zero on a single-tenant corpus: \
there are documents they could be nonzero about.

## ACL probes

Each protected document is asked for twice — once by a caller who must not see it, once by \
a caller who must. The control is what makes the first column mean something.

| document | axis | isolation | bases that leaked it | control | violations |
|---|---|---|---|---|---|
{chr(10).join(probe_rows)}

{reasons}

## Why §4.1 still does not apply

{blockers}

## Limitations

{limitations}

## Per-arm detail

{harness_markdown}
"""


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
