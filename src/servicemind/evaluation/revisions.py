"""Which revision of the platform a batch of observations describes, and whether it is one.

Every gate in this phase grades a directory of replays, and those replays are the entire
offline statement about the platform: the verdict is reached by reading them and nothing
else. Each observation has carried ``deployed_revision`` since the drivers were written --
the security and load graders even compute the distinct set and render it in their reports
-- but no gate ever asked whether the set had more than one member, so the number was a
footnote rather than a precondition.

Measured on 2026-09-30, that is not hypothetical. ``evaluation/quality/replays`` held 200
observations across four source revisions (plus ``_batch.json``, the batch header, which
the loaders skip and which is therefore not one of them):

    148  @ 7a6e675+dirty(121)    the bulk batch
     37  @ 7a6e675+dirty(120)    the same commit, one recording run later
     11  @ cf08ac8+dirty(56)     two commits later
      2  @ 688dd90+dirty(131)    two commits earlier
      1  @ cf08ac8+dirty(8)      the same commit as the eleven above
      1  @ cf08ac8+dirty(69)     the same commit as the twelve above

The quality gate graded that corpus ``PASS`` with exit 0. A verdict computed across four
platforms is a verdict about none of them, and the gate could not tell the difference
between "the deployed platform passes" and "some platform passed once" -- which is the one
distinction the whole offline half exists to make.

The two ``7a6e675`` entries are not two working trees, which is what an earlier version of
this docstring called them. They are one source tree recorded twice, and the count moved
because those runs wrote their evidence between the two readings. Every ``dirty(N)`` in the
list above is a count of files in ``git status``, most of which are the replays the recording
process itself had just rewritten. That made the revision string a function of the act of
recording it, which is why the check above could never have been satisfied by re-running
anything; the string is now a fingerprint of the source tree, and the defect is written up in
:mod:`servicemind.evaluation.source_revision`. The revision strings above are kept as
recorded -- they are what the corpus says -- and are replaced by the next full recording.

Two rules, checked separately because they fail for different reasons and call for
different actions:

* **Homogeneity** is answerable from the observations alone. A batch spanning revisions is
  not a measurement of anything, whatever the revisions are. No external input is needed,
  so this is always checked.
* **Currency** needs to be told what the current revision is, which an offline CI run on a
  bare checkout cannot work out. ``--expect-revision`` supplies it; a run that does not
  supply it checks homogeneity only, and the report records what was actually recorded so
  the reader can compare.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

#: How a missing revision is counted and named. Not the empty string, which is what a
#: caller who recorded ``""`` would leave behind and which would otherwise be sorted in
#: among the real revisions as though it named one.
UNRECORDED = "<not recorded>"


def _normalise(revision: str | None) -> str:
    """One recorded revision as it is counted and named. See ``UNRECORDED``."""
    return UNRECORDED if revision is None or revision == "" else str(revision)


def revision_problems(revisions: Iterable[str | None], *, expect: str | None = None) -> list[str]:
    """The reasons this batch cannot be attributed to a single, expected platform revision.

    Returns one sentence per problem so a gate can raise its own error type over them and
    keep the wording of its own report. An empty list means the batch describes exactly one
    revision and, if ``expect`` was given, that revision is the expected one.

    An empty batch returns no problems: "nothing was observed" is what the gates' own
    insufficiency rules are for, and reporting it here as a provenance failure would give
    it the wrong exit code.
    """
    counted: Counter[str] = Counter(_normalise(revision) for revision in revisions)
    if not counted:
        return []

    recorded = sorted(name for name in counted if name != UNRECORDED)
    problems: list[str] = []
    if len(recorded) > 1:
        tally = ", ".join(f"{name} x{counted[name]}" for name in recorded)
        problems.append(
            f"the observations describe {len(recorded)} different source revisions ({tally}), "
            "so their verdict is about the platform only in the sense that each observation "
            "was about one; re-run the batch against a single deployment"
        )
    if counted[UNRECORDED]:
        problems.append(
            f"{counted[UNRECORDED]} observation(s) do not record the source revision they were "
            "taken against, so nothing here says which platform they describe"
        )
    if expect is not None:
        disagreeing = [name for name in recorded if name != expect]
        if disagreeing:
            problems.append(
                f"the observations were taken against {', '.join(disagreeing)} but this run "
                f"expects {expect}"
            )
    return problems


def recorded_revisions(revisions: Iterable[str | None]) -> tuple[str, ...]:
    """The distinct source revisions a batch describes, in a stable order.

    Named separately from ``revision_problems`` so a report can state what was recorded
    even on the path where nothing is refused -- a reader comparing a report against a
    working tree needs the revision in it, not merely a promise that there was only one.

    ``UNRECORDED`` sorts last. It is a placeholder, not a revision, and ``"<"`` sorts before
    every hexadecimal digit, so letting it sort naturally would put the one entry that names
    no revision at the head of the list a reader scans for the revision.
    """
    names = [_normalise(revision) for revision in revisions]
    named = sorted({name for name in names if name != UNRECORDED})
    return tuple(named + ([UNRECORDED] if UNRECORDED in names else []))
