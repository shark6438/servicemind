"""Revert each fix in turn and require the suite to notice.

A test that passes both with and without the behaviour it claims to protect has no
teeth, and this repository has produced exactly that before -- an earlier version of the
Phase 7 principal tests asserted on ``configurable``, which nothing reads, so they were
green while the narrowing they described was being discarded.

The machinery lives here rather than in each experiment because the failure modes are
subtle and identical: a tree left dirty by a signal, two runs interleaving at file
granularity, an anchor that no longer matches because the code moved. Each of those was
found the hard way in the first experiment, and re-deriving them per script is how a
later one quietly loses the protection. What differs between experiments is the list of
mutations, which is all a caller supplies.

The rest of this docstring is the record of why each guard exists; it was written
against the first experiment and applies to every one that reuses this harness.

Equivalent mutants are called out where they were found, because an equivalent mutant
that reads RED is a false sense of safety and an equivalent mutant left GREEN looks
like a hole. The fourth reading -- RED because nothing ran -- is graded as a failure
rather than as a detection, for the same reason: a run that never reached an assertion
is not evidence about the assertion. See ``UNRUN`` in ``run_mutations``.
"""

from __future__ import annotations

import ast
import atexit
import json
import os
import signal
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Mutation:
    """One reverted decision, and the tests that are supposed to notice it went away."""

    name: str
    path: Path
    old: str
    new: str
    tests: tuple[str, ...]


def _describe(mutation: Mutation, root: Path) -> str:
    return mutation.path.relative_to(root).as_posix()


def run_mutations(
    *,
    root: Path,
    experiment: str,
    mutations: Sequence[Mutation],
    pytest_args: Sequence[str] = (),
) -> int:
    """Run every mutation, restore the tree, and report which ones went unnoticed.

    ``experiment`` names the journal and lock, so two experiments do not share them --
    but it must stay stable across runs of the same experiment, or an interrupted run's
    journal is orphaned at the path the next run does not look at.

    ``pytest_args`` is passed to every inner run. An experiment whose teeth live in a
    docker-gated test must pass ``--run-docker`` here: without it the test *skips*, the
    run exits 0, and the harness grades every mutation as undetected -- which reads as
    "this experiment found four holes" when the hole is in the invocation. Skipped is
    not detected, so the harness also treats a run whose summary is all skips as not
    having exercised anything.
    """
    runtime = root / ".runtime"
    #: Where the pristine snapshot of the last run is kept. Written before the first
    #: mutation and read before the next run, so a tree left dirty by ``SIGKILL`` -- the
    #: one signal no handler can catch -- is reported rather than graded.
    #: ``.runtime/`` rather than the repo root: it is already ignored, so the snapshot
    #: neither shows up as a working-tree change nor is read as a scaffold file by
    #: ``scripts/audit_project_structure.py``.
    manifest = runtime / f"mutate_{experiment}.baseline.json"
    lock = runtime / f"mutate_{experiment}.lock"

    #: Pristine text of every file this run mutates, taken once before the first write.
    #: A mutation experiment that dies mid-flight leaves a mutated tree behind, and a
    #: mutated tree is a *silently wrong* tree: the next test run grades the mutation
    #: rather than the fix. An earlier version restored only in a ``finally``, which a
    #: ``SIGTERM`` from a supervising timeout skips entirely -- and that is how a
    #: reversal of the entitlement gate survived in ``orchestration/runtime.py``. So the
    #: snapshot is taken up front and restored from an ``atexit`` hook *and* from the
    #: signal handlers, which together cover every exit except ``SIGKILL``; that last
    #: hole is closed by the journal ``_reconcile_interrupted_run`` reads.
    pristine: dict[Path, str] = {}

    def restore_all() -> None:
        for path, text in pristine.items():
            if path.read_text() != text:
                path.write_text(text)
        # Without this the journal would outlive the run it describes and the next
        # start would report a crash that has already been undone.
        manifest.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)

    def acquire_lock() -> None:
        """Refuse to run while another copy of this experiment holds the tree.

        Two runs interleave at file granularity: each snapshots a target, writes its own
        mutation, and restores *that* snapshot -- so the second run's restore can
        reinstate the first run's mutation, or erase it, depending on which one lands
        last. Neither run can detect this from the results, because both are comparing
        against a file the other one is rewriting. The tree then ends up somewhere
        neither run intended, which is how a leftover mutation and a *silently reverted
        fix* both appeared at once.
        """
        if lock.exists():
            holder = lock.read_text().strip()
            if holder.isdecimal() and Path(f"/proc/{holder}").exists():
                raise SystemExit(
                    f"another mutation run is in flight (pid {holder}); refusing to interleave"
                )
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(os.getpid()))

    def reconcile_interrupted_run() -> None:
        """Undo a mutation that a previous run never got to revert.

        The journal is written immediately before a file is mutated and deleted
        immediately after it is restored, so its *presence* means a run died with a
        mutation in the tree. ``SIGKILL`` -- and the ``timeout(1)`` that sends it -- is
        uncatchable, so this is the only way that window is ever closed; it is what the
        earlier reversal of the entitlement gate survived through.

        The three cases are distinguished by content rather than assumed: equal to the
        pristine text means the run actually finished and only the journal is stale;
        equal to the mutated text means the crash is real and is undone here; anything
        else means the file was edited on purpose after the crash, and is left alone.
        """
        if not manifest.exists():
            return
        record = json.loads(manifest.read_text())
        manifest.unlink()
        path = Path(root, record["path"])
        current = path.read_text()
        if current == record["pristine"]:
            return
        if current == record["mutated"]:
            path.write_text(record["pristine"])
            print(
                f"NOTE: {record['path']} carried an unreverted mutation "
                f"({record['name']}); it has been restored.",
                file=sys.stderr,
            )
            return
        print(
            f"NOTE: {record['path']} changed after the interrupted run "
            f"({record['name']}); leaving it untouched.",
            file=sys.stderr,
        )

    def install_restore_handlers() -> None:
        atexit.register(restore_all)

        def handler(signum, _frame):  # noqa: ANN001 - signal handlers take the frame raw
            restore_all()
            print(f"\nreceived signal {signum}; restored the working tree", file=sys.stderr)
            raise SystemExit(1 if signum != signal.SIGINT else 130)

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    for path in sorted({mutation.path for mutation in mutations}):
        pristine[path] = path.read_text()
    # The anchors are checked before any handler is installed: a missing anchor is a
    # stale script, not a detection result, and it must not be reported as one.
    # All of them are reported, not just the first: a code change that moves one anchor
    # usually moves its neighbours, and stopping at the first turns repairing this into
    # one run per anchor with a full mutation sweep in between.
    stale = [
        f"  {mutation.name} -- anchor not found in {_describe(mutation, root)}"
        for mutation in mutations
        if mutation.old not in pristine[mutation.path]
    ]
    if stale:
        raise SystemExit(
            f"{len(stale)} stale mutation(s); repair the anchors before grading:\n"
            + "\n".join(stale)
        )
    # The same reasoning one step further in: a replacement that does not parse is a
    # broken mutation rather than a detection, and pytest would report it as ``1 error``
    # with a non-zero exit -- indistinguishable, in the summary line, from a test that
    # failed. Caught here so it names the cause and costs no test runs.
    unparseable = []
    for mutation in mutations:
        if mutation.path.suffix != ".py":
            continue
        mutated = pristine[mutation.path].replace(mutation.old, mutation.new, 1)
        try:
            ast.parse(mutated, filename=str(mutation.path))
        except SyntaxError as exc:
            unparseable.append(
                f"  {mutation.name} -- {exc.msg} at line {exc.lineno} of the mutated "
                f"{_describe(mutation, root)}"
            )
    if unparseable:
        raise SystemExit(
            f"{len(unparseable)} mutation(s) do not parse; a syntax error is not a "
            f"detection (no assertion runs), so repair the replacement before grading:\n"
            + "\n".join(unparseable)
        )
    runtime.mkdir(parents=True, exist_ok=True)
    install_restore_handlers()
    reconcile_interrupted_run()
    acquire_lock()

    failures: list[str] = []
    unrunnable_names: list[str] = []
    for mutation in mutations:
        original = pristine[mutation.path]
        mutated = original.replace(mutation.old, mutation.new, 1)
        manifest.write_text(
            json.dumps(
                {
                    "name": mutation.name,
                    "path": str(mutation.path.relative_to(root)),
                    "pristine": original,
                    "mutated": mutated,
                }
            )
        )
        mutation.path.write_text(mutated)
        try:
            proc = subprocess.run(
                [
                    "uv",
                    "run",
                    "pytest",
                    *mutation.tests,
                    "-q",
                    "--no-header",
                    "-p",
                    "no:randomly",
                    *pytest_args,
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=1800,
            )
            tail = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()][-1]
        finally:
            mutation.path.write_text(original)
            manifest.unlink(missing_ok=True)
        detected = proc.returncode != 0
        # A green run that only skipped graded nothing: the behaviour the mutation removed
        # was never reached, so "the suite did not notice" would be the wrong reading --
        # there was no suite. Reported separately, because it is a defect in the invocation
        # (usually a missing ``--run-docker``) rather than a hole in the tests.
        unexercised = (not detected) and " skipped" in tail and " passed" not in tail
        # A red run in which no test ever ran is not a detection either. Removing a line
        # can leave a block empty, which is a syntax error, and pytest then reports
        # ``1 error`` from collection -- exit code 1, "RED", and exactly as uninformative
        # as a green run, because the assertion that was supposed to notice the behaviour
        # was never executed. That reads as the strongest possible evidence ("the suite
        # fails loudest without this line") while proving nothing, which is the same false
        # sense of safety as an equivalent mutant that reads RED. The mutation has to be
        # repaired into a semantic change; until it is, the experiment has not graded it.
        unrunnable = detected and " passed" not in tail and " failed" not in tail
        if unexercised:
            status = "UNEXERCISED (all skipped)"
        elif unrunnable:
            status = "UNRUN (no test ran)"
        else:
            status = "RED (good)" if detected else "GREEN (BAD - no teeth)"
        print(f"{status:28} | {mutation.name}\n{'':28} | {tail}")
        if not detected or unrunnable:
            failures.append(mutation.name)
        if unrunnable:
            unrunnable_names.append(mutation.name)

    print()
    verdicts = []
    if unrunnable_names:
        verdicts.append(f"UNRUNNABLE: {unrunnable_names}")
    undetected = [name for name in failures if name not in unrunnable_names]
    if undetected:
        verdicts.append(f"UNDETECTED: {undetected}")
    print(f"{len(mutations)} mutations; " + ("; ".join(verdicts) if verdicts else "all detected"))
    return 1 if failures else 0
