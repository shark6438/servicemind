"""Live docling/PDF ingest acceptance against real OpenSearch + a real PDF.

Loads a real PDF through ``AttachmentSource`` (metadata["source_file"] routing) and
drives it through ``EnterpriseRAG.ingest`` so ``StructureParser.parse_file`` runs the
docling-aware path with the pypdf fallback. Then probes retrieval with phrases that
exist only inside the PDF, proving the binary reached the search index.

Run offline (HF_HUB_OFFLINE=1) unless the docling PDF-layout snapshot is already
cached: without the snapshot docling degrades to pypdf plain-text, which is the
honest, documented fallback this script asserts. Requires the real corpus PDF
(fetch_phase4_corpora.py) or any PDF via --pdf.

    HF_HUB_OFFLINE=1 PYTHONPATH=src .venv/bin/python scripts/verify_phase4_docling.py

Writes evaluation/reports/phase4_docling_live.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import selectors
from pathlib import Path
from uuid import UUID, uuid4

from servicemind.domain.knowledge import RetrievalPrincipal
from servicemind.rag.models import (
    CallableReranker,
    DeterministicEmbeddingProvider,
)
from servicemind.rag.opensearch import OpenSearchKnowledgeIndex
from servicemind.rag.parsing import PDF_SUFFIXES, structure_parser
from servicemind.rag.service import EnterpriseRAG, build_opensearch_client
from servicemind.rag.sources import AttachmentSource

TENANT = UUID("11111111-1111-4111-8111-111111111111")
REPORT_PATH = Path("evaluation/reports/phase4_docling_live.json")
DEFAULT_PDF = (
    Path("data/phase4/raw/production/incident-response-docs-master")
    / "docs/assets/pdf/pagerduty_incident_response_training_public.pdf"
)


class MemoryRepository:
    """RLS-authority substitute so the harness runs without PostgreSQL."""

    def __init__(self) -> None:
        self.parent_content: dict[UUID, str] = {}

    async def is_current(self, tenant_id, document) -> bool:
        return False

    async def replace(self, tenant_id, document, parents, children):
        for parent in parents:
            self.parent_content[parent.parent_chunk_id] = parent.content
        return document, parents, children

    async def mark_indexed(self, tenant_id, document_id) -> None:
        return None

    async def count_pending(self, tenant_id) -> int:
        return 0

    async def parents(self, tenant_id, ids):
        return {item: self.parent_content[item] for item in ids if item in self.parent_content}


def _reranker() -> CallableReranker:
    return CallableReranker(
        lambda query, text: len(set(query.casefold().split()) & set(text.casefold().split()))
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    args = parser.parse_args()
    pdf: Path = args.pdf.resolve()
    if not pdf.is_file():
        raise SystemExit(
            f"PDF not found: {pdf}\nRun scripts/fetch_phase4_corpora.py first, or pass --pdf."
        )
    if pdf.suffix.casefold() not in PDF_SUFFIXES:
        raise SystemExit(f"not a PDF: {pdf}")
    from pypdf import PdfReader

    reader = PdfReader(str(pdf))
    page_count = len(reader.pages)
    client = build_opensearch_client()
    embedding = DeterministicEmbeddingProvider()
    index = OpenSearchKnowledgeIndex(
        client,
        prefix=f"sm-rag-docling-{uuid4().hex[:8]}",
        dimension=embedding.dimension,
    )
    rag = EnterpriseRAG(
        index=index,
        embedding=embedding,
        reranker=_reranker(),
        repository=MemoryRepository(),
    )
    try:
        docs = await AttachmentSource(pdf.parent, tenant_id=TENANT).load()
        pdf_docs = [d for d in docs if d.metadata.get("file_format") == ".pdf"]
        if not pdf_docs:
            raise RuntimeError(f"AttachmentSource produced no PDF document for {pdf}")
        totals = await rag.ingest(TENANT, pdf_docs)
        # Parser evidence: the blocks that reached the search index came from
        # parse_file. With the docling snapshot absent the pypdf fallback is expected;
        # report which parser actually ran so the offline limitation is explicit.
        probe = structure_parser.parse_file(pdf, pdf_docs[0])
        parsers = sorted({b.metadata.get("parser", "docling") for b in probe if b.metadata})
        principal = RetrievalPrincipal(
            tenant_id=TENANT, user_id="docling-verify", entity_ids=frozenset({1})
        )
        # Phrases chosen from pypdf text of the PagerDuty IR training slides.
        probes = {
            "incident commander": "What is the role of an incident commander?",
            "severity levels": "How are severity levels assigned during an incident?",
            "Incident Response Training": "Describe the incident response training material.",
        }
        results = {}
        for label, question in probes.items():
            result = await rag.retrieve(
                principal=principal,
                query=question,
                use_query_model=False,
                final_k=3,
            )
            results[label] = {
                "items": len(result.items),
                "top_source_record_id": (
                    result.items[0].citation.source_record_id if result.items else None
                ),
                "top_uri": result.items[0].citation.source_uri if result.items else None,
            }
        pdf_hits = [
            label
            for label, res in results.items()
            if res["top_source_record_id"] and res["top_source_record_id"].endswith(".pdf")
        ]
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "title": "Phase 4 — docling/PDF ingest routing (live OpenSearch + real PDF)",
            "status": "passed" if pdf_hits else "failed",
            "pdf": str(pdf),
            "pdf_kb": round(pdf.stat().st_size / 1024, 1),
            "pdf_pages": page_count,
            "ingested": totals,
            "parsers_observed": parsers,
            "retrieval_probes": results,
            "pdf_grounded_probes": pdf_hits,
            "note": (
                "Wiring check: a real PDF attachment was routed by metadata[source_file] "
                "into StructureParser.parse_file and reached the OpenSearch index. "
                "Parser evidence records docling when its layout-model snapshot is "
                "cached, else the pypdf plain-text fallback (offline-safe). "
                "Deterministic embeddings are used here; retrieval scoring with the "
                "pinned BGE models is the evaluation harness's job."
            ),
        }
        REPORT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2))
        if not pdf_hits:
            raise SystemExit("no retrieval probe grounded in the PDF; see report")
    finally:
        try:
            await index.drop_tenant_indices(TENANT)
        finally:
            await client.close()


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
