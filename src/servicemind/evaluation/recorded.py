"""Reading runs that were already recorded, so a question about them costs no model call.

**Why this module exists.** Every Phase 7 driver so far answers its question by *making* runs:
it starts a stack, posts cases at the live API, and records what came back. That is the right
shape for a question about how the platform behaves, and the wrong shape for a question about
runs that have already happened -- what path a run took, which sub-agents actually ran, whether
a repeat disagreed with its sibling. Those were recorded once, in ``agent_runs`` and
``run_events``. Re-asking by running the platform again would pay a model provider for an
answer already on disk, and would move the revision that answer describes.

**The join, and why it is the only safe one.** ``agent_runs`` carries no revision -- its columns
are the run's own identity, goal, status, result and timestamps -- so a reader that selected rows
by ``created_at`` would be pooling revisions it cannot see, and every metric over that pool would
describe "the platform, at some point". The replay files do carry a revision, and they carry the
``run_id`` that revision belongs to. The replay is therefore the *attribution* and ``run_id`` is
the key. This module reads revisions out of replays and never guesses one; a replay naming no run
is skipped rather than reported with a null join.

**What it does not do.** It writes nothing, decides nothing, and applies no threshold. It hands
back rows; whether a distribution is acceptable belongs to whoever reads the report.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import sqlalchemy as sa

#: The batch header both the gates and this module skip: it names the batch, not an observation.
BATCH_HEADER = "_batch.json"

#: RLS is enabled on both tables this module reads, so a tenant has to be named. Session-level
#: rather than transaction-local: the engine autocommits a bare ``execute``, and a local setting
#: is silently discarded with the transaction that carried it, leaving the query to return zero
#: rows for runs that exist. That failure is invisible -- an empty result looks like "no runs".
TENANT_SETTING = "app.tenant_id"


def load_replays(directory: Path) -> list[dict[str, Any]]:
    """The observations in ``directory``, in file order.

    An observation is a file that is not the batch header and that names a ``run_id``.
    """
    if not directory.is_dir():
        raise FileNotFoundError(f"no replay directory at {directory}")
    observations: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        if path.name == BATCH_HEADER:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("run_id"):
            continue
        data["_replay"] = path.name
        observations.append(data)
    return observations


def revisions(replays: Iterable[dict[str, Any]]) -> dict[str, int]:
    """How many observations name each deployed revision, most-recorded first."""
    counted: dict[str, int] = {}
    for replay in replays:
        name = str(replay.get("deployed_revision") or "<not recorded>")
        counted[name] = counted.get(name, 0) + 1
    return dict(sorted(counted.items(), key=lambda item: (-item[1], item[0])))


@dataclass(frozen=True)
class RecordedRun:
    """One run as it was recorded: the replay that attributes it, and the rows behind it.

    ``calls`` is the run's ``model_invocations`` ledger rather than anything read out of
    ``result``. The distinction matters for two fields that look alike and are not: the
    ledger's ``retries`` is the provider-level retry count, while the per-agent ``attempts``
    inside an agent envelope is that agent's own aggregate and is summed across tool calls by
    the DataAgent (``agents/data.py``), so it is not comparable between agents and cannot be
    read as "did this step retry".
    """

    replay: dict[str, Any]
    run_id: str
    case_id: str | None
    revision: str | None
    status: str | None
    goal: str | None
    request_write: bool | None
    result: dict[str, Any]
    events: tuple[tuple[int, str, dict[str, Any]], ...]
    calls: tuple[dict[str, Any], ...] = ()

    @property
    def timeline(self) -> list[str]:
        """Event names in the order the platform emitted them, by ``run_events.sequence``."""
        return [event_type for _, event_type, _ in self.events]

    def payloads(self, event_type: str) -> list[dict[str, Any]]:
        """Every payload for one event name, in emission order."""
        return [payload for _, name, payload in self.events if name == event_type]

    def field(self, name: str) -> Any:
        """One key of ``agent_runs.result``, or ``None`` when the run did not record it."""
        return self.result.get(name)


@dataclass(frozen=True)
class RecordedCorpus:
    """A batch of replays joined to the runs they describe, with the joins that failed."""

    runs: tuple[RecordedRun, ...]
    replays: tuple[dict[str, Any], ...]
    unresolved: tuple[str, ...]

    @property
    def revision_counts(self) -> dict[str, int]:
        return revisions(self.replays)


def _as_mapping(value: Any) -> dict[str, Any]:
    """A JSON column as a mapping. psycopg hands back ``json`` as a dict, but not always."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    return {}


def read_corpus(engine: sa.Engine, directory: Path, *, tenant_id: UUID | str) -> RecordedCorpus:
    """``directory``'s replays, joined to their recorded runs and timelines.

    Two queries for the whole batch rather than two per replay: the corpora this reads are
    hundreds of runs, and a per-run round trip would make the join the slowest part of a
    report whose entire point is that it is cheaper than re-running anything.
    """
    replays = load_replays(directory)
    wanted = [str(replay["run_id"]) for replay in replays]
    if not wanted:
        return RecordedCorpus(runs=(), replays=tuple(replays), unresolved=())

    with engine.connect() as connection:
        connection.execute(
            sa.text(f"select set_config('{TENANT_SETTING}', :tenant, false)"),
            {"tenant": str(tenant_id)},
        )
        rows = connection.execute(
            sa.text(
                "select id::text, status, goal, request_write, result "
                "from agent_runs where id = any(:ids)"
            ),
            {"ids": wanted},
        ).all()
        events = connection.execute(
            sa.text(
                "select run_id::text, sequence, event_type, payload from run_events "
                "where run_id = any(:ids) order by run_id, sequence"
            ),
            {"ids": wanted},
        ).all()
        calls = connection.execute(
            sa.text(
                "select run_id::text, agent_role, purpose, status, error_code, attempts, "
                "retries, input_tokens, output_tokens, latency_ms, model "
                "from model_invocations where run_id = any(:ids) order by run_id, created_at"
            ),
            {"ids": wanted},
        ).all()

    by_run: dict[str, list[tuple[int, str, dict[str, Any]]]] = {}
    for run_id, sequence, event_type, payload in events:
        by_run.setdefault(run_id, []).append((int(sequence), str(event_type), _as_mapping(payload)))

    by_call: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        by_call.setdefault(call[0], []).append(
            {
                "agent_role": call[1],
                "purpose": call[2],
                "status": call[3],
                "error_code": call[4],
                "attempts": call[5],
                "retries": call[6],
                "input_tokens": call[7],
                "output_tokens": call[8],
                "latency_ms": call[9],
                "model": call[10],
            }
        )

    found = {row[0]: row for row in rows}
    runs: list[RecordedRun] = []
    unresolved: list[str] = []
    for replay in replays:
        run_id = str(replay["run_id"])
        row = found.get(run_id)
        if row is None:
            unresolved.append(run_id)
            continue
        runs.append(
            RecordedRun(
                replay=replay,
                run_id=run_id,
                case_id=replay.get("case_id"),
                revision=replay.get("deployed_revision"),
                status=row[1],
                goal=row[2],
                request_write=row[3],
                result=_as_mapping(row[4]),
                events=tuple(by_run.get(run_id, ())),
                calls=tuple(by_call.get(run_id, ())),
            )
        )
    return RecordedCorpus(runs=tuple(runs), replays=tuple(replays), unresolved=tuple(unresolved))


def inputs_block(
    directory: Path,
    replays: Iterable[dict[str, Any]],
    *,
    tool_revision: str | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What this report was computed *from* and *by*, as two revisions and not one.

    The distinction is not pedantry. ``scripts/`` is source -- ``NON_SOURCE_PREFIXES`` exempts
    only ``evaluation/`` and ``docs/`` -- so a scorer written after a batch fingerprints
    differently from the tree that produced the batch. Naming both keeps the reader from
    reading the scorer's revision as the platform's.
    """
    observations = list(replays)
    block: dict[str, Any] = {
        "replay_directory": str(directory),
        "observations": len(observations),
        "data_revisions": revisions(observations),
        "scorer_revision": tool_revision,
    }
    if extra:
        block.update(extra)
    return block
