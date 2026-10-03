"""Fail CI when a Phase-7 gate's committed report contradicts the gate's own exit code.

On 2026-10-01 ``evaluation/reports/phase7_quality_latest.json`` said ``PASS`` while
``scripts/gate_phase7_quality.py --check`` refused the same observations with exit 3. Both
artefacts were on disk, both were written by the platform, and they disagreed -- so a
reader who opened the report concluded the opposite of the reader who ran the gate.

The four gates now stamp a refusal over a report they will not stand behind, which fixes
the instance. This script is what stops the class: it re-runs each gate offline into a
scratch directory (the committed reports are never touched), maps the exit code to the
verdict the report must then carry, and checks the committed report against the verdict
the gate produces today. A committed report that has drifted away from its gate fails
here rather than being read as fact.

**What a green run does not say.** The verdicts below are reached by grading replays on
disk, and every replay names the platform revision that produced it. The script prints
those revisions for exactly this reason: on 2026-10-01 the acceptance gate exited 0 over
28 replays all recorded at ``cf08ac8+...``, while ``HEAD`` was five commits later, and
nothing in the exit code distinguished the two readings "the current tree passes" and
"these are mutually consistent and the grader still accepts them". Homogeneity is always
checked -- by the gates, over the observations themselves. Currency is not, and cannot
be: an offline checkout grading files has no way to know whether those files describe the
code beside them. ``--expect-revision`` is how a caller supplies that, and the run reports
``currency_checked`` either way, so a reader is never left to infer it.

Exit codes: 0 every gate agrees with its report, 1 something disagrees.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATES = ("acceptance", "quality", "security", "load")

#: Printed above the list of revisions every run, so the number a reader is most likely to
#: over-read -- an exit code -- is never separated from what it was computed over.
CURRENCY_HEADING = "**What this run graded.** Each verdict above was computed from observations naming these source revisions:"

#: The sentence that keeps "the gate exited 0" from being read as "the current tree passes".
CURRENCY_UNCHECKED = (
    "Currency was **not** checked: nothing here compares a graded revision against the tree "
    "you are reading. A green run means the gates and the committed reports agree about the "
    "observations on disk, and it does **not** mean the current checkout passes acceptance -- "
    "re-record against the deployment you mean to describe, or pass --expect-revision to make "
    "that a checked fact."
)

#: What the gate's exit code asserts. ``2`` is "not enough was observed to judge" and
#: ``3`` is "this gate will not judge", which the report records as a refusal.
VERDICT_FOR_EXIT = {0: "PASS", 1: "FAIL", 2: "INSUFFICIENT", 3: "REFUSED"}


@dataclass(frozen=True)
class Verdict:
    gate: str
    exit_code: int
    fresh: str | None
    committed: str | None
    committed_path: Path
    #: The source revisions the gate graded, as recorded in its own summary. Empty when the
    #: gate refused before it could attribute anything, or when its stdout was not the
    #: summary at all (a crash) -- both of which the exit code already reports.
    revisions: tuple[str, ...] = ()

    @property
    def agrees(self) -> bool:
        """Whether the exit code, the fresh report and the committed report say one thing.

        A missing committed report is **not** agreement. ``_read_verdict`` returns
        ``None`` for a file that is gone and for one that will not parse, so accepting
        ``None`` made deleting the artefact a way to pass: the fresh run agrees with
        itself and there is nothing left to disagree with it. That is the same defect
        this tool exists to catch -- "the report says PASS while the gate refused" -- in
        the one case where the report is absent, and it is the case a reader is least
        able to see.
        """
        return (
            self.committed is not None
            and self.fresh == VERDICT_FOR_EXIT.get(self.exit_code)
            and self.committed == self.fresh
        )

    @property
    def committed_is_missing(self) -> bool:
        return self.committed is None


def _verdict_of(document: object) -> str | None:
    if isinstance(document, dict):
        verdict = document.get("verdict")
        return verdict if isinstance(verdict, str) else None
    return None


def _read_verdict(path: Path) -> str | None:
    try:
        return _verdict_of(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def _graded_revisions(stdout: str) -> tuple[str, ...]:
    """The revisions a gate says it graded, read back from its own summary.

    The gates print that summary as JSON when they reach a verdict and print nothing when
    they refuse, so anything unparseable means "this gate did not attribute its verdict" --
    which is what an empty tuple records here, and which the exit code speaks to separately.
    """
    try:
        summary = json.loads(stdout)
    except json.JSONDecodeError:
        return ()
    if not isinstance(summary, dict):
        return ()
    revisions = summary.get("deployed_revisions")
    if not isinstance(revisions, list):
        return ()
    return tuple(name for name in revisions if isinstance(name, str))


def judge(gate: str, scratch: Path, *, expect_revision: str | None = None) -> Verdict:
    report = scratch / f"{gate}.json"
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / f"gate_phase7_{gate}.py"),
        "--check",
        "--replay-only",
        "--format",
        "json",
        "--report",
        str(report),
    ]
    if expect_revision is not None:
        command += ["--expect-revision", expect_revision]
    process = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)
    committed_path = REPO_ROOT / "evaluation" / "reports" / f"phase7_{gate}_latest.json"
    return Verdict(
        gate=gate,
        exit_code=process.returncode,
        fresh=_read_verdict(report),
        committed=_read_verdict(committed_path),
        committed_path=committed_path,
        revisions=_graded_revisions(process.stdout),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--format", default="markdown", choices=("markdown", "json"), help="output format"
    )
    parser.add_argument(
        "--expect-revision",
        default=None,
        help=(
            "the source revision the graded observations must describe. Homogeneity is "
            "checked either way; without this the run reports currency_checked=false and "
            "says nothing about whether the current checkout passes"
        ),
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="phase7-gate-reports-") as directory:
        verdicts = [
            judge(gate, Path(directory), expect_revision=args.expect_revision) for gate in GATES
        ]

    disagreements = [verdict for verdict in verdicts if not verdict.agrees]
    if args.format == "json":
        payload = {
            "gates": [
                {
                    "gate": verdict.gate,
                    "exit_code": verdict.exit_code,
                    "expected_verdict": VERDICT_FOR_EXIT.get(verdict.exit_code),
                    "report_verdict": verdict.fresh,
                    "committed_verdict": verdict.committed,
                    "agrees": verdict.agrees,
                    "deployed_revisions": list(verdict.revisions),
                }
                for verdict in verdicts
            ],
            "agreements": len(verdicts) - len(disagreements),
            "disagreements": len(disagreements),
            "currency_checked": args.expect_revision is not None,
            "expect_revision": args.expect_revision,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("| gate | exit | verdict the gate must report | reported | committed | agrees |")
        print("|---|---|---|---|---|---|")
        for verdict in verdicts:
            print(
                f"| {verdict.gate} | {verdict.exit_code} | "
                f"{VERDICT_FOR_EXIT.get(verdict.exit_code, '?')} | {verdict.fresh} | "
                f"{verdict.committed} | {'yes' if verdict.agrees else 'NO'} |"
            )
        print()
        print(CURRENCY_HEADING)
        print()
        for verdict in verdicts:
            graded = ", ".join(verdict.revisions) if verdict.revisions else "<nothing graded>"
            print(f"- {verdict.gate}: {graded}")
        print()
        if args.expect_revision is None:
            print(CURRENCY_UNCHECKED)
        else:
            print(
                f"Currency was checked: every verdict above must describe {args.expect_revision}."
            )
        for verdict in disagreements:
            print(
                f"\n{verdict.gate}: exited {verdict.exit_code}, which requires the report to say "
                f"{VERDICT_FOR_EXIT.get(verdict.exit_code)}, but the report it just wrote says "
                f"{verdict.fresh} and {verdict.committed_path} says "
                f"{'nothing (missing or unreadable)' if verdict.committed_is_missing else verdict.committed}."
            )
    return 1 if disagreements else 0


if __name__ == "__main__":
    raise SystemExit(main())
