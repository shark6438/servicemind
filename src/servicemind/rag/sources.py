from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd

from servicemind.domain.knowledge import (
    AuthorityLevel,
    CorpusScope,
    KnowledgeACL,
    KnowledgeDocument,
    KnowledgeProvenance,
)
from servicemind.integrations.glpi.client import GlpiClient
from servicemind.integrations.glpi.resolver import resolve_glpi_config
from servicemind.security.auth import TenantContext


def make_document(
    *,
    title: str,
    content: str,
    document_type: str,
    source: str,
    source_version: str,
    source_uri: str,
    source_record_id: str,
    license_name: str,
    authority: AuthorityLevel,
    acl: KnowledgeACL,
    language: str = "en",
    metadata: dict[str, Any] | None = None,
) -> KnowledgeDocument:
    content = content.strip()
    return KnowledgeDocument(
        title=title,
        content=content,
        document_type=document_type,
        language=language,
        metadata=metadata or {},
        acl=acl,
        provenance=KnowledgeProvenance(
            source=source,
            source_version=source_version,
            source_uri=source_uri,
            source_record_id=source_record_id,
            license=license_name,
            authority_level=authority,
            content_hash=KnowledgeDocument.content_digest(content),
        ),
    )


class InternalRunbookSource:
    def __init__(self, tenant_id, runbooks: Iterable[Any]) -> None:
        self.tenant_id, self.runbooks = tenant_id, tuple(runbooks)

    async def load(self) -> list[KnowledgeDocument]:
        return [
            make_document(
                title=x.title,
                content=f"# {x.title}\n\n{x.content}",
                document_type="internal_runbook",
                source="servicemind_internal_runbook",
                source_version="phase4-v1",
                source_uri=f"runbook://{x.identifier}",
                source_record_id=x.identifier,
                license_name="project-owned",
                authority=AuthorityLevel.INTERNAL_KNOWLEDGE,
                acl=KnowledgeACL(corpus_scope=CorpusScope.TENANT, tenant_id=self.tenant_id),
                metadata={"tags": list(x.tags)},
            )
            for x in self.runbooks
        ]


class PagerDutyMarkdownSource:
    def __init__(self, root: Path, revision: str) -> None:
        self.root, self.revision = root, revision

    async def load(self) -> list[KnowledgeDocument]:
        result = []
        for path in sorted((self.root / "docs").rglob("*.md")):
            content = path.read_text(encoding="utf-8")
            rel = path.relative_to(self.root).as_posix()
            title = next(
                (line[2:].strip() for line in content.splitlines() if line.startswith("# ")),
                path.stem,
            )
            result.append(
                make_document(
                    title=title,
                    content=content,
                    document_type="external_incident_response_guide",
                    source="pagerduty_incident_response_docs",
                    source_version=self.revision,
                    source_uri=f"https://github.com/PagerDuty/incident-response-docs/blob/{self.revision}/{rel}",
                    source_record_id=rel,
                    license_name="Apache-2.0",
                    authority=AuthorityLevel.EXTERNAL_BEST_PRACTICE,
                    acl=KnowledgeACL(corpus_scope=CorpusScope.GLOBAL_LICENSED),
                )
            )
        return result


class MendeleyHistoricalCaseSource:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def load(self) -> list[KnowledgeDocument]:
        issues = pd.read_csv(self.root / "issues.csv", low_memory=False).set_index("id", drop=False)
        utterances = pd.read_csv(self.root / "sample_utterances.csv", low_memory=False)
        result = []
        for issue_id, msgs in utterances.groupby("issueid", sort=True):
            if issue_id not in issues.index:
                continue
            numeric_id = int(float(str(issue_id)))
            x = issues.loc[issue_id]
            msgs = msgs.sort_values(["comment_seq", "utr_seq"])
            conversation = "\n".join(
                f"- [{r.author_role}] {str(r.actionbody).strip()}"
                for r in msgs.itertuples()
                if str(r.actionbody).strip()
            )
            content = (
                f"# Historical help desk case {numeric_id}\n\nProject: {x.issue_proj}\nType: {x.issue_type}\n"
                f"Priority: {x.issue_priority}\nResolution status: {x.issue_resolution}\nFinal status: {x.issue_status}\n\n"
                f"## Conversation\n\n{conversation}"
            )
            result.append(
                make_document(
                    title=f"Historical help desk case {numeric_id}",
                    content=content,
                    document_type="public_historical_case",
                    source="mendeley_help_desk_tickets",
                    source_version="3",
                    source_uri="https://doi.org/10.17632/btm76zndnt.3",
                    source_record_id=str(numeric_id),
                    license_name="CC-BY-4.0",
                    authority=AuthorityLevel.PUBLIC_HISTORICAL,
                    acl=KnowledgeACL(corpus_scope=CorpusScope.GLOBAL_LICENSED),
                    metadata={"utterance_count": len(msgs)},
                )
            )
        return result


class GlpiKnowledgeBaseSource:
    def __init__(self, context: TenantContext) -> None:
        self.context = context

    async def load(self) -> list[KnowledgeDocument]:
        config = await resolve_glpi_config(self.context)
        async with GlpiClient(config) as client:
            items = await client.list_knowledge_items()
        result = []
        for x in items:
            p = x.to_agent_payload()
            begin = _date(p["begin_date"]) or datetime.now(UTC)
            end = _date(p["end_date"])
            result.append(
                make_document(
                    title=x.name,
                    content=x.answer,
                    document_type="glpi_knowledge_base",
                    source="glpi_knowledge_base",
                    source_version="v2.3",
                    source_uri=f"glpi://knowbase/{x.id}",
                    source_record_id=str(x.id),
                    license_name="tenant-owned",
                    authority=AuthorityLevel.INTERNAL_KNOWLEDGE,
                    acl=KnowledgeACL(
                        corpus_scope=CorpusScope.TENANT,
                        tenant_id=self.context.tenant_id,
                        entity_ids=frozenset(cast(list[int], p["entity_ids"])),
                        group_ids=frozenset(cast(list[int], p["group_ids"])),
                        profile_ids=frozenset(cast(list[int], p["profile_ids"])),
                        user_ids=frozenset(cast(list[str], p["user_ids"])),
                        effective_from=begin,
                        effective_to=end,
                    ),
                    metadata={"is_faq": x.is_faq},
                )
            )
        return result


def _date(value: object) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
