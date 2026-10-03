"""Whether the process answering requests is the code in the tree.

**The failure this exists to catch.** ``src`` is installed into the venv in editable mode,
so an edit is on disk the moment it is saved -- and a running process, holding the modules
it imported at startup, keeps answering with the old ones. Every live batch stamps each
observation with ``source_revision``, and that stamp describes the *tree*. It says nothing
about whether the tree is what answered. The two are usually the same and, when they are
not, nothing about the finished batch looks wrong: a stale process answers every request
competently, the sweep completes, and every replay carries the revision of code the
platform never ran.

That is not hypothetical here. The acceptance batch found it by accident -- a sweep ran
while ``orchestration/`` was being edited and observed pre-fix behaviour under a post-fix
revision (``scripts/verify_phase7_acceptance_live.py``, ``stale_deployment``) -- and grew a
guard that refuses by default. The other five live batches (quality, security, load,
reliability, coordination) were written around the same driver and never got one: measured
on 2026-10-02, only ``verify_phase7_acceptance_live`` mentioned staleness at all. So the
guard lives here, once, and every batch calls it before it observes anything.

**Why the comparison is a process start time and not a pid or a build stamp.** The question
is only ever "was this process started before the newest source file was written", which is
answerable from ``/proc/<pid>/stat`` and the file's mtime. The start time comes from the
boot clock plus the process's own start tick rather than from systemd's formatted
timestamp, because the latter is rendered in the machine's locale and this value has to be
compared with an mtime, not read by a person.

**When it cannot tell.** If the unit is not running, has no main pid, or ``/proc`` is not
readable, this reports nothing rather than everything: a batch that cannot see the serving
process must not claim it is fresh. The batch's own health check is what establishes that
something is answering.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

#: The user unit that serves the platform. Named here rather than in each batch because a
#: second name would be a second answer to "which process did this batch grade".
DEFAULT_UNIT = "servicemind-api"

#: Returned by :func:`refuse_stale_deployment` when the serving process may be graded.
EXIT_OK = 0

#: Returned when it may not. The same configuration-error code the gates use for "the
#: inputs do not describe what this run claims to describe", deliberately not a verdict.
EXIT_CONFIGURATION = 3


def unit_main_pid(unit: str = DEFAULT_UNIT) -> int | None:
    """The unit's main pid, or ``None`` when systemd cannot name one."""
    completed = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "MainPID"],
        capture_output=True,
        text=True,
        check=False,
    )
    parts = completed.stdout.strip().split("=", 1)
    if len(parts) != 2 or not parts[1].strip().isdigit():
        return None
    return int(parts[1])


def unit_started_at(unit: str = DEFAULT_UNIT) -> str | None:
    """The unit's ``ActiveEnterTimestamp`` as systemd renders it, for a report header.

    For people, not for comparisons -- see :func:`process_started_at` for the machine
    readable counterpart.
    """
    completed = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "ActiveEnterTimestamp"],
        capture_output=True,
        text=True,
        check=False,
    )
    parts = completed.stdout.strip().split("=", 1)
    return parts[1].strip() if len(parts) == 2 and parts[1].strip() else None


def process_started_at(pid: int) -> float | None:
    """When a process began, as a Unix timestamp."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        ticks = int(fields[21])
    except (OSError, IndexError, ValueError):
        return None
    boot = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
    return boot + ticks / os.sysconf("SC_CLK_TCK")


def newest_source(repo_root: Path) -> tuple[float, Path] | None:
    """The most recently written ``*.py`` under ``src``, or ``None`` if there is none."""
    sources = (repo_root / "src").rglob("*.py")
    newest: tuple[float, Path] | None = None
    for path in sources:
        try:
            mtime = path.stat().st_mtime
        except OSError:  # pragma: no cover -- a file that vanished mid-scan
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, path)
    return newest


def stale_deployment(
    repo_root: Path, *, unit: str = DEFAULT_UNIT, pid: int | None = None
) -> dict[str, Any] | None:
    """Whether the serving process predates the code in the tree, and by how much.

    ``None`` means either "it does not" or "this could not be determined". A caller that
    needs the second case to be a refusal must say so itself; every batch here treats both
    the same way, because the batch's health check is what proves something is serving.

    ``pid`` names the process to ask about directly, for a batch that started its own
    instance rather than measuring whichever one the unit happens to be running. Without it
    the answer comes from the unit, which is right for a batch pointed at the deployment.
    """
    served_by = unit_main_pid(unit) if pid is None else pid
    started = None if served_by is None else process_started_at(served_by)
    if started is None:
        return None
    newest = newest_source(repo_root)
    if newest is None:  # pragma: no cover -- a checkout with no sources is not one
        return None
    newest_mtime, newest_path = newest
    if newest_mtime <= started:
        return None
    finding = {
        "unit": unit,
        "unit_main_pid": served_by,
        "unit_started_epoch": round(started, 1),
        "newest_source": str(newest_path.relative_to(repo_root)),
        "newest_source_epoch": round(newest_mtime, 1),
        "seconds_behind": round(newest_mtime - started, 1),
        "remedy": f"systemctl --user restart {unit}, then re-run",
    }
    if pid is not None:
        # A process the caller started is not the unit's to restart, and a reader told to
        # restart the unit over a finding about some other process would be misled.
        finding["served_by"] = "caller-supplied pid"
        finding["remedy"] = f"restart pid {pid}, then re-run"
    return finding


def refuse_stale_deployment(
    repo_root: Path,
    *,
    unit: str = DEFAULT_UNIT,
    pid: int | None = None,
    allow: bool = False,
    argv: Sequence[str] = (),
) -> int:
    """``0`` when the serving process may be graded, ``3`` when it may not.

    Refuses by default rather than warning, because this is the failure that does not
    announce itself: the batch completes, the replays are written, and the only thing wrong
    with them is invisible in every field they carry. ``allow`` exists for the one honest
    use -- observing deliberately old code and saying so -- and the finding is printed
    either way.
    """
    finding = stale_deployment(repo_root, unit=unit, pid=pid)
    if finding is None:
        return EXIT_OK
    finding = {**finding, "argv": list(argv)} if argv else finding
    if not allow:
        print(json.dumps({"stale_deployment": finding}, ensure_ascii=False, indent=2))
        return EXIT_CONFIGURATION
    print(json.dumps({"stale_deployment": finding, "recording_anyway": True}, ensure_ascii=False))
    return EXIT_OK


__all__ = [
    "DEFAULT_UNIT",
    "EXIT_CONFIGURATION",
    "EXIT_OK",
    "newest_source",
    "process_started_at",
    "refuse_stale_deployment",
    "stale_deployment",
    "unit_main_pid",
    "unit_started_at",
]
