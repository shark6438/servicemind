"""End-to-end verification using DeepSeek query processing and real BGE services."""

import asyncio
import json
import selectors
from uuid import UUID

from servicemind.agents.knowledge import KnowledgeAgent

TENANT = UUID("11111111-1111-4111-8111-111111111111")


async def main() -> None:
    agent = KnowledgeAgent()
    try:
        evidence = await agent.retrieve(
            tenant_id=TENANT,
            user_id="phase4-verifier",
            entity_ids={1},
            query="How should we troubleshoot a VPN MFA authentication failure and verify the identity provider?",
        )
        if not evidence:
            raise RuntimeError("Knowledge Agent returned no evidence")
        for item in evidence:
            citation = item.metadata.get("citation")
            if not citation or not citation.get("content_hash"):
                raise RuntimeError("Knowledge evidence is missing a content-bound citation")
        print(
            json.dumps(
                {
                    "status": "passed",
                    "evidence_count": len(evidence),
                    "top_provider": evidence[0].provenance.provider,
                    "top_source_ref": evidence[0].source_ref,
                    "retrieval_method": evidence[0].provenance.retrieval_method,
                    "citation_id": evidence[0].metadata["citation"]["citation_id"],
                },
                indent=2,
            )
        )
    finally:
        if agent._rag is not None:
            await agent._rag.index.client.close()


if __name__ == "__main__":
    asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
