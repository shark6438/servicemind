from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from core import settings
from servicemind.context.builder import ContextBuilder
from servicemind.context.contracts import (
    ContextAgent,
    ContextEnvelope,
    ContextItem,
    ContextSource,
    TrustLabel,
)
from servicemind.context.repository import ContextArtifactSink, PostgresContextArtifactSink
from servicemind.domain.analysis import AnalysisResult
from servicemind.domain.evidence import JoinedEvidence
from servicemind.domain.review import ReviewResult
from servicemind.domain.task import Task
from servicemind.memory.contracts import (
    MemoryCandidate,
    MemoryEvidenceRef,
    MemoryQuery,
    MemoryScope,
    MemoryScopeType,
    MemoryType,
)
from servicemind.memory.policy import MemoryGovernancePolicy
from servicemind.memory.repository import MemoryRepository, PostgresMemoryRepository
from servicemind.memory.service import (
    CachedMemoryEmbeddingProvider,
    MemoryEmbeddingProvider,
    MemoryRetriever,
    MemoryWriter,
    PostRunMemoryMiddleware,
)
from servicemind.rag.models import TeiEmbeddingProvider
from servicemind.runtime.contracts import AgentInvocationContext
from servicemind.skills.registry import SkillRegistry


class Phase5Governance:
    """Workflow adapter that keeps Phase 5 services outside domain agents."""

    def __init__(
        self,
        *,
        context_sink: ContextArtifactSink | None = None,
        memory_repository_factory: Callable[[UUID], MemoryRepository] | None = None,
        memory_embedding: MemoryEmbeddingProvider | None = None,
    ) -> None:
        self.context_builder = ContextBuilder()
        self.context_sink = context_sink or PostgresContextArtifactSink()
        self.memory_repository_factory = memory_repository_factory or PostgresMemoryRepository
        self.memory_embedding = memory_embedding
        if (
            self.memory_embedding is None
            and settings.SERVICEMIND_MEMORY_VECTOR_ENABLED
            and settings.SERVICEMIND_EMBEDDING_URL
        ):
            self.memory_embedding = CachedMemoryEmbeddingProvider(
                TeiEmbeddingProvider(
                    settings.SERVICEMIND_EMBEDDING_URL,
                    model_revision=settings.SERVICEMIND_EMBEDDING_REVISION,
                )
            )
        self._skill_registry: SkillRegistry | None = None

    def _skills(self) -> SkillRegistry:
        if self._skill_registry is None:
            root = Path(settings.SERVICEMIND_SKILLS_DIR)
            if not root.is_absolute():
                root = Path(__file__).resolve().parents[3] / root
            registry = SkillRegistry(root)
            registry.load()
            self._skill_registry = registry
        return self._skill_registry

    async def build_fast_knowledge_context(
        self, *, state: dict[str, Any]
    ) -> ContextEnvelope | None:
        """Apply the same model boundary to the plan-free knowledge fast path."""
        if not settings.SERVICEMIND_CONTEXT_ENABLED:
            return None
        tenant_id = UUID(state["tenant_id"])
        run_id = UUID(state["run_id"])
        agent = ContextAgent.KNOWLEDGE
        allowed_agents = frozenset({agent})
        envelope = self.context_builder.build(
            tenant_id=tenant_id,
            run_id=run_id,
            task_id="FAST-KNOWLEDGE",
            agent=agent,
            items=(
                ContextItem(
                    item_id="task",
                    source=ContextSource.TASK,
                    content=json.dumps(
                        {"goal": state["goal"], "ticket_id": state["ticket_id"]},
                        ensure_ascii=False,
                    ),
                    allowed_agents=allowed_agents,
                    trust=TrustLabel.TRUSTED_CONTROL,
                    authority=1,
                    relevance=1,
                    required=True,
                    provenance_ref=f"run://{run_id}/fast-knowledge",
                ),
                ContextItem(
                    item_id="policy",
                    source=ContextSource.POLICY,
                    content=json.dumps(
                        {
                            "rule": "read-only knowledge retrieval",
                            "entity_ids": state.get("allowed_glpi_entity_ids", []),
                            "group_ids": state.get("group_ids", []),
                            "profile_ids": state.get("profile_ids", []),
                        }
                    ),
                    allowed_agents=allowed_agents,
                    trust=TrustLabel.TRUSTED_CONTROL,
                    authority=1,
                    relevance=1,
                    required=True,
                    provenance_ref="policy://servicemind-agent-policy-v2",
                ),
            ),
            max_input_tokens=settings.SERVICEMIND_CONTEXT_MAX_INPUT_TOKENS,
        )
        await self.context_sink.record(envelope)
        return envelope

    async def build_context(
        self,
        *,
        state: dict[str, Any],
        task: Task,
        invocation: AgentInvocationContext,
        agent: ContextAgent,
    ) -> ContextEnvelope | None:
        if not settings.SERVICEMIND_CONTEXT_ENABLED:
            return None
        tenant_id = invocation.tenant_id
        run_id = invocation.run_id
        allowed_agents = frozenset({agent})
        items: list[ContextItem] = [
            ContextItem(
                item_id="task",
                source=ContextSource.TASK,
                content=json.dumps(
                    {
                        "goal": state["goal"],
                        "task_id": task.task_id,
                        "task_type": task.task_type,
                        "objective": task.task_input.get("objective"),
                        "ticket_id": state["ticket_id"],
                        "request_write": state.get("request_write", False),
                    },
                    ensure_ascii=False,
                ),
                allowed_agents=allowed_agents,
                trust=TrustLabel.TRUSTED_CONTROL,
                authority=1,
                relevance=1,
                required=True,
                provenance_ref=f"run://{run_id}/task/{task.task_id}",
            ),
            ContextItem(
                item_id="policy",
                source=ContextSource.POLICY,
                content=json.dumps(
                    {
                        "policy_version": invocation.policy_version,
                        "allowed_capabilities": sorted(invocation.allowed_capabilities),
                        "deadline": invocation.deadline.isoformat(),
                        "model_budget": invocation.max_model_calls,
                        "tool_budget": invocation.max_tool_calls,
                    }
                ),
                allowed_agents=allowed_agents,
                trust=TrustLabel.TRUSTED_CONTROL,
                authority=1,
                relevance=1,
                required=True,
                provenance_ref=f"policy://{invocation.policy_version}",
            ),
        ]
        if agent in {ContextAgent.DATA, ContextAgent.ACTION}:
            items.append(
                ContextItem(
                    item_id="tool-contract",
                    source=ContextSource.TOOL_SCHEMA,
                    content=json.dumps(
                        {
                            "allowed_tools": sorted(invocation.allowed_capabilities),
                            "rule": "Tool arguments remain subject to deterministic policy",
                        }
                    ),
                    allowed_agents=allowed_agents,
                    trust=TrustLabel.TRUSTED_CONTROL,
                    authority=1,
                    relevance=1,
                    required=True,
                    provenance_ref=f"policy://{invocation.policy_version}/tools",
                )
            )
        output_schema = (
            AnalysisResult.model_json_schema()
            if agent is ContextAgent.ANALYSIS
            else ReviewResult.model_json_schema()
            if agent is ContextAgent.REVIEWER
            else None
        )
        if output_schema is not None:
            items.append(
                ContextItem(
                    item_id="output-schema",
                    source=ContextSource.OUTPUT_SCHEMA,
                    content=json.dumps(output_schema, ensure_ascii=False),
                    allowed_agents=allowed_agents,
                    trust=TrustLabel.TRUSTED_CONTROL,
                    authority=1,
                    relevance=1,
                    required=True,
                    provenance_ref="schema://servicemind/phase5",
                )
            )
        items.extend(self._state_items(state, agent, allowed_agents, run_id))
        joined_payload = state.get("joined_evidence") or {}
        if agent in {ContextAgent.ANALYSIS, ContextAgent.REVIEWER} and joined_payload:
            joined = JoinedEvidence.model_validate(joined_payload)
            items.extend(
                ContextItem(
                    item_id=f"evidence:{evidence.evidence_id}",
                    source=ContextSource.EVIDENCE,
                    content=evidence.model_dump_json(),
                    allowed_agents=allowed_agents,
                    trust=TrustLabel.VERIFIED,
                    authority=0.95,
                    relevance=evidence.confidence if evidence.confidence is not None else 0.5,
                    provenance_ref=evidence.source_ref,
                    taint_labels=frozenset({"untrusted_content"}),
                    occurred_at=evidence.provenance.retrieved_at,
                )
                for evidence in joined.items
            )
            # Only ANALYSIS may consume long-term memory: the ContextBuilder
            # allowlist grants MEMORY to ANALYSIS, not REVIEWER (which verifies
            # against task/evidence only). Retrieving for REVIEWER would pay a
            # Postgres query + embedding pass for items the builder rejects.
            if settings.SERVICEMIND_MEMORY_ENABLED and agent is ContextAgent.ANALYSIS:
                memories = await MemoryRetriever(
                    self.memory_repository_factory(tenant_id),
                    embedding=self.memory_embedding,
                    candidate_ceiling=settings.SERVICEMIND_MEMORY_CANDIDATE_CEILING,
                ).retrieve(
                    MemoryQuery(
                        tenant_id=tenant_id,
                        text=state["goal"],
                        user_id=state["user_id"],
                        entity_ids=frozenset(state.get("allowed_glpi_entity_ids") or ()),
                        group_ids=frozenset(state.get("group_ids") or ()),
                    )
                )
                items.extend(
                    ContextItem(
                        item_id=f"memory:{selection.memory.memory_id}",
                        source=ContextSource.MEMORY,
                        content=selection.memory.content,
                        allowed_agents=allowed_agents,
                        trust=TrustLabel.VERIFIED,
                        authority=0.7,
                        relevance=selection.score,
                        provenance_ref=f"memory://{selection.memory.memory_id}",
                        occurred_at=selection.memory.updated_at,
                    )
                    for selection in memories
                )
        if settings.SERVICEMIND_SKILLS_ENABLED and agent in {
            ContextAgent.ANALYSIS,
            ContextAgent.REVIEWER,
        }:
            skills = self._skills().resolve(
                tenant_id=tenant_id,
                agent=agent,
                task_text=state["goal"],
                agent_capabilities=invocation.allowed_capabilities,
                user_permissions=invocation.allowed_capabilities,
                tenant_policy=invocation.allowed_capabilities,
                tool_policy=invocation.allowed_capabilities,
            )
            items.extend(
                ContextItem(
                    item_id=(
                        f"skill:{resolved.skill.manifest.skill_id}@"
                        f"{resolved.skill.manifest.version}"
                    ),
                    source=ContextSource.SKILL,
                    content=resolved.skill.instructions,
                    allowed_agents=allowed_agents,
                    trust=TrustLabel.TRUSTED_CONTROL,
                    authority=0.9,
                    relevance=resolved.match_score,
                    provenance_ref=resolved.skill.source_path,
                )
                for resolved in skills
            )
        envelope = self.context_builder.build(
            tenant_id=tenant_id,
            run_id=run_id,
            task_id=task.task_id,
            agent=agent,
            items=items,
            max_input_tokens=settings.SERVICEMIND_CONTEXT_MAX_INPUT_TOKENS,
        )
        await self.context_sink.record(envelope)
        return envelope

    @staticmethod
    def _state_items(
        state: dict[str, Any],
        agent: ContextAgent,
        allowed_agents: frozenset[ContextAgent],
        run_id: UUID,
    ) -> list[ContextItem]:
        values: dict[str, Any] = {}
        if agent is ContextAgent.DATA:
            values = {
                "ticket_id": state["ticket_id"],
                "entity_ids": state.get("allowed_glpi_entity_ids", []),
            }
        elif agent is ContextAgent.KNOWLEDGE:
            values = {
                "query": state["goal"],
                "entity_ids": state.get("allowed_glpi_entity_ids", []),
                "group_ids": state.get("group_ids", []),
                "profile_ids": state.get("profile_ids", []),
            }
        elif agent is ContextAgent.REVIEWER:
            values = {"analysis": state.get("analysis_result", {})}
        elif agent is ContextAgent.ACTION:
            values = {
                "review": state.get("review_result", {}),
                "handoff": state.get("handoff_envelope", {}),
            }
        if not values:
            return []
        return [
            ContextItem(
                item_id="state",
                source=ContextSource.STATE,
                content=json.dumps(values, ensure_ascii=False),
                allowed_agents=allowed_agents,
                trust=TrustLabel.TRUSTED_CONTROL,
                authority=1,
                relevance=1,
                required=True,
                provenance_ref=f"run://{run_id}/state",
            )
        ]

    async def post_run(
        self,
        *,
        state: dict[str, Any],
        result: dict[str, Any],
        status: str,
    ) -> int:
        if not settings.SERVICEMIND_MEMORY_ENABLED:
            return 0
        if not result.get("final_state_verified") or not state.get("user_id"):
            return 0
        if state.get("request_write") and not (result.get("execution") or {}).get("verified"):
            return 0
        tenant_id = UUID(state["tenant_id"])
        run_id = UUID(state["run_id"])

        async def extract(payload: dict[str, Any]) -> list[MemoryCandidate]:
            analysis = payload.get("analysis") or {}
            review = payload.get("review") or {}
            evidence_payload = payload.get("evidence") or {}
            if review.get("decision") != "passed" or not analysis or not evidence_payload:
                return []
            joined = JoinedEvidence.model_validate(evidence_payload)
            declared = set(analysis.get("evidence_refs") or ())
            evidence_refs = tuple(
                MemoryEvidenceRef(
                    evidence_id=item.evidence_id,
                    source_ref=item.source_ref,
                    content_hash=item.provenance.content_hash,
                    verified=True,
                )
                for item in joined.items
                if item.evidence_id in declared
            )
            if not evidence_refs:
                return []
            content = json.dumps(
                {
                    "ticket_id": state["ticket_id"],
                    "classification": analysis.get("classification"),
                    "priority": analysis.get("priority"),
                    "recommended_group": analysis.get("recommended_group"),
                    "outcome": analysis.get("reasoning_summary"),
                    "execution": payload.get("execution"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            confidence = min(
                float(analysis.get("confidence", 0)), float(review.get("confidence", 0))
            )
            return [
                MemoryCandidate(
                    tenant_id=tenant_id,
                    scope=MemoryScope(scope_type=MemoryScopeType.USER, scope_id=state["user_id"]),
                    memory_type=MemoryType.EPISODIC,
                    subject_key=f"ticket:{state['ticket_id']}:verified_outcome",
                    content=content[:8000],
                    source_run_id=run_id,
                    source_trace_id=state.get("thread_id") or state["run_id"],
                    evidence_refs=evidence_refs,
                    final_state_verified=True,
                    expires_at=datetime.now(UTC) + timedelta(days=90),
                    confidence=confidence,
                    importance=0.8 if analysis.get("recurring_incident") else 0.6,
                    provenance={
                        "review_id": review.get("review_id"),
                        "review_policy": review.get("policy_version"),
                        "official_knowledge_authority": "rag",
                        "required_entity_ids": sorted(state.get("allowed_glpi_entity_ids") or []),
                        "required_group_ids": sorted(state.get("group_ids") or []),
                    },
                    created_by="post-run-memory-middleware",
                )
            ]

        middleware = PostRunMemoryMiddleware(
            writer_factory=lambda selected_tenant: MemoryWriter(
                self.memory_repository_factory(selected_tenant),
                MemoryGovernancePolicy(
                    auto_activation_confidence=(
                        settings.SERVICEMIND_MEMORY_AUTO_ACTIVATION_CONFIDENCE
                    )
                ),
            ),
            extractor=extract,
        )
        records = await middleware.process(
            tenant_id=tenant_id,
            run_id=run_id,
            status=status,
            result=result,
        )
        return len(records)


phase5_governance = Phase5Governance()
