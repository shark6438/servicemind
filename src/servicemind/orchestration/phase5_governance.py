from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from core import settings
from servicemind.context.builder import ContextBuilder
from servicemind.context.contracts import (
    CONTEXT_ITEM_CONTENT_MAX,
    ContextAgent,
    ContextAssemblyError,
    ContextEnvelope,
    ContextItem,
    ContextSource,
    TrustLabel,
)
from servicemind.context.repository import ContextArtifactSink, PostgresContextArtifactSink
from servicemind.domain.evidence import (
    CITATION_KEY,
    Evidence,
    EvidenceSourceType,
    JoinedEvidence,
)
from servicemind.domain.knowledge import AuthorityLevel
from servicemind.domain.task import Task
from servicemind.memory.contracts import (
    CROSS_TICKET_PROCEDURE_POLICY,
    POST_RUN_MEMORY_WRITER,
    POST_RUN_PROCEDURAL_WRITER,
    POST_RUN_TENANT_EPISODE_POLICY,
    MemoryCandidate,
    MemoryEvidenceRef,
    MemoryPatternQuery,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemoryScopeType,
    MemoryStatus,
    MemoryType,
    normalize_memory_content,
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

logger = logging.getLogger("servicemind.orchestration.phase5_governance")


#: Evidence metadata that describes how the fact was *found* rather than what the
#: fact *is*. None of it may reach the model. On a re-retrieval round the query is
#: built from the Reviewer's own feedback, so serializing it into the evidence made
#: the model read its previous critique back as its own grounding passage -- the
#: analysis was then rejected for failing to match text that had originated in the
#: review, which re-retrieval could never resolve. Providers no longer emit these
#: keys (see ``EnterpriseRAG.to_evidence`` and ``to_graph_evidence``); this filter is
#: the boundary guarantee, so a future provider cannot reintroduce the loop.
_QUERY_DERIVED_METADATA = frozenset({"query", "model_query", "rewritten_queries"})


def _model_visible_evidence(evidence: Evidence) -> str:
    """Serialize evidence for the model, without retrieval bookkeeping."""
    payload = evidence.model_dump(mode="json")
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in _QUERY_DERIVED_METADATA:
            metadata.pop(key, None)
    return json.dumps(payload, ensure_ascii=False)


def _citation_check_evidence(evidence: Evidence) -> str:
    """Serialize evidence the way the Reviewer's judge reads it: id, source, content.

    The Reviewer's model call is a citation check -- "does the row this claim cites
    actually state the claim" -- and ``ReviewerAgent._semantic_node`` already hands it
    exactly these fields when there is no governed envelope. The envelope path passed
    the full row instead, and the two differ by a factor of three: on the ticket-17
    incident of 2026-09-23 the nine cited rows were 5990 tokens with their provenance,
    metadata, tenant id and timestamps and 1985 without them. That overhead is what the
    packer then had to fit, and what it dropped rows to fit -- including the rows under
    review. It is also information no citation check can use: ``tenant_id`` is the
    envelope's own tenant, and the retrieval bookkeeping is checked deterministically
    against the ``Evidence`` objects, not against prose the model reads back.

    The Analysis role keeps ``_model_visible_evidence``. It reasons over the whole row
    rather than verifying a citation, and its view is the one the operator reads back
    from the trace, so it is not narrowed to serve a different role's budget.
    """
    return json.dumps(
        {
            "evidence_id": evidence.evidence_id,
            "source_type": evidence.source_type.value,
            "source_ref": evidence.source_ref,
            "content": evidence.content,
            "content_hash": evidence.provenance.content_hash,
            "citation": (
                evidence.metadata.get(CITATION_KEY)
                if evidence.source_type is EvidenceSourceType.KNOWLEDGE
                else None
            ),
        },
        ensure_ascii=False,
    )


def _model_visible_memory(record: MemoryRecord) -> str:
    """Serialize a memory as a row, the way evidence is serialized.

    Both channels carry text the platform did not author; they now arrive in the same
    shape, so a reader can compare what they are instead of inferring it from a string.
    """
    return json.dumps(record.model_payload(), ensure_ascii=False)


def _cited_evidence_ids(analysis: Any) -> frozenset[str]:
    """Every evidence id the analysis cites, from every place it may cite one.

    The Reviewer's whole job is to check each claim against the evidence it cites, so
    the Reviewer's envelope has to contain that evidence. It is assembled by a budget
    packer that ranks every item by authority, and evidence the analysis never mentions
    ranks just as high -- so on a run with a large analysis the packer prunes cited rows
    to make room for uncited ones. What the model then does is exactly what it was told
    to: the semantic judge is handed ``governed_context``, the analysis cites an id that
    is not in it, and the judge reports the claim as unsupported. That verdict is a
    property of the delivery decision, not of the analysis, and it is not reproducible:
    the same pruning passed review on the first pass of the same run and failed on the
    second (ACC-18 / ACC-23, 2026-09-23), and each false rejection sends the run into a
    replan / retrieve_more storm until the budget is gone.

    The union is taken over the top-level refs, the per-claim refs and the proposed
    actions' refs. ``AnalysisResult`` already constrains claims to a subset of the
    declared refs, but the actions are not constrained, and reading all three costs
    nothing and keeps this honest if that changes.
    """
    if not isinstance(analysis, Mapping):
        return frozenset()
    groups: list[Any] = [
        analysis.get("evidence_refs"),
        *[
            claim.get("evidence_refs")
            for claim in analysis.get("claims") or ()
            if isinstance(claim, Mapping)
        ],
        *[
            action.get("evidence_refs")
            for action in analysis.get("proposed_actions") or ()
            if isinstance(action, Mapping)
        ],
    ]
    return frozenset(ref for group in groups for ref in (group or ()) if isinstance(ref, str))


def _procedure_pattern(analysis: dict[str, Any]) -> tuple[str, str] | None:
    """Build a conservative cross-ticket identity and reviewable procedure body.

    Two returns, and the difference between them is the point: the key decides that
    two tickets describe the same recurring condition, so it may not move with the
    evidence any one ticket happened to hold; the body is what the reviewer reads and
    what the activated procedure carries, so it must.

    The **identity** is ``classification``, ``recommended_group`` and *which*
    recommendation fields the analysis filled -- never their wording. Wording is the
    part that varies with evidence. Measured on ACC-12b (2026-09-23): two structurally
    isomorphic tickets agreed verbatim on both of the first two fields, and the model's
    ``problem_recommendation`` differed in *polarity* -- the first said no problem
    record was warranted, the second said one was -- because the second ticket by
    construction saw one more sibling incident. No normalisation stabilises that, and
    the earlier identity, which hashed the recommendation prose itself, asked it to.
    The key exists to let the second ticket be recognised as the same root cause, and
    it refused on exactly the pair it was built for. Recorded as defect D15.

    ``recommended_group`` stays in the identity and is what keeps this honest: it is
    the team the procedure tells to run it, so merging two tickets that disagree on it
    would hand a procedure to the wrong team. Ditto ``classification``.

    The **body** carries the recommendation prose, which is the actionable content, so
    it is not discarded -- only kept out of the identity. It is therefore one ticket's
    phrasing rather than a consensus of the supporting pair, which is what the review
    gate is for: the body is written to quarantine and a human activates it.

    Ticket ids and action arguments are never *read*: the function takes the three
    named fields and ignores everything else in the analysis, so the ticket's own
    identifier cannot reach the identity. Free-form recommendation prose is quoted as
    written, so a model that names an incident inside a recommendation is relayed
    rather than filtered -- that is a review-time concern, not an identity one.

    Recurrence is deliberately *not* an input. It is the conclusion corroboration
    establishes, so requiring ``recurring_incident`` here inverted the dependency and
    the feature could not start: the key is what lets the second ticket be recognised
    as the same root cause, and until that happens there is nothing stating an
    incident recurs. The skill the analysis agent is given says as much -- identify a
    recurring pattern "only when at least two comparable, verified incidents support
    it" -- so on the first ticket the honest value of the flag is False, and the gate
    therefore refused to form the key on exactly the ticket that had to form it. The
    flag is not discarded with the gate: it still raises a matching episode's
    importance below, where it is a statement about the incident rather than a
    precondition for recognising one.
    """
    names = ("classification", "recommended_group")
    if any(not isinstance(analysis.get(name), str) or not analysis[name].strip() for name in names):
        return None
    recommendation_names = ("problem_recommendation", "change_recommendation")
    recommendations = {
        name: normalize_memory_content(value)
        for name in recommendation_names
        if isinstance((value := analysis.get(name)), str) and value.strip()
    }
    if not recommendations:
        return None
    identity = {
        "classification": normalize_memory_content(analysis["classification"]),
        "recommended_group": normalize_memory_content(analysis["recommended_group"]),
        # The shape, not the prose: "this class of ticket warrants a problem record and
        # a change" is a durable statement about the condition; the sentence that says
        # it is a statement about this ticket's evidence.
        "recommendation_fields": sorted(recommendations),
    }
    encoded_identity = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(encoded_identity) > 8000:
        return None
    body = json.dumps(
        {**identity, **recommendations}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(body) > 8000:
        return None
    return hashlib.sha256(encoded_identity.encode()).hexdigest(), body


#: Withdraw one stored knowledge document from every future retrieval, by tenant and by
#: the source record the citation names. The governance layer states the consequence it
#: needs; the RAG subsystem supplies it. Same shape as ``memory_repository_factory`` --
#: the adapter names a capability, it does not reach into a subsystem.
KnowledgeWithdrawal = Callable[[UUID, str], Awaitable[None]]


async def withdraw_knowledge_document(tenant_id: UUID, source_record_id: str) -> None:
    """Suspend a knowledge document in the live index: PostgreSQL first, then search.

    ``is_active`` is ACL, not a soft flag -- the repository row is the authority and the
    search projection is patched to match, so the pre-filter stops serving the document
    on the next retrieval rather than on the next re-ingest. It is reversible through
    the same call, which matters because the tripwire that reaches here matches on
    substrings and cannot tell a document that *attempts* an injection from one that
    quotes it as an example.
    """
    if not settings.SERVICEMIND_RAG_ENABLED:
        return
    # Resolved here rather than at import time, the way ``_skills`` defers the skill
    # registry: a deployment that never catches an injection never builds the RAG
    # subsystem, and the import graph keeps orchestration from depending on agents.
    from servicemind.agents.knowledge import knowledge_agent

    await knowledge_agent.rag.set_document_active(tenant_id, source_record_id, is_active=False)


class Phase5Governance:
    """Workflow adapter that keeps Phase 5 services outside domain agents."""

    def __init__(
        self,
        *,
        context_sink: ContextArtifactSink | None = None,
        memory_repository_factory: Callable[[UUID], MemoryRepository] | None = None,
        memory_embedding: MemoryEmbeddingProvider | None = None,
        knowledge_withdrawal: KnowledgeWithdrawal | None = None,
    ) -> None:
        self.context_builder = ContextBuilder()
        self.context_sink = context_sink or PostgresContextArtifactSink()
        self.memory_repository_factory = memory_repository_factory or PostgresMemoryRepository
        self.memory_embedding = memory_embedding
        self.knowledge_withdrawal = knowledge_withdrawal or withdraw_knowledge_document
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

    async def _withdraw(self, tenant_id: UUID, source_record_id: str, evidence_id: str) -> None:
        """Suspend a document the tripwire caught, and never fail the run over it.

        The row is already out of this run's envelope -- the taint saw to that -- so a
        withdrawal that raises would kill a run that is already safe. It is logged
        instead, at error level with the traceback: a withdrawal that silently did not
        happen leaves a poisoned document being retrieved and re-detected on every
        future run, and nothing on the run's own record would say so.
        """
        try:
            await self.knowledge_withdrawal(tenant_id, source_record_id)
        except Exception:
            logger.exception(
                "prompt injection in %s could not be withdrawn from the corpus (document %s)",
                evidence_id,
                source_record_id,
            )

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
        # No role is given an ``output-schema`` item, and that is not a saving made by
        # dropping a control. Both governed roles already carry the schema of the *only*
        # schema they are ever asked to emit, in the system prompt, built in the same
        # process from the same call, as the last thing the model reads before the input:
        # ``agents/analysis.py`` ends with ``json.dumps(AnalysisResult.model_json_schema())``
        # and ``agents/reviewer.py`` ends with ``json.dumps(SemanticReview.model_json_schema())``.
        # The envelope copies restated what had been said one message earlier -- 2985 tokens
        # for the Analyst (28% of a 10720-token envelope) and 1586 for the Reviewer (15%).
        #
        # The Reviewer's copy was worse than duplicated: it carried ``ReviewResult``'s
        # schema, and ``ReviewResult`` is never requested from a model at all. The
        # reviewer's one structured call asks for ``SemanticReview`` (``reviewer.py:632``)
        # and the result it returns is assembled in Python (``reviewer.py:259``). So the
        # item spent 15% of the reviewer's envelope describing a shape no model is asked
        # to produce, while the shape it is asked for was already in the prompt.
        #
        # Measured on ACC-03 (2026-09-23): the Analyst's copy, together with an evidence cap
        # charged in rank order, left the runbook the case turns on pruned while 229 tokens
        # of the envelope sat unused, and the Analyst recorded in ``unresolved_questions``
        # that no cited evidence stated a mechanism -- because the document that states one
        # had been cut for room it was not even using. Measured on ACC-06 (2026-09-23): with
        # the Analyst's copy gone the analysis grew to the room it had been given, and the
        # reviewer -- whose envelope must hold that analysis *and* every row it cites, both
        # required -- ran out of budget and failed the run with
        # ``required_item_exceeds_token_budget``. Two roles paying for the same mistake is
        # how that mistake gets found; it is fixed once, here, for both.
        items.extend(self._state_items(state, agent, allowed_agents, run_id))
        joined_payload = state.get("joined_evidence") or {}
        if agent in {ContextAgent.ANALYSIS, ContextAgent.REVIEWER} and joined_payload:
            joined = JoinedEvidence.model_validate(joined_payload)
            # The evidence the analysis cites is the evidence being reviewed: the
            # Reviewer's envelope must carry it or the review has nothing to check
            # against. See ``_cited_evidence_ids``. The Analysis role is deliberately left
            # alone -- it cites from what it was shown, so pinning its own output back
            # into its input would beg the question rather than answer it.
            cited = (
                _cited_evidence_ids(state.get("analysis_result"))
                if agent is ContextAgent.REVIEWER
                else frozenset()
            )
            # The Reviewer is shown what the Analyst's envelope delivered -- not the
            # whole joined set. Both roles read the same retrieval, but the Analyst
            # reads it through a budget packer that prunes, and this branch used to hand
            # the Reviewer the pre-pruning set: the evidence the Reviewer checked was
            # then a strict superset of the evidence the Analyst wrote from. The verdict
            # that produces is not a property of the analysis. Measured on ACC-03
            # (2026-09-23, run 96435a80): the Analyst's envelope selected 9 evidence ids
            # and pruned 2 -- ``ev-f380cb32f4a5b880`` (the ``KB-GLOBEX-VPN-MFA-REBIND``
            # runbook chunk) and ``ev-ff24bc453152bf9d`` -- while the Reviewer's selected
            # all 11, the union of the two. A root-cause claim is now checked against the
            # material its author was given, which is the only reading under which
            # "unsupported" means something about the analysis.
            #
            # ``analysis_evidence_ids`` is written by the analysis node from the envelope
            # it just built (``supervisor_workflow.analysis_node``). It is absent when
            # no envelope was built at all -- a stub Analyst reads the joined set
            # directly, and then the Reviewer reads the same set, so absent means "no
            # delivery decision to mirror" rather than "no evidence". Cited ids are
            # unioned in for the same reason ``required`` is set from them below: a
            # citation the Reviewer cannot resolve is a delivery fault, not a finding,
            # and it sends the run into a replan storm rather than reporting a verdict.
            delivered: frozenset[str] | None = None
            if agent is ContextAgent.REVIEWER:
                recorded = state.get("analysis_evidence_ids")
                if recorded is not None:
                    delivered = frozenset(recorded) | cited
            poisoned: dict[str, str] = {}
            for evidence in joined.items:
                if delivered is not None and evidence.evidence_id not in delivered:
                    continue
                taints = evidence.taints()
                if "prompt_injection" in taints:
                    # Enforcement is in-process and immediate: the taint is one of
                    # ``BLOCKED_TAINTS``, so the builder rejects this row with
                    # ``unresolved_taint`` and the analysis model never reads it. The
                    # withdrawal below is the slower half -- it stops tomorrow's run
                    # retrieving the document at all -- and the builder is what makes
                    # today's run safe, so it runs whether or not the withdrawal does.
                    record = evidence.source_record_id
                    if record is not None:
                        poisoned.setdefault(record, evidence.evidence_id)
                items.append(
                    ContextItem(
                        # The item id *is* the evidence id. The model echoes whichever id it
                        # is shown when it cites a claim, and every validator
                        # (AnalysisAgent._quality, ReviewerAgent,
                        # JoinedEvidence.evidence_refs) resolves citations in the bare
                        # ``ev-...`` namespace. A namespaced id here made the model cite
                        # something the validators could never resolve, so every analysis
                        # came back ANALYSIS_GROUNDING_FAILED and every review saw unknown
                        # references.
                        item_id=evidence.evidence_id,
                        source=ContextSource.EVIDENCE,
                        content=(
                            _citation_check_evidence(evidence)
                            if agent is ContextAgent.REVIEWER
                            else _model_visible_evidence(evidence)
                        ),
                        allowed_agents=allowed_agents,
                        # Not ``VERIFIED``. This row is retrieved text from a corpus the
                        # platform did not author -- a runbook, a public post, a ticket
                        # body, a Graph projection -- and the item said so out of the other
                        # side of its mouth already, carrying ``untrusted_content`` as a
                        # taint. One item cannot be both verified and untrusted, and the
                        # label is what tells the model these are facts to reason over
                        # rather than instructions to follow.
                        trust=TrustLabel.UNTRUSTED,
                        # The source's own ``AuthorityLevel``, rescaled to the envelope's
                        # 0..1. Every case used to be a constant 0.95 here, which is the
                        # number the packer sorts on -- so the level each source declares,
                        # stores and reads back decided nothing, and under a budget the
                        # surviving evidence was chosen by the tiebreak, an evidence id.
                        authority=evidence.authority_level / 100,
                        relevance=evidence.confidence if evidence.confidence is not None else 0.5,
                        provenance_ref=evidence.source_ref,
                        taint_labels=taints,
                        occurred_at=evidence.provenance.retrieved_at,
                        # Required only when the analysis cites it -- see ``cited`` above.
                        # A cited row that cannot fit now raises
                        # ``required_item_exceeds_token_budget`` and the run terminates as
                        # ``context_assembly_failure``. That is the honest outcome: the
                        # Reviewer cannot verify a citation it was not shown, and letting
                        # the packer drop it does not avoid the failure, it renames it
                        # after the analysis instead.
                        required=evidence.evidence_id in cited,
                    )
                )
            if poisoned and agent is ContextAgent.ANALYSIS:
                # Once per run, on the first role that reads evidence. The reviewer is
                # handed the same joined set in the same run, so withdrawing here covers
                # both; ``set_document_active`` and the repository write behind it are
                # idempotent, and doing it twice would only double the round trips.
                for record, evidence_id in poisoned.items():
                    await self._withdraw(tenant_id, record, evidence_id)
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
                        # The row, not the bare string: it carries ``created_by``,
                        # ``memory_type`` and ``provenance``, which is how a reader
                        # tells a verified outcome of a past run from a curated
                        # document -- and how it tells that the prose inside an
                        # episode was written by the run that resolved it.
                        content=_model_visible_memory(selection.memory),
                        allowed_agents=allowed_agents,
                        # Kept VERIFIED, unlike evidence: this passage did not merely
                        # get retrieved, it passed the deterministic write policy, cites
                        # only verified evidence, and was reviewed before it was stored.
                        trust=TrustLabel.VERIFIED,
                        # A case resolved by an earlier run, which is the level the
                        # memory write path already reasoned with -- quoted here rather
                        # than restated, so the two cannot drift.
                        authority=AuthorityLevel.TENANT_RESOLVED_CASE / 100,
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
        evidence_cap = settings.SERVICEMIND_CONTEXT_EVIDENCE_TOKEN_CAP
        envelope = self.context_builder.build(
            tenant_id=tenant_id,
            run_id=run_id,
            task_id=task.task_id,
            agent=agent,
            items=items,
            max_input_tokens=settings.SERVICEMIND_CONTEXT_MAX_INPUT_TOKENS,
            source_token_caps=(
                {ContextSource.EVIDENCE: evidence_cap}
                if evidence_cap is not None and agent is ContextAgent.ANALYSIS
                else None
            ),
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
        content = json.dumps(values, ensure_ascii=False)
        if len(content) > CONTEXT_ITEM_CONTENT_MAX:
            # The state item carries derived model output -- the analysis the reviewer
            # must judge, the review the action agent must honour -- so its size is not
            # something this module decides. Clipping it is not an option either: the
            # reviewer's entire model input is this item, and half a JSON document is
            # not a smaller analysis, it is an unparseable one. Name the cause and let
            # the caller terminate the run honestly.
            raise ContextAssemblyError(
                "state_item_exceeds_item_ceiling",
                f"state context item is {len(content)} characters, over the "
                f"{CONTEXT_ITEM_CONTENT_MAX}-character item ceiling",
            )
        return [
            ContextItem(
                item_id="state",
                source=ContextSource.STATE,
                content=content,
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
            pattern = _procedure_pattern(analysis)
            return [
                MemoryCandidate(
                    tenant_id=tenant_id,
                    # Tenant scope, not the requester's scope. A ticket outcome is
                    # evidence about the tenant's estate, and it is the scope a
                    # procedure derived from it must be able to cite: the activation
                    # guard only accepts a supporting episode that shares the
                    # procedure's scope or is tenant-wide, so a USER-scoped episode
                    # could never support a tenant-scoped procedure.
                    #
                    # This is deliberately narrower than the pre-Phase 5.1 widening:
                    # the record carries required_entity_ids / required_group_ids, so
                    # it stays filtered to the requester's GLPI entities and groups,
                    # and post_run_scope tells the read path the choice was made here
                    # rather than inherited from an unreviewed migration.
                    scope=MemoryScope(scope_type=MemoryScopeType.TENANT),
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
                        "post_run_scope": POST_RUN_TENANT_EPISODE_POLICY,
                        "source_ticket_id": state["ticket_id"],
                        **({"procedure_pattern_key": pattern[0]} if pattern else {}),
                        "required_entity_ids": sorted(state.get("allowed_glpi_entity_ids") or []),
                        "required_group_ids": sorted(state.get("group_ids") or []),
                    },
                    created_by=POST_RUN_MEMORY_WRITER,
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
        if not settings.SERVICEMIND_MEMORY_PROCEDURAL_PROPOSALS_ENABLED:
            return len(records)
        pattern = _procedure_pattern(result.get("analysis") or {})
        # QUARANTINE is admitted alongside ACTIVE, and the gate must agree with the
        # query it guards: ``pattern_episodes`` goes through
        # ``MemoryPatternQuery.allows_record``, which calls ``corroborable_at``. If the
        # two disagree, one of them decides and the other is decoration -- and the
        # earlier attempt at this fix is the proof. It dropped the ACTIVE requirement
        # here alone, on the grounds that quarantine is the normal resting state of a
        # post-run episode, and the corroboration still never fired: the two episodes
        # it went looking for were quarantined and therefore invisible to the query.
        # Both sides have now moved together, which is why this one works.
        #
        # Admitting quarantine does not widen what a model may see. The episode is a
        # *supporting* record here, not served content; the procedure derived from it
        # is written to quarantine below and needs its own human activation, which
        # revalidates the same support chain through ``_procedural_support_failure``.
        if pattern is None or not any(
            record.memory_type is MemoryType.EPISODIC
            and record.status in {MemoryStatus.ACTIVE, MemoryStatus.QUARANTINE}
            and record.provenance.get("procedure_pattern_key") == pattern[0]
            for record in records
        ):
            return len(records)

        repository = self.memory_repository_factory(tenant_id)
        episodes = await repository.pattern_episodes(
            MemoryPatternQuery(
                tenant_id=tenant_id,
                pattern_key=pattern[0],
                entity_ids=frozenset(state.get("allowed_glpi_entity_ids") or ()),
                group_ids=frozenset(state.get("group_ids") or ()),
            )
        )
        supporting: list[MemoryRecord] = []
        ticket_ids: set[str] = set()
        run_ids: set[UUID] = set()
        for episode in episodes:
            ticket_id = episode.provenance.get("source_ticket_id")
            if ticket_id is None or episode.source_run_id is None:
                continue
            normalized_ticket_id = str(ticket_id)
            if normalized_ticket_id in ticket_ids or episode.source_run_id in run_ids:
                continue
            supporting.append(episode)
            ticket_ids.add(normalized_ticket_id)
            run_ids.add(episode.source_run_id)
            if len(supporting) == 2:
                break
        if len(supporting) < 2:
            return len(records)

        evidence_by_identity = {
            (ref.evidence_id, ref.source_ref, ref.content_hash): ref
            for episode in supporting
            for ref in episode.evidence_refs
        }
        required_entities = sorted(
            {
                value
                for episode in supporting
                for value in episode.provenance.get("required_entity_ids", [])
            }
        )
        required_groups = sorted(
            {
                value
                for episode in supporting
                for value in episode.provenance.get("required_group_ids", [])
            }
        )
        procedure = MemoryCandidate(
            tenant_id=tenant_id,
            scope=MemoryScope(scope_type=MemoryScopeType.TENANT),
            memory_type=MemoryType.PROCEDURAL,
            subject_key=f"procedure:cross-ticket:{pattern[0]}",
            content=pattern[1],
            source_run_id=run_id,
            source_trace_id=state.get("thread_id") or state["run_id"],
            evidence_refs=tuple(evidence_by_identity[key] for key in sorted(evidence_by_identity)),
            supporting_episode_ids=tuple(episode.memory_id for episode in supporting),
            confidence=min(episode.confidence for episode in supporting),
            importance=0.9,
            expires_at=datetime.now(UTC) + timedelta(days=180),
            provenance={
                "derivation_policy": CROSS_TICKET_PROCEDURE_POLICY,
                "procedure_pattern_key": pattern[0],
                "source_ticket_ids": sorted(ticket_ids),
                "supporting_episode_count": len(supporting),
                "required_entity_ids": required_entities,
                "required_group_ids": required_groups,
            },
            created_by=POST_RUN_PROCEDURAL_WRITER,
        )
        proposed = await MemoryWriter(repository, MemoryGovernancePolicy()).write(procedure)
        return len(records) + (1 if proposed is not None else 0)


phase5_governance = Phase5Governance()
