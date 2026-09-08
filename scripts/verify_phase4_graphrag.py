"""Infrastructure smoke test for the Graph-RAG side channel against real Neo4j.

Projects a small incident topology (Phase 4 baseline 4.2 edges) and runs the
structural retriever the KnowledgeAgent uses. Requires the Neo4j container to be up
and Graph-RAG to be enabled:

    SERVICEMIND_GRAPH_RAG_ENABLED=true \
    NEO4J_PASSWORD=<password> \
    PYTHONPATH=src .venv/bin/python scripts/verify_phase4_graphrag.py
"""

import asyncio
import json
import selectors
from pathlib import Path
from uuid import UUID

from servicemind.domain.knowledge import RetrievalPrincipal
from servicemind.graphrag import IncidentRecord, project_incidents
from servicemind.graphrag.build import build_graph_store
from servicemind.graphrag.retrieval import GraphRetriever, to_graph_evidence
from servicemind.rag.query import query_processor

TENANT = UUID("11111111-1111-4111-8111-111111111111")
REPORT_PATH = Path(__file__).resolve().parents[1] / "evaluation" / "reports" / "phase4_graphrag_live.json"


def _demo_records() -> list[IncidentRecord]:
    return [
        IncidentRecord(
            ticket_ref="INC-3001",
            ticket_title="VPN MFA identity-provider timeout at branch A",
            ci_ref="ci-vpn-gw",
            ci_title="VPN Gateway cluster",
            service_ref="svc-auth",
            service_title="Authentication Service",
            problem_ref="PRB-77",
            problem_title="Recurring VPN MFA token failures",
            change_ref="CHG-88",
            change_title="Reissue MFA tokens for affected users",
            runbook_ref="runbook://rb-vpn-mfa",
            runbook_title="VPN MFA token enrollment runbook",
        ),
        IncidentRecord(
            ticket_ref="INC-3002",
            ticket_title="MFA enrollment failures across VPN users",
            ci_ref="ci-vpn-gw",
            ci_title="VPN Gateway cluster",
        ),
        IncidentRecord(
            ticket_ref="INC-3003",
            ticket_title="Slow VPN reconnects after token rotation",
            ci_ref="ci-vpn-gw",
            ci_title="VPN Gateway cluster",
        ),
    ]


async def main() -> None:
    store = build_graph_store()
    if store is None:
        raise RuntimeError(
            "Graph store not built: enable SERVICEMIND_GRAPH_RAG_ENABLED and set NEO4J_PASSWORD"
        )
    try:
        await store.apply_batch(
            project_incidents(TENANT, _demo_records(), similar=[("INC-3001", "INC-3002", 0.9)])
        )
        principal = RetrievalPrincipal(
            tenant_id=TENANT, user_id="graph-smoke", entity_ids=frozenset({1})
        )
        # Deterministic processing keeps this script offline (no LLM rewrite).
        incident_query = await query_processor.process(
            "INC-3001 VPN MFA authentication failure - any similar incidents?",
            use_model=False,
        )
        incident_evidence = to_graph_evidence(
            TENANT,
            await GraphRetriever().retrieve(incident_query, principal, store),
            incident_query,
        )
        # Change anchor (CHG-88): known-problem path + the runbook it implements.
        change_query = await query_processor.process(
            "CHG-88 - what does this approved change resolve and which runbook applies?",
            use_model=False,
        )
        change_evidence = to_graph_evidence(
            TENANT,
            await GraphRetriever().retrieve(change_query, principal, store),
            change_query,
        )
        evidence = [*incident_evidence, *change_evidence]
        relations = sorted({item.metadata["relation"] for item in evidence})
        runbook_refs = sorted(
            {
                ref
                for item in evidence
                for ref in item.metadata["source_refs"]
                if ref.startswith("runbook://")
            }
        )
        if not evidence or not relations:
            raise RuntimeError("Graph-RAG returned no structural evidence for INC-3001/CHG-88")
        if "known_change_scope" not in relations:
            raise RuntimeError("CHG-88 change anchor did not produce known_change_scope")
        if not runbook_refs:
            raise RuntimeError("Graph-RAG surfaced no runbook evidence for the VPN topology")
        top = evidence[0] if evidence else None
        payload = {
            "title": "Phase 4.2 — Graph-RAG side-channel coverage (live Neo4j)",
            "status": "passed",
            "tenant": str(TENANT),
            "store": store.label,
            "relations": relations,
            "evidence": len(evidence),
            "runbook_refs": runbook_refs,
            "top_anchor": top.metadata["anchor"] if top else None,
            "top_source_refs": top.metadata["source_refs"] if top else None,
            "change_anchor_relations": sorted(
                {item.metadata["relation"] for item in change_evidence}
            ),
            "note": (
                "Text Recall/MRR is measured by evaluate_phase4_retrieval.py against "
                "the text gold set; this report measures graph side-channel COVERAGE "
                "(relations + runbook refs) for identifier-anchored probes, because the "
                "text gold corpus carries no incident topology to project."
            ),
        }
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2))
    finally:
        await store.delete_tenant(TENANT)
        await store.close()


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
