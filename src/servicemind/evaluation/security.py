"""The Phase 7 security and fault scenario list: what each scenario defends, and against what.

``acceptance.py`` answers "did a real user's run come out right". This module answers a
different question: "does the platform still refuse the things it is supposed to refuse".
The two are separate on purpose. A run can come out right for the ordinary path and still
hand a revoked analyst their group's handbook, and a case list that only ever exercises
the happy path has no way to say so.

**A scenario names the evidence that decides it, and nothing decides itself.** Every
scenario carries at least one ``ScenarioEvidence``, and a scenario with no evidence is
rejected at load time rather than reported as covered -- an unbacked row in a security
table is worse than a missing one, because a reader counting rows reads it as coverage.
The evidence kinds are deliberately limited to things a machine can re-run:

``test``      a pytest node id. The runner executes it and records the exit status.
``mutation``  a named mutation inside a ``scripts/mutate_*.py`` experiment. This is the
              half that gives the first half teeth: a green test says the behaviour is
              present, and a mutation that removes the behaviour and turns that same test
              red says the test would have noticed. A scenario backed only by ``test``
              evidence is marked as such in the report -- see ``SecurityScenario.teeth``.
``acceptance``an acceptance case id. The observation is that case's recorded verdict, not
              a second live run: re-running ACC-22's outage would be a second, weaker
              observation of the same thing, and it would need the same stack.
``script``    a live verifier script that must exit 0 and print its JSON verdict.

**What this module does not claim.** It does not claim the scenario list is complete.
``SecurityScenarioSet`` enforces a floor (``MINIMUM_SCENARIOS``) and a set of categories,
which is a statement about the list, not about the platform; the report carries an
explicit "not covered here" section written by hand, because a machine cannot enumerate
the threats nobody thought of.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

SECURITY_SCHEMA_VERSION = "phase7-security-v1"

#: The floor the set has to clear to be a set at all. Stated as a number in the model
#: rather than left to the reviewer to count, because the requirement it comes from
#: ("P7.6.5 安全与故障场景 >= 40") is a number.
MINIMUM_SCENARIOS = 40

#: The categories a scenario may be filed under. Closed on purpose: an open string field
#: drifts into one category per scenario, and then the report groups nothing.
SECURITY_CATEGORIES = (
    "tenant-isolation",
    "entity-and-group-authorization",
    "principal-narrowing",
    "approval-binding",
    "write-path-authorization",
    "rate-limit-and-degradation",
    "prompt-injection",
    "data-bounds",
    "self-authored-evidence",
    "idempotency",
    "outbox-retention",
    "crash-recovery",
    "verifier-availability",
    "webhook-authenticity",
    "credential-handling",
)


class EvidenceKind(StrEnum):
    TEST = "test"
    MUTATION = "mutation"
    ACCEPTANCE = "acceptance"
    SCRIPT = "script"


class ScenarioEvidence(BaseModel):
    """One re-runnable thing that decides part of a scenario.

    ``mutation`` is a separate kind rather than a flag on ``test`` because it is graded
    differently: a mutation that does *not* turn its test red is the finding, and it is a
    finding about the test, not about the platform. Folding the two into one kind would
    make "the mutation was not detected" and "the test failed" the same verdict, and they
    call for opposite responses.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EvidenceKind
    #: A pytest node id, a ``scripts/mutate_*.py`` path, an acceptance case id, or a
    #: verifier script path. Which one is read off ``kind``.
    ref: str
    #: Required for ``mutation`` and forbidden otherwise: the mutation's own name, which
    #: is the anchor ``mutation_harness`` prints next to its verdict.
    mutation: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _the_mutation_field_matches_the_kind(self) -> ScenarioEvidence:
        if self.kind is EvidenceKind.MUTATION and not self.mutation:
            raise ValueError("a mutation evidence item must name the mutation it grades")
        if self.kind is not EvidenceKind.MUTATION and self.mutation is not None:
            raise ValueError("only a mutation evidence item may name a mutation")
        return self


class SecurityScenario(BaseModel):
    """One thing the platform must keep refusing, and what proves it still does.

    ``defends_against`` is written as the attack or fault, not as the control. A row that
    says "rate limiting is implemented" cannot be checked by a reader; a row that says
    "a throttled call must not be replayed inside the window it was told to wait out" can
    be held against the named test.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=120)
    category: str
    title: str = Field(min_length=1, max_length=200)
    defends_against: str = Field(min_length=1, max_length=1000)
    #: Why the named evidence would go red if the behaviour regressed. Required text, so
    #: a scenario cannot be filed without its author saying what the pin is anchored to.
    teeth: str = Field(min_length=1, max_length=1000)
    evidence: tuple[ScenarioEvidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _the_category_is_one_of_the_closed_set(self) -> SecurityScenario:
        if self.category not in SECURITY_CATEGORIES:
            raise ValueError(
                f"unknown security category {self.category!r}; the closed set is "
                f"{list(SECURITY_CATEGORIES)}"
            )
        return self


class SecurityScenarioSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    description: str
    scenarios: tuple[SecurityScenario, ...]
    #: Threats this set deliberately does not claim to cover, written by hand.
    #:
    #: Required text rather than an optional field, because the honest part of a security
    #: table is the part that says where it stops. A reader who counts 52 green rows and
    #: finds no statement about what is outside them will read the count as the whole
    #: picture; the count is a statement about the rows, not about the platform.
    not_covered: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _the_set_is_a_set(self) -> SecurityScenarioSet:
        if self.schema_version != SECURITY_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {SECURITY_SCHEMA_VERSION!r}, got {self.schema_version!r}"
            )
        ids = [scenario.id for scenario in self.scenarios]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"duplicate scenario ids: {duplicates}")
        if len(self.scenarios) < MINIMUM_SCENARIOS:
            raise ValueError(
                f"{len(self.scenarios)} scenarios is below the {MINIMUM_SCENARIOS} this "
                "set exists to clear; a smaller set is not a shorter list, it is a "
                "different claim"
            )
        return self

    def by_category(self) -> dict[str, list[SecurityScenario]]:
        grouped: dict[str, list[SecurityScenario]] = {name: [] for name in SECURITY_CATEGORIES}
        for scenario in self.scenarios:
            grouped[scenario.category].append(scenario)
        return {name: rows for name, rows in grouped.items() if rows}

    def mutation_scripts(self) -> list[str]:
        """Every mutation script the set grades against, once, in first-seen order.

        Run once each rather than once per scenario: a mutation script mutates one file
        and restores it, so two scenarios naming the same script are two reads of one
        experiment, and running it twice would double the wall clock to learn the same
        thing.
        """
        seen: list[str] = []
        for scenario in self.scenarios:
            for item in scenario.evidence:
                if item.kind is EvidenceKind.MUTATION and item.ref not in seen:
                    seen.append(item.ref)
        return seen

    def test_node_ids(self) -> list[str]:
        seen: list[str] = []
        for scenario in self.scenarios:
            for item in scenario.evidence:
                if item.kind is EvidenceKind.TEST and item.ref not in seen:
                    seen.append(item.ref)
        return seen


def load_security_scenarios(path: Path) -> SecurityScenarioSet:
    return SecurityScenarioSet.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _canonical(value: object) -> object:
    """Collapse unordered containers to a hash-order-free rendering.

    Same reason as ``acceptance._canonical``: this digest is compared across processes,
    and ``PYTHONHASHSEED`` varies per process, so a digest taken over a ``frozenset``
    would differ between the run that wrote a report and the run that checks it.
    """
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_canonical(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), default=str),
        )
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _digest(value: object) -> str:
    payload = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def scenario_set_digest(scenario_set: SecurityScenarioSet) -> str:
    """What the scenarios *say*. A change here invalidates any report generated before it."""
    return _digest(scenario_set.model_dump(mode="python"))


def observation_digest(payload: object) -> str:
    """What was *observed*, independent of the order the caller happened to hold it in.

    Kept here so the runner and the gate cannot compute differently -- and they did, which is
    why this sorts. The runner stamps the digest over the observations in *scenario-list*
    order; the gate reads the replay files back in *filename* order. Both are the same set and
    neither is wrong, but digesting them as handed produced two different numbers for one
    batch, so a reader comparing the run's summary to the report saw a mismatch that meant
    nothing. Sorting by ``scenario_id`` removes the caller's ordering from the answer
    entirely, which is the only way this function can keep the promise in its own name.
    """
    if isinstance(payload, Iterable) and not isinstance(payload, (str, bytes, Mapping)):
        rows = list(payload)
        if all(isinstance(row, Mapping) and "scenario_id" in row for row in rows):
            return _digest(sorted(rows, key=lambda row: str(row["scenario_id"])))
    return _digest(payload)
