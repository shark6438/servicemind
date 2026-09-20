"""Memory quality evaluation harness (Phase 5).

The RAG harness answers "did we find the right document?". This one answers the two
questions the memory subsystem is actually governed by, and keeps them apart:

* **Write side** -- does the governance policy put every candidate in the right
  state? A governance decision is not a fuzzy score. The expected action is exact,
  so the report states exact-match accuracy plus per-class precision/recall; there
  is deliberately no single "governance score" to tune against.
* **Read side** -- does the retriever surface what it should, and -- the property
  that actually carries risk -- never surface what it must not?
  ``must_not_surface`` violations are reported as a leak rate, not folded into an
  average, because "3% of the time we showed another tenant's memory" is not a
  quality dip, it is a defect.

Scoring modes
-------------
Ranking metrics depend on the similarity function, so the report carries the mode it
ran in. ``lexical`` (Jaccard over tokens) is the offline/CI mode; an embedding
provider can be injected for the live mode. **Safety metrics are mode-independent by
construction** -- isolation, taint, staleness, injection and temporal-leak probes
assert on membership, not on order -- so they must hold in every mode, and the report
keeps them in a separate block from the ranking numbers.

Honesty boundary
----------------
The scenario corpus in ``evaluation/memory/`` is authored by this project, the same
way the Phase 4 gold set is. It is a regression contract over the guarantees the
design claims -- not evidence about any tenant's real memory distribution, and not a
substitute for replaying production memory traffic. Corpus times are offsets from a
load-time anchor, so the corpus stays stable while the wall-clock stays honest.

Rejected writes carry no record, so a probe can never reference them: the write side
scores them, the read side cannot see them.

A green safety number only counts if the record would otherwise have been served
-------------------------------------------------------------------------------
A ``must_not_surface`` probe is only as good as the argument that the record was
reachable in the first place. A record that the similarity floor drops, that the ACL
excludes, or that the write policy parked in ``quarantine`` produces exactly the same
green result as a filter doing its job -- which is how the v1 probes named after the
taint and injection filters passed without ever reaching them. Two mechanisms close
that hole, and a corpus is expected to use one of them for every safety probe:

* a **matched control** in ``must_surface`` that differs from the forbidden record in
  the one property under test, so the same query proves the wording clears the floor
  and the ACL while only the poison is blocked; or
* a **seed** (see :class:`MemorySeedStep`) that plants the forbidden state directly,
  for properties the write path cannot produce at all.

:class:`RankGate` closes the matching hole on the ranking side: an average cannot go
red for a position regression, a stated per-probe requirement can.

Explicit non-goal: this harness does **not** test historical time travel. The
repository revalidates with ``max(query.at, now)``, so a record revoked after ``at``
is dropped even for a query dated before the revocation. That is the intended
behaviour (``at`` is the instant the query is asked, not a rewind); the ``as_of``
probes assert the property that *is* implemented -- a record whose validity window
has not opened yet, or has already closed, must never surface.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from servicemind.context.builder import ContextBuilder
from servicemind.context.contracts import (
    ContextAgent,
    ContextItem,
    ContextSource,
    TrustLabel,
)
from servicemind.domain.analysis import AnalysisResult
from servicemind.evaluation.metrics import mrr_at_k, ndcg_at_k, recall_at_k
from servicemind.memory.contracts import (
    MemoryCandidate,
    MemoryQuery,
    MemoryStatus,
    MemoryWriteAction,
    MemoryWriteDecision,
)
from servicemind.memory.policy import MemoryGovernancePolicy
from servicemind.memory.repository import InMemoryMemoryRepository
from servicemind.memory.service import MemoryRetriever, MemoryWriter

SCHEMA_VERSION = "phase5-memory-eval-v1"

#: Ranking cut-offs the report slices the returned list at.
RANK_CUTOFFS = (1, 3, 5, 8)

#: Probe categories whose properties hold regardless of the similarity function.
SAFETY_CATEGORIES = ("as_of", "injection", "isolation", "staleness", "taint")

#: Recorded on every seeded record so a stored row says out loud that it did not come
#: from the governance policy.
SEED_REASON_CODE = "SEEDED_BY_EVALUATION_CORPUS"


class MemoryEvaluationError(RuntimeError):
    """The corpus or the harness is inconsistent; evaluation cannot proceed."""


class WriteExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_action: MemoryWriteAction
    #: Every code listed here must be present in the policy's decision. Extra codes
    #: are allowed and reported, because the repository appends codes of its own
    #: (``VERSION_CONFLICT`` and friends) and pinning the exact tuple would make the
    #: corpus break on any unrelated added code.
    policy_reason_codes: tuple[str, ...] = ()
    #: ``active`` | ``quarantine`` | ``rejected`` | ``unchanged``
    stored: str
    #: For ``stored == "unchanged"``: the step id whose record must be returned.
    same_as: str | None = None

    @model_validator(mode="after")
    def validate_stored(self) -> WriteExpectation:
        allowed = {"active", "quarantine", "rejected", "unchanged"}
        if self.stored not in allowed:
            raise ValueError(f"stored must be one of {sorted(allowed)}")
        if (self.stored == "unchanged") != (self.same_as is not None):
            raise ValueError("same_as is required exactly when stored is 'unchanged'")
        return self


class MemoryWriteStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["write"] = "write"
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    #: ``golden`` | ``contradiction`` | ``resurrection`` | ``supersession`` | ``reject``
    #: | ``quarantine`` | ``reaffirmation``. Reported so a subset can be scored alone.
    category: str = "golden"
    candidate: dict
    expect: WriteExpectation


class MemoryTransitionStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["transition"] = "transition"
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    target: str
    status: MemoryStatus
    actor_id: str = "memory-evaluator"
    reason: str = "evaluation"
    human_review_ref: str | None = None


class MemorySeedStep(BaseModel):
    """Persist a record directly in a given state, bypassing the write policy.

    Needed because some read-side guarantees are **unreachable through the write
    path**. The clearest case is the taint filter: the policy quarantines every
    candidate carrying a taint label, and ``_validate_activation`` lists
    ``UNRESOLVED_TAINT`` as a hard blocker, so no sequence of writes can produce an
    ACTIVE record that carries taint labels. A probe aimed at that filter therefore
    passes on the *status* check and never exercises the filter at all -- deleting
    the filter changes nothing. Seeding models the one production way such a record
    exists: it was written before the filter (or the marker vocabulary) existed and
    is still ACTIVE after migration.

    **A seed may only be used to state a read-side premise.** It bypasses the
    policy by construction, so it can never support a claim about write-side
    governance; :attr:`MemoryScenarioCorpus.writes` excludes it from the write
    score, and the report lists every seeded step id so a reader can tell which
    records did not come from the policy under test.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["seed"] = "seed"
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    category: str = "seed"
    candidate: dict
    #: Only the two states a migrated-but-not-yet-filtered record can hold. Anything
    #: else would be this corpus inventing a lifecycle the repository does not have.
    status: Literal[MemoryStatus.ACTIVE, MemoryStatus.QUARANTINE] = MemoryStatus.ACTIVE
    reason: str = "seeded by the evaluation corpus to state a read-side premise"


MemoryStep = Annotated[
    MemoryWriteStep | MemoryTransitionStep | MemorySeedStep, Field(discriminator="kind")
]


class DeliveryLoad(BaseModel):
    """What the memory has to survive on its way into the prompt.

    ``evidence_items`` x ``evidence_item_chars`` describes the evidence the workflow
    attaches for the same run; the control items (task, policy, output schema) are added
    by the harness because the real path always carries them. These are **declared
    assumptions about a run's shape**, not measurements of production traffic -- no
    ``evidence.joined`` event has ever been recorded in the production database, so the
    real volume is unknown. The report names the load so a green here cannot be read as
    a production claim.

    ``rationale`` is mandatory as soon as the load is non-empty, for the same reason
    ``MemoryProbe.delivery_rationale`` is: an undeclared basis is an unattributable
    number, and a gate tuned by back-filling the load from the run it gates measures
    nothing. Derive the load from configuration the product is already committed to
    (the knowledge packer's own ceilings), then write down which ones.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_input_tokens: int = Field(default=12_000, ge=1024, le=1_000_000)
    evidence_items: int = Field(default=0, ge=0, le=200)
    #: Per item, not the total: one item is one serialized evidence row, so the natural
    #: unit is a single stored parent (``parent_max_chars``), not a sum the caller has
    #: to pre-divide.
    evidence_item_chars: int = Field(default=0, ge=0, le=20_000)
    #: The envelope's evidence-channel ceiling. ``None`` is the shipped default (no
    #: cap), and the point of naming it here is that the two settings are measured
    #: against each other rather than argued about: an uncapped envelope leaves the
    #: memory channel the remainder, a capped one leaves it the cap's complement.
    evidence_token_cap: int | None = Field(default=None, ge=256, le=1_000_000)
    rationale: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_rationale(self) -> DeliveryLoad:
        if self.evidence_items and not self.rationale.strip():
            raise ValueError(
                "a non-empty delivery load must state its rationale; an unexplained load "
                "makes the delivery gate unattributable"
            )
        if self.evidence_items and not self.evidence_item_chars:
            raise ValueError(
                "evidence_items without evidence_item_chars would attach empty evidence, "
                "which cannot compete for the budget the gate is measuring"
            )
        return self


class MemoryProbe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    category: str = "retrieval"
    #: Seconds relative to the load anchor; negative is the past. Kept as an offset so
    #: the committed corpus does not rot.
    at_offset_s: float = 0.0
    tenant: str = "primary"
    text: str = Field(min_length=1, max_length=4000)
    user_id: str = "evaluator"
    entity_ids: tuple[int, ...] = ()
    group_ids: tuple[int, ...] = ()
    service_ids: tuple[str, ...] = ()
    limit: int = Field(default=8, ge=1, le=50)
    #: Write-step ids that must appear in the returned selection.
    must_surface: tuple[str, ...] = ()
    #: Write-step ids that must never appear. A single appearance is a leak.
    must_not_surface: tuple[str, ...] = ()
    leak_reason: str = ""
    #: Product requirement: every required record must land within the first
    #: ``max_rank`` positions. **Set this from the requirement, never from the
    #: observed rank.** A gate back-filled from the run it governs measures nothing
    #: -- it would have passed by construction on the day it was written and can
    #: only ever ratify whatever the retriever already does. ``max_rank_rationale``
    #: exists to make that failure visible in review: a rationale that names an
    #: observed position instead of a caller-facing need is the tell.
    max_rank: int | None = Field(default=None, ge=1, le=50)
    max_rank_rationale: str = ""
    #: Product requirement: the required records must still be in the prompt after the
    #: context builder has spent its budget, not merely returned by the retriever.
    #: Declared for the same reason and under the same rule as ``max_rank``: state the
    #: requirement, never back-fill it from the run it governs.
    must_deliver: bool = False
    delivery_rationale: str = ""

    @model_validator(mode="after")
    def validate_probe(self) -> MemoryProbe:
        overlap = set(self.must_surface) & set(self.must_not_surface)
        if overlap:
            raise ValueError(
                f"probe {self.id} lists {sorted(overlap)} as both required and forbidden"
            )
        if self.must_not_surface and not self.leak_reason:
            raise ValueError(f"probe {self.id} forbids records without stating why")
        if self.max_rank is not None:
            if not self.must_surface:
                raise ValueError(f"probe {self.id} sets max_rank without requiring a record")
            if not self.max_rank_rationale.strip():
                raise ValueError(f"probe {self.id} sets max_rank without stating the requirement")
        if self.must_deliver:
            if not self.must_surface:
                raise ValueError(f"probe {self.id} sets must_deliver without requiring a record")
            if not self.delivery_rationale.strip():
                raise ValueError(
                    f"probe {self.id} sets must_deliver without stating the requirement"
                )
        return self


class MemoryScenarioCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    name: str
    description: str = ""
    tenants: dict[str, str]
    #: Ordered: a transition must be able to precede the write that reacts to it
    #: (resurrection after revocation, a new version after supersession).
    steps: list[MemoryStep]
    probes: list[MemoryProbe]
    #: What a memory must survive to reach the model. Declared, not measured: see
    #: :class:`DeliveryLoad`.
    delivery: DeliveryLoad = Field(default_factory=DeliveryLoad)

    @property
    def writes(self) -> list[MemoryWriteStep]:
        return [step for step in self.steps if isinstance(step, MemoryWriteStep)]

    @property
    def seeds(self) -> list[MemorySeedStep]:
        return [step for step in self.steps if isinstance(step, MemorySeedStep)]

    @property
    def record_ids(self) -> set[str]:
        """Step ids that produce a stored record -- the ids a probe may name.

        Seeds are included because they do store a record; they are kept out of
        :attr:`writes` precisely because they do not go through the policy, so the
        write score must not see them.
        """
        return {step.id for step in self.writes} | {step.id for step in self.seeds}

    @model_validator(mode="after")
    def validate_references(self) -> MemoryScenarioCorpus:
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        record_ids = self.record_ids
        for step in self.writes:
            if step.expect.same_as is not None and step.expect.same_as not in record_ids:
                raise ValueError(f"write {step.id} points at unknown step {step.expect.same_as}")
        for step in self.steps:
            if isinstance(step, MemoryTransitionStep) and step.target not in record_ids:
                raise ValueError(f"transition {step.id} targets unknown record {step.target}")
        for probe in self.probes:
            for key in (*probe.must_surface, *probe.must_not_surface):
                if key not in record_ids:
                    raise ValueError(f"probe {probe.id} references unknown record {key}")
            if probe.tenant not in self.tenants:
                raise ValueError(f"probe {probe.id} references unknown tenant {probe.tenant}")
        return self


def load_corpus(path: Path) -> MemoryScenarioCorpus:
    return MemoryScenarioCorpus.model_validate(json.loads(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------------------
# Report models
# --------------------------------------------------------------------------------------


class ClassScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: str
    support: int
    precision: float
    recall: float


class WriteOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    label: str
    category: str
    action_match: bool
    stored_match: bool
    expected_action: str
    actual_action: str
    expected_reason_codes: list[str]
    actual_reason_codes: list[str]
    missing_reason_codes: list[str]
    expected_stored: str
    actual_stored: str
    memory_id: str | None = None


class WriteScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: int
    action_accuracy: float
    stored_accuracy: float
    reason_code_accuracy: float
    #: Tracked apart from the averages because the error directions are asymmetric:
    #: activating what should have been held back is a governance breach, holding
    #: back what should have gone live is only a cost.
    unsafe_activations: list[str]
    spurious_rejections: list[str]
    per_action: list[ClassScore]


class ProbeOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    probe_id: str
    label: str
    category: str
    ranked_keys: list[str]
    must_surface: list[str]
    missing_required: list[str]
    leaks: list[str]
    leak_reason: str
    recall: float
    mrr: float
    ndcg: float
    max_rank: int | None = None
    #: 1-based position of each required record. A required record that did not
    #: surface at all is absent -- a miss is a recall failure, not a rank failure,
    #: and conflating the two would let the rank gate hide a missing record.
    required_ranks: dict[str, int] = Field(default_factory=dict)
    #: The deepest position any required record landed at; ``None`` when any of them
    #: is missing. This is what the gate reads, so "every required record is within
    #: the first N" is the property actually enforced.
    worst_required_rank: int | None = None
    rank_ok: bool = True
    #: Required records the retriever returned and the context builder then pruned.
    #: Empty for a probe that declares no delivery requirement.
    pruned_required: list[str] = Field(default_factory=list)
    delivered: bool = True
    #: Memory-channel tokens this probe's envelope actually selected, and the room
    #: that channel had left. ``None`` when the probe declared no delivery
    #: requirement -- the probe was never measured, which is not the same as zero.
    delivery_memory_tokens: int | None = None
    delivery_headroom_tokens: int | None = None


class ProbeScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    probes: int
    #: Probes that produced at least one leak, over all probes in the group.
    leak_rate: float
    leaking_probes: list[str]
    leaked_records: int


class RankingScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str
    k: int
    probes: int
    recall: float
    mrr: float
    ndcg: float


class RankViolation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    probe_id: str
    max_rank: int
    #: ``None`` when a required record never surfaced at all.
    worst_required_rank: int | None
    missing_required: list[str]


class RankGate(BaseModel):
    """The declarative "the right memory must be near the top" gate.

    Averages hide exactly the failure this gate exists for: a probe whose record
    lands 7th still contributes a respectable MRR, and a corpus where everything
    lands 2nd and a corpus where half land 1st and half land 3rd produce the same
    recall. The gate states the caller-facing requirement per probe and reports the
    violation, so a regression in position is a red result rather than a smaller
    decimal.

    Only probes that declare ``max_rank`` are gated. A probe that states no rank
    requirement cannot be violated by a rank, and counting it as a pass would
    inflate the gate's coverage.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    probes: int
    violations: list[RankViolation]

    @property
    def passed(self) -> bool:
        return not self.violations


class DeliveryViolation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    probe_id: str
    #: Required records that the retriever returned but the context builder pruned.
    pruned_required: list[str]
    reason: str


class DeliveryGate(BaseModel):
    """The declarative "the right memory must reach the model" gate.

    Every other block in this report stops at the retriever. Retrieval is not delivery:
    ``ContextBuilder`` sorts optional items by authority and then drops whatever no
    longer fits the token budget, and a memory (authority 0.7) sorts *after* evidence
    (0.95). So a memory can be retrieved, scored, ranked first -- and then pruned on the
    way into the prompt, with every probe in this file still green.

    That is the same shape of hole as a safety probe whose record was never reachable:
    the published number is clean and describes nothing. This gate closes it by reading
    the context builder's own selection manifest and reporting required records that
    were retrieved but not selected.

    Only probes that declare ``must_deliver`` are gated; counting the rest as passes
    would inflate the gate's coverage exactly as it would for rank.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    probes: int
    violations: list[DeliveryViolation]
    #: Tokens left unspent once evidence and the required control items were packed,
    #: i.e. everything the memory channel had to work with. The smallest value across
    #: gated probes: the gate says "the requirement held", this says "and it held with
    #: this much room". A pass at 249 of 10720 tokens is a pass that is one longer
    #: memory away from failing, and no violation list would ever say so.
    headroom_tokens: int = 0
    #: What the memory channel actually consumed, for the same reason.
    memory_tokens: int = 0

    @property
    def passed(self) -> bool:
        return not self.violations


class MemoryEvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = SCHEMA_VERSION
    corpus: str
    description: str = ""
    anchor: datetime
    scoring_mode: str
    #: Which scorer produced the ranking block, named precisely enough to be reproduced
    #: (``lexical-jaccard``, or ``BAAI/bge-m3@<revision>``). ``scoring_mode`` alone only
    #: says "embedding", which cannot distinguish two pinned revisions with different
    #: vectors -- and the ranking block is the one block whose numbers depend on it.
    scoring_model: str = ""
    write: WriteScore
    write_outcomes: list[WriteOutcome]
    probe: ProbeScore
    probe_outcomes: list[ProbeOutcome]
    ranking: list[RankingScore]
    per_category: dict[str, ProbeScore]
    rank_gate: RankGate
    delivery_gate: DeliveryGate
    delivery_load: DeliveryLoad
    #: Step ids whose records were seeded straight into the repository, bypassing the
    #: write policy. Listed so no reader mistakes them for write-side results.
    seeded: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------------------


#: Deterministic namespace for the run ids the delivery layer feeds the context
#: builder. The corpus holds no generated UUIDs, and this keeps that true.
_DELIVERY_NAMESPACE = UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def _evidence_content(chars: int) -> str:
    """Deterministic filler of exactly ``chars`` characters (or ``""`` for zero).

    Declared and actual length must agree, or the load the report names is not the load
    the context builder saw and the gate's green describes the wrong envelope.
    """
    if chars <= 0:
        return ""
    filler = (
        "Gateway certificate rotation history and assignment notes for the Authentication Service. "
    )
    return (filler * (chars // len(filler) + 1))[:chars]


def _delivery_envelope(
    *,
    corpus: MemoryScenarioCorpus,
    probe: MemoryProbe,
    selections: Sequence,
    by_id: Mapping[UUID, str],
) -> tuple[set[str], str, int, int]:
    """Build the ANALYSIS envelope a live run would build, and read its manifest.

    Returns the set of required memory keys that were **retrieved but not selected**,
    the manifest reason for the first of them, and the memory channel's tokens spent
    and left over.

    The item list mirrors ``Phase5Governance.build_context`` for the ANALYSIS agent:
    the required control items (task, policy, output schema), the retrieved memories at
    their production authority, and the evidence that competes with them for the same
    budget. It deliberately builds through the real :class:`ContextBuilder` rather than
    modelling the budget here -- a reimplementation would drift from the thing it is
    supposed to be measuring.
    """
    load = corpus.delivery
    run_id = uuid5(_DELIVERY_NAMESPACE, f"{corpus.name}/{probe.id}")

    def control(item_id: str, source: ContextSource, content: str) -> ContextItem:
        return ContextItem(
            item_id=item_id,
            source=source,
            content=content,
            allowed_agents=frozenset({ContextAgent.ANALYSIS}),
            trust=TrustLabel.TRUSTED_CONTROL,
            authority=1,
            relevance=1,
            required=True,
            provenance_ref=f"run://{run_id}/{item_id}",
        )

    items: list[ContextItem] = [
        control("task", ContextSource.TASK, json.dumps({"goal": probe.text})),
        control("policy", ContextSource.POLICY, json.dumps({"policy_version": "evaluation"})),
        control(
            "output-schema",
            ContextSource.OUTPUT_SCHEMA,
            json.dumps(AnalysisResult.model_json_schema(), ensure_ascii=False),
        ),
    ]
    items.extend(
        ContextItem(
            item_id=f"memory:{selection.memory.memory_id}",
            source=ContextSource.MEMORY,
            content=selection.memory.content,
            allowed_agents=frozenset({ContextAgent.ANALYSIS}),
            trust=TrustLabel.VERIFIED,
            authority=0.7,
            relevance=selection.score,
            provenance_ref=f"memory://{selection.memory.memory_id}",
            occurred_at=selection.memory.updated_at,
        )
        for selection in selections
    )
    items.extend(
        ContextItem(
            item_id=f"evidence:{index}",
            source=ContextSource.EVIDENCE,
            content=json.dumps(
                {
                    "resource": f"glpi://ticket/{1000 + index}",
                    "content": _evidence_content(load.evidence_item_chars),
                }
            ),
            allowed_agents=frozenset({ContextAgent.ANALYSIS}),
            trust=TrustLabel.VERIFIED,
            authority=0.95,
            relevance=1.0,
            provenance_ref=f"glpi://ticket/{1000 + index}",
            taint_labels=frozenset({"untrusted_content"}),
        )
        for index in range(load.evidence_items)
    )

    envelope = ContextBuilder().build(
        tenant_id=_resolve_tenant(corpus, probe.tenant),
        run_id=run_id,
        task_id=f"EVAL-{probe.id}",
        agent=ContextAgent.ANALYSIS,
        items=items,
        max_input_tokens=load.max_input_tokens,
        source_token_caps=(
            {ContextSource.EVIDENCE: load.evidence_token_cap}
            if load.evidence_token_cap is not None
            else None
        ),
    )
    pruned: set[str] = set()
    reason = ""
    for entry in envelope.selection_manifest:
        if entry.decision == "selected" or not entry.item_id.startswith("memory:"):
            continue
        key = by_id.get(UUID(entry.item_id.removeprefix("memory:")))
        if key is None:
            continue
        pruned.add(key)
        reason = reason or entry.reason
    budget = envelope.budget
    usable = budget.max_input_tokens - budget.system_reserve - budget.output_reserve
    memory_tokens = sum(
        entry.tokens
        for entry in envelope.selection_manifest
        if entry.decision == "selected" and entry.source is ContextSource.MEMORY
    )
    # Everything the memory channel *could* have spent: the budget minus what the other
    # channels took. Reporting tokens_used instead would fold the memory's own spend
    # back in and make a starved envelope look roomy.
    return pruned, reason, memory_tokens, usable - (budget.tokens_used - memory_tokens)


def _resolve_tenant(corpus: MemoryScenarioCorpus, name: str) -> UUID:
    try:
        return UUID(corpus.tenants[name])
    except (KeyError, ValueError) as exc:
        raise MemoryEvaluationError(f"corpus tenant {name!r} is not a known UUID") from exc


_TIME_FIELDS = ("valid_from", "valid_to", "expires_at")


def _build_candidate(
    step: MemoryWriteStep | MemorySeedStep,
    corpus: MemoryScenarioCorpus,
    anchor: datetime,
    identifiers: dict[str, UUID],
) -> MemoryCandidate:
    """Resolve one corpus step into a validated candidate.

    Tenant names, time offsets and episode references are all resolved here, in that
    order, so the committed JSON holds no wall-clock timestamps and no generated UUID.
    """
    payload = dict(step.candidate)
    payload["tenant_id"] = str(_resolve_tenant(corpus, payload.pop("tenant", "primary")))
    for field in _TIME_FIELDS:
        offset = payload.pop(f"{field}_offset_s", None)
        if offset is not None:
            payload[field] = anchor + timedelta(seconds=float(offset))
    # ``valid_from`` would otherwise default to the wall-clock instant the candidate is
    # constructed -- a few microseconds *after* the anchor every probe queries at, which
    # makes every record invisible to its own probe. The anchor is the reference instant.
    payload.setdefault("valid_from", anchor)
    episodes = payload.pop("supporting_episode_steps", None)
    if episodes is not None:
        missing = [key for key in episodes if key not in identifiers]
        if missing:
            raise MemoryEvaluationError(
                f"write {step.id} supports itself with unwritten episodes {missing}"
            )
        payload["supporting_episode_ids"] = [str(identifiers[key]) for key in episodes]
    return MemoryCandidate.model_validate(payload)


def _class_scores(
    expected: Sequence[str], actual: Sequence[str], labels: Sequence[str]
) -> list[ClassScore]:
    scores: list[ClassScore] = []
    for label in labels:
        true_positive = sum(
            1 for want, got in zip(expected, actual, strict=True) if want == label and got == label
        )
        predicted = sum(1 for got in actual if got == label)
        supported = sum(1 for want in expected if want == label)
        scores.append(
            ClassScore(
                action=label,
                support=supported,
                precision=round(true_positive / predicted, 4) if predicted else 0.0,
                recall=round(true_positive / supported, 4) if supported else 0.0,
            )
        )
    return scores


def _stored_label(record_status: MemoryStatus | None, unchanged: bool) -> str:
    if unchanged:
        return "unchanged"
    if record_status is None:
        return "rejected"
    return record_status.value


async def evaluate(
    corpus: MemoryScenarioCorpus,
    *,
    anchor: datetime | None = None,
    embedding=None,
    scoring_model: str | None = None,
    policy: MemoryGovernancePolicy | None = None,
    retriever_factory: Callable[[InMemoryMemoryRepository], MemoryRetriever] | None = None,
) -> MemoryEvaluationReport:
    """Run the full write/read evaluation over ``corpus``.

    ``embedding`` is an optional :class:`~servicemind.memory.service.MemoryEmbeddingProvider`.
    Without one the retriever falls back to its lexical path, and the report records
    that in ``scoring_mode`` so no reader mistakes the ranking block for the production
    (embedding) configuration.

    ``scoring_model`` names that provider (``BAAI/bge-m3@<revision>``). It is required
    whenever ``embedding`` is given: ``scoring_mode`` alone would record only the word
    "embedding", and two pinned revisions produce different vectors from identical text,
    so an unnamed run cannot be reproduced or compared against.

    ``policy`` and ``retriever_factory`` exist so a caller can substitute a deliberately
    weaker component and watch the harness report the resulting breach; the defaults are
    the production policy and retriever.
    """
    if anchor is None:
        anchor = datetime.now(UTC)
    if embedding is None:
        scoring_mode, scoring_model = "lexical", scoring_model or "lexical-jaccard"
    else:
        if not scoring_model:
            raise MemoryEvaluationError(
                "an embedding scorer must be named via scoring_model; an unnamed provider "
                "makes the ranking block unattributable"
            )
        scoring_mode = "embedding"

    repository = InMemoryMemoryRepository()
    policy = policy or MemoryGovernancePolicy()
    writer = MemoryWriter(repository, policy)
    retriever = (
        retriever_factory(repository)
        if retriever_factory is not None
        else MemoryRetriever(repository, embedding=embedding)
    )

    write_outcomes: list[WriteOutcome] = []
    seeded: list[str] = []
    identifiers: dict[str, UUID] = {}
    for step in corpus.steps:
        if isinstance(step, MemorySeedStep):
            # The policy is bypassed deliberately. The decision below is a *label*
            # saying which state the corpus plants, not a governance outcome -- hence
            # a reason code that names the seeding rather than any policy verdict.
            record = await repository.persist(
                _build_candidate(step, corpus, anchor, identifiers),
                MemoryWriteDecision(
                    action=(
                        MemoryWriteAction.ACTIVATE
                        if step.status is MemoryStatus.ACTIVE
                        else MemoryWriteAction.QUARANTINE
                    ),
                    reason_codes=(SEED_REASON_CODE,),
                ),
            )
            if record is None:
                raise MemoryEvaluationError(f"seed {step.id} did not store a record")
            if record.status is not step.status:
                raise MemoryEvaluationError(
                    f"seed {step.id} asked for {step.status.value}, "
                    f"repository stored {record.status.value}"
                )
            identifiers[step.id] = record.memory_id
            seeded.append(step.id)
            continue
        if isinstance(step, MemoryTransitionStep):
            target = identifiers.get(step.target)
            if target is None:
                raise MemoryEvaluationError(f"transition {step.id} targets a rejected write")
            await repository.transition(
                target,
                step.status,
                actor_id=step.actor_id,
                reason=step.reason,
                human_review_ref=step.human_review_ref,
            )
            continue

        candidate = _build_candidate(step, corpus, anchor, identifiers)
        decision = policy.assess(candidate)
        known_before = {record.memory_id for record in repository.records}
        record = await writer.write(candidate)
        unchanged = record is not None and record.memory_id in known_before
        if record is not None:
            identifiers[step.id] = record.memory_id

        # The repository appends codes of its own (VERSION_CONFLICT and friends) to the
        # decision it actually persists. Those are not visible from here, so the one
        # with a durable marker is recovered from the stored record -- the authority
        # for what actually landed.
        actual_codes = list(decision.reason_codes)
        if record is not None and record.provenance.get("conflict_detected") is True:
            actual_codes.append("VERSION_CONFLICT")

        expected = step.expect
        same_as_ok = True
        if expected.same_as is not None:
            same_as_ok = (
                record is not None and identifiers.get(expected.same_as) == record.memory_id
            )
        write_outcomes.append(
            WriteOutcome(
                step_id=step.id,
                label=step.label,
                category=step.category,
                action_match=decision.action is expected.policy_action,
                stored_match=_stored_label(record.status if record else None, unchanged)
                == expected.stored
                and same_as_ok,
                expected_action=expected.policy_action.value,
                actual_action=decision.action.value,
                expected_reason_codes=list(expected.policy_reason_codes),
                actual_reason_codes=actual_codes,
                missing_reason_codes=[
                    c for c in expected.policy_reason_codes if c not in actual_codes
                ],
                expected_stored=expected.stored,
                actual_stored=_stored_label(record.status if record else None, unchanged),
                memory_id=str(record.memory_id) if record else None,
            )
        )

    # Name the returned records by corpus id, keeping the retriever's order. A
    # reaffirmed write shares its record with the step it deduped onto, so the first
    # (canonical) step id wins.
    by_id: dict[UUID, str] = {}
    for key, value in identifiers.items():
        by_id.setdefault(value, key)

    probe_outcomes: list[ProbeOutcome] = []
    #: (headroom, spent) per gated probe; the tightest is the one that decides the gate.
    delivery_headroom: list[tuple[int, int]] = []
    for probe in corpus.probes:
        query = MemoryQuery(
            tenant_id=_resolve_tenant(corpus, probe.tenant),
            text=probe.text,
            user_id=probe.user_id,
            entity_ids=frozenset(probe.entity_ids),
            group_ids=frozenset(probe.group_ids),
            service_ids=frozenset(probe.service_ids),
            at=anchor + timedelta(seconds=probe.at_offset_s),
            limit=probe.limit,
        )
        selections = await retriever.retrieve(query)
        ranked_keys = [
            by_id[selection.memory.memory_id]
            for selection in selections
            if selection.memory.memory_id in by_id
        ]
        relevant = set(probe.must_surface)
        required_ranks = {
            key: position for position, key in enumerate(ranked_keys, start=1) if key in relevant
        }
        # Only a *complete* set of required records has a worst rank. If one is
        # missing, the probe already failed every ranking metric; reporting the
        # surviving records' worst rank here would dress a miss up as a rank result.
        worst_required_rank = (
            max(required_ranks.values())
            if relevant and len(required_ranks) == len(relevant)
            else None
        )
        pruned_required: list[str] = []
        # ``None`` on a probe that declared no delivery requirement: "not measured"
        # and "measured zero" are different facts and the report must not merge them.
        memory_tokens: int | None = None
        headroom: int | None = None
        if probe.must_deliver:
            pruned, _reason, spent, room = _delivery_envelope(
                corpus=corpus, probe=probe, selections=selections, by_id=by_id
            )
            # A record the retriever never returned is a recall failure, already
            # reported above; delivery only speaks to records that were returned.
            pruned_required = sorted(key for key in pruned if key in relevant)
            memory_tokens, headroom = spent, room
            delivery_headroom.append((room, spent))
        probe_outcomes.append(
            ProbeOutcome(
                probe_id=probe.id,
                label=probe.label,
                category=probe.category,
                ranked_keys=ranked_keys,
                must_surface=list(probe.must_surface),
                missing_required=[key for key in probe.must_surface if key not in ranked_keys],
                leaks=[key for key in probe.must_not_surface if key in ranked_keys],
                leak_reason=probe.leak_reason,
                recall=round(recall_at_k(ranked_keys, relevant, probe.limit), 4),
                mrr=round(mrr_at_k(ranked_keys, relevant, probe.limit), 4),
                ndcg=round(ndcg_at_k(ranked_keys, relevant, probe.limit), 4),
                max_rank=probe.max_rank,
                required_ranks=required_ranks,
                worst_required_rank=worst_required_rank,
                rank_ok=probe.max_rank is None
                or (worst_required_rank is not None and worst_required_rank <= probe.max_rank),
                pruned_required=pruned_required,
                delivered=not pruned_required,
                delivery_memory_tokens=memory_tokens,
                delivery_headroom_tokens=headroom,
            )
        )

    def summarize(outcomes: Sequence[ProbeOutcome]) -> ProbeScore:
        leaking = [outcome.probe_id for outcome in outcomes if outcome.leaks]
        return ProbeScore(
            probes=len(outcomes),
            leak_rate=round(len(leaking) / len(outcomes), 4) if outcomes else 0.0,
            leaking_probes=leaking,
            leaked_records=sum(len(outcome.leaks) for outcome in outcomes),
        )

    def mean(values: Sequence[float]) -> float:
        return round(statistics.fmean(values), 4) if values else 0.0

    write_score = WriteScore(
        steps=len(write_outcomes),
        action_accuracy=mean([1.0 if o.action_match else 0.0 for o in write_outcomes]),
        stored_accuracy=mean([1.0 if o.stored_match else 0.0 for o in write_outcomes]),
        reason_code_accuracy=mean(
            [1.0 if not o.missing_reason_codes else 0.0 for o in write_outcomes]
        ),
        unsafe_activations=[
            o.step_id
            for o in write_outcomes
            if o.actual_action == "activate" and o.expected_action != "activate"
        ],
        spurious_rejections=[
            o.step_id
            for o in write_outcomes
            if o.actual_action == "reject" and o.expected_action != "reject"
        ],
        per_action=_class_scores(
            [o.expected_action for o in write_outcomes],
            [o.actual_action for o in write_outcomes],
            [action.value for action in MemoryWriteAction],
        ),
    )

    ranked_probes = [o for o in probe_outcomes if o.must_surface]
    ranking = [
        RankingScore(
            mode=scoring_mode,
            k=k,
            probes=len(ranked_probes),
            recall=mean(
                [recall_at_k(o.ranked_keys, set(o.must_surface), k) for o in ranked_probes]
            ),
            mrr=mean([mrr_at_k(o.ranked_keys, set(o.must_surface), k) for o in ranked_probes]),
            ndcg=mean([ndcg_at_k(o.ranked_keys, set(o.must_surface), k) for o in ranked_probes]),
        )
        for k in RANK_CUTOFFS
    ]

    rank_violations: list[RankViolation] = []
    gated_probes = 0
    for outcome in probe_outcomes:
        if outcome.max_rank is None:
            continue
        gated_probes += 1
        if not outcome.rank_ok:
            rank_violations.append(
                RankViolation(
                    probe_id=outcome.probe_id,
                    max_rank=outcome.max_rank,
                    worst_required_rank=outcome.worst_required_rank,
                    missing_required=outcome.missing_required,
                )
            )

    delivery_violations: list[DeliveryViolation] = []
    for outcome in probe_outcomes:
        if outcome.pruned_required:
            delivery_violations.append(
                DeliveryViolation(
                    probe_id=outcome.probe_id,
                    pruned_required=outcome.pruned_required,
                    reason="token_budget_exceeded",
                )
            )
    # A violation may only ever come from a probe that declared the requirement, so
    # the gate cannot go red on a probe that never asked to be gated.
    declared = {probe.id for probe in corpus.probes if probe.must_deliver}
    if any(violation.probe_id not in declared for violation in delivery_violations):
        raise MemoryEvaluationError("delivery gate reported a probe that declared no requirement")

    categories = sorted({outcome.category for outcome in probe_outcomes})
    return MemoryEvaluationReport(
        corpus=corpus.name,
        description=corpus.description,
        anchor=anchor,
        scoring_mode=scoring_mode,
        scoring_model=scoring_model,
        write=write_score,
        write_outcomes=write_outcomes,
        probe=summarize(probe_outcomes),
        probe_outcomes=probe_outcomes,
        ranking=ranking,
        per_category={
            name: summarize([o for o in probe_outcomes if o.category == name])
            for name in categories
        },
        rank_gate=RankGate(probes=gated_probes, violations=rank_violations),
        delivery_gate=DeliveryGate(
            probes=len(declared),
            violations=delivery_violations,
            headroom_tokens=min((h for h, _ in delivery_headroom), default=0),
            memory_tokens=max((m for _, m in delivery_headroom), default=0),
        ),
        delivery_load=corpus.delivery,
        seeded=seeded,
    )


def render_markdown(report: MemoryEvaluationReport) -> str:
    """Human-readable mirror of the JSON report."""
    lines = [
        "# 记忆质量评测报告",
        "",
        f"- 语料：`{report.corpus}`",
        f"- 打分模式：`{report.scoring_mode}`",
        f"- 打分模型：`{report.scoring_model}`",
        f"- 时间锚点：`{report.anchor.isoformat()}`",
        "",
        "> 本语料由本项目编写，是对设计所声称保证的**回归契约**，不是任何租户真实记忆",
        "> 分布的度量，也不能替代生产记忆流量回放。时间以锚点偏移表达。",
        "",
        "## 1. 写侧治理",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 步骤数 | {report.write.steps} |",
        f"| 动作精确匹配率 | {report.write.action_accuracy} |",
        f"| 落库状态匹配率 | {report.write.stored_accuracy} |",
        f"| 原因码覆盖率 | {report.write.reason_code_accuracy} |",
        f"| **越权激活** | {report.write.unsafe_activations or '无'} |",
        f"| 误拒 | {report.write.spurious_rejections or '无'} |",
        "",
        "| 动作 | 支持数 | 精确率 | 召回率 |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {score.action} | {score.support} | {score.precision} | {score.recall} |"
        for score in report.write.per_action
    ]
    lines += [
        "",
        "## 2. 读侧安全（与打分模式无关）",
        "",
        f"- 探针数：{report.probe.probes}",
        f"- **泄漏率：{report.probe.leak_rate}**（{report.probe.leaked_records} 条记录）",
        f"- 泄漏探针：{report.probe.leaking_probes or '无'}",
        "",
        "| 类别 | 探针数 | 泄漏率 | 泄漏记录数 |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {name} | {score.probes} | {score.leak_rate} | {score.leaked_records} |"
        for name, score in sorted(report.per_category.items())
    ]
    lines += [
        "",
        "## 3. 读侧排序",
        "",
        f"打分模式 `{report.scoring_mode}`（模型 `{report.scoring_model}`）；"
        "仅统计声明了 `must_surface` 的探针。",
        "",
        "| k | 探针数 | Recall@k | MRR@k | NDCG@k |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {score.k} | {score.probes} | {score.recall} | {score.mrr} | {score.ndcg} |"
        for score in report.ranking
    ]
    lines += [
        "",
        "### 3.1 排名门禁（声明式）",
        "",
        "只统计显式声明 `max_rank` 的探针；未声明排名要求的探针不计入，",
        "把它们算作通过会虚增门禁覆盖面。",
        "",
        f"- 受门禁探针数：{report.rank_gate.probes}",
        f"- **判定：{'通过' if report.rank_gate.passed else '不通过'}**",
        "",
    ]
    if report.rank_gate.violations:
        lines += [
            "| 探针 | 要求名次 | 实际最深名次 | 未召回的必需记录 |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| {v.probe_id} | ≤{v.max_rank} | "
            f"{v.worst_required_rank if v.worst_required_rank is not None else '未召回'} | "
            f"{v.missing_required or '无'} |"
            for v in report.rank_gate.violations
        ]
        lines.append("")
    load = report.delivery_load
    lines += [
        "### 3.2 投递门禁（声明式）",
        "",
        "前两节都止步于检索器。**检索不等于投递**：`ContextBuilder` 按 authority 排序后",
        "丢弃超出 token 预算的可选项，而记忆（authority 0.7）排在 evidence（0.95）之后，",
        "因此一条记忆可以被检索到、排在第 1 位，然后在进入提示前被裁掉——而上面每一行仍然全绿。",
        "",
        "本节按真实 `ContextBuilder` 的**选择清单**判定：声明了 `must_deliver` 的探针，",
        "其必需记录必须处于 `selected` 状态，而不只是被检索器返回。",
        "",
        f"- 受门禁探针数：{report.delivery_gate.probes}",
        f"- 声明的载荷：预算 {load.max_input_tokens} token、evidence {load.evidence_items} 条"
        f"（每条约 {load.evidence_item_chars} 字符）",
        f"- 载荷依据：{load.rationale or '（空载荷，未声明）'}",
        f"- 记忆通道可支配余量：最紧探针 **{report.delivery_gate.headroom_tokens}** token",
        f"- 记忆通道实际占用：占用最多的探针 **{report.delivery_gate.memory_tokens}** token",
        f"- **判定：{'通过' if report.delivery_gate.passed else '不通过'}**",
        "",
        "> 载荷是**声明的运行形态假设，不是生产观测**：生产库中没有任何 `evidence.joined`",
        "> 事件被记录过，真实 evidence 体积未知。此处的绿色不得读作生产可达性结论。",
        "",
    ]
    gated = [o for o in report.probe_outcomes if o.delivery_memory_tokens is not None]
    if gated:
        # Both aggregates come from this table -- "tightest remaining room" and
        # "largest amount spent" are generally *different* probes, and a summary
        # that merges them would read as though one probe had spent more than it
        # was allowed.
        lines += [
            "| 探针 | 记忆通道余量 | 实际占用 | 判定 |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| {o.probe_id} | {o.delivery_headroom_tokens} | {o.delivery_memory_tokens} |"
            f" {'送达' if o.delivered else '**被裁**'} |"
            for o in gated
        ]
        lines.append("")
    if report.delivery_gate.violations:
        lines += [
            "| 探针 | 被裁掉的必需记录 | 原因 |",
            "| --- | --- | --- |",
        ]
        lines += [
            f"| {v.probe_id} | {v.pruned_required} | {v.reason} |"
            for v in report.delivery_gate.violations
        ]
        lines.append("")
    if report.seeded:
        lines += [
            "## 4. 直接播种的记录（绕过写入策略）",
            "",
            "以下记录由语料直接写入仓库，**未经过治理策略**，因此不构成任何写侧结论；",
            "它们只用于陈述读侧前提（例如「迁移前已存在、带污点标签且仍为 ACTIVE」）。",
            "",
            "`" + "`, `".join(report.seeded) + "`",
            "",
        ]
    return "\n".join(lines)
