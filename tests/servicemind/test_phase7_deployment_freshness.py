"""The serving process must be the code in the tree before a batch may observe it.

The failure this guards against leaves no trace in the evidence it produces: a process
running older code answers every request competently, so the batch completes and each
record carries the revision of a tree nothing was serving. These tests hold both halves --
the detector, and the fact that every live batch calls it. The second half is asserted by
reading the batch scripts, because the way this defect returns is a new batch being written
around the same driver without the guard, which no unit test of the detector would notice.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from servicemind.evaluation import deployment

REPO_ROOT = Path(__file__).resolve().parents[2]

HARNESS_SPEC = importlib.util.spec_from_file_location(
    "mutation_harness", REPO_ROOT / "scripts" / "mutation_harness.py"
)
assert HARNESS_SPEC is not None and HARNESS_SPEC.loader is not None
HARNESS = importlib.util.module_from_spec(HARNESS_SPEC)
# Registered before execution because the harness defines a ``slots`` dataclass, which
# resolves its own module through ``sys.modules`` while the class is being built.
sys.modules[HARNESS_SPEC.name] = HARNESS
HARNESS_SPEC.loader.exec_module(HARNESS)

#: Every live batch. A new one belongs in this list; that is the point of the list.
LIVE_BATCHES = (
    "verify_phase7_quality_live.py",
    "verify_phase7_security.py",
    "verify_phase7_load_live.py",
    "verify_phase7_reliability_live.py",
    "verify_phase7_coordination_live.py",
    "verify_phase7_acceptance_live.py",
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A checkout whose single source file is a known age."""
    source = tmp_path / "src" / "servicemind" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def test_fresh_process_is_not_stale(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deployment, "unit_main_pid", lambda unit=deployment.DEFAULT_UNIT: 42)
    monkeypatch.setattr(deployment, "process_started_at", lambda pid: 1e12)
    assert deployment.stale_deployment(tree) is None


def test_a_process_older_than_the_tree_is_reported(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deployment, "unit_main_pid", lambda unit=deployment.DEFAULT_UNIT: 42)
    monkeypatch.setattr(deployment, "process_started_at", lambda pid: 1.0)
    finding = deployment.stale_deployment(tree)
    assert finding is not None
    assert finding["seconds_behind"] > 0
    assert finding["unit_main_pid"] == 42
    assert deployment.DEFAULT_UNIT in finding["remedy"]
    # The finding has to name what moved, not merely that something did: a reader deciding
    # whether to restart needs to know which file the process never imported.
    assert finding["newest_source"].endswith("module.py")


def test_an_unreadable_process_is_not_claimed_fresh(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not knowing must not be reported as being current."""
    monkeypatch.setattr(deployment, "unit_main_pid", lambda unit=deployment.DEFAULT_UNIT: None)
    assert deployment.stale_deployment(tree) is None
    monkeypatch.setattr(deployment, "unit_main_pid", lambda unit=deployment.DEFAULT_UNIT: 42)
    monkeypatch.setattr(deployment, "process_started_at", lambda pid: None)
    assert deployment.stale_deployment(tree) is None


def test_newest_source_ignores_files_that_are_not_python(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "notes.md").write_text("newer\n", encoding="utf-8")
    (tmp_path / "src" / "code.py").write_text("x = 1\n", encoding="utf-8")
    import os

    os.utime(tmp_path / "src" / "notes.md", (2e9, 2e9))
    os.utime(tmp_path / "src" / "code.py", (1e9, 1e9))
    newest = deployment.newest_source(tmp_path)
    assert newest is not None
    assert newest[1].name == "code.py"


def test_an_empty_source_tree_is_not_a_verdict(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    assert deployment.newest_source(tmp_path) is None


def test_refusal_is_the_default_and_observation_is_opt_in(
    tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(deployment, "unit_main_pid", lambda unit=deployment.DEFAULT_UNIT: 42)
    monkeypatch.setattr(deployment, "process_started_at", lambda pid: 1.0)

    assert deployment.refuse_stale_deployment(tree) == deployment.EXIT_CONFIGURATION
    refused = json.loads(capsys.readouterr().out)
    assert refused["stale_deployment"]["unit_main_pid"] == 42
    assert "recording_anyway" not in refused

    assert deployment.refuse_stale_deployment(tree, allow=True) == deployment.EXIT_OK
    allowed = json.loads(capsys.readouterr().out)
    assert allowed["recording_anyway"] is True

    monkeypatch.setattr(deployment, "process_started_at", lambda pid: 1e12)
    assert deployment.refuse_stale_deployment(tree) == deployment.EXIT_OK


@pytest.mark.parametrize("script", LIVE_BATCHES)
def test_every_live_batch_refuses_a_stale_deployment(script: str) -> None:
    source = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
    assert "refuse_stale_deployment(" in source, (
        f"{script} observes the live platform without checking that the platform is the "
        "code in this tree; its records would carry this tree's revision while describing "
        "an older process"
    )
    assert "--allow-stale-deployment" in source


def test_a_named_process_is_asked_about_directly(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch running its own instance must be able to say which process answers it.

    The unit is not the only thing that serves this platform: a comparison that starts a
    second instance on another port would otherwise be checked against the first one's
    start time, and pass by looking at the wrong process.
    """
    asked: list[int] = []

    def fake_started(pid: int) -> float:
        asked.append(pid)
        return 1.0

    monkeypatch.setattr(deployment, "unit_main_pid", lambda unit=deployment.DEFAULT_UNIT: 1)
    monkeypatch.setattr(deployment, "process_started_at", fake_started)

    finding = deployment.stale_deployment(tree, pid=777)
    assert finding is not None
    assert asked == [777], "the named process must be the one inspected"
    assert finding["unit_main_pid"] == 777
    assert finding["remedy"] == "restart pid 777, then re-run"
    assert finding["served_by"] == "caller-supplied pid"

    # Without a pid the unit is still what is asked about, and the remedy still names it.
    assert deployment.stale_deployment(tree) is not None
    assert asked[-1] == 1
    assert deployment.stale_deployment(tree)["remedy"].startswith("systemctl --user restart")


# --- the other side of the same detector: a restore that does not look like a write ------


def test_a_mutation_restore_puts_the_timestamp_back_with_the_text(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restoring the bytes is not restoring the tree, and the difference is measurable.

    The harness mutates a source file and puts it back. Rewriting the original contents
    leaves a tree that diffs clean and still reads, to ``stale_deployment``, as newer than
    the process serving it -- so every live batch run after a mutation sweep refuses to
    observe anything, and the remedy it prints is wrong: nothing about the deployment
    changed. The write is what the detector sees, so undoing a mutation has to undo the
    write, not only its content.
    """
    started = 1.7e9
    monkeypatch.setattr(deployment, "unit_main_pid", lambda unit=deployment.DEFAULT_UNIT: 42)
    monkeypatch.setattr(deployment, "process_started_at", lambda pid: started)

    source = tree / "src" / "servicemind" / "module.py"
    aged = int(1.6e9 * 1e9)
    os.utime(source, ns=(aged, aged))
    assert deployment.stale_deployment(tree) is None

    text, stamp = HARNESS.snapshot(source)
    source.write_text("x = 2\n", encoding="utf-8")
    assert deployment.stale_deployment(tree) is not None, "the mutation must be visible"

    HARNESS.restore(source, text, stamp)

    assert source.read_text(encoding="utf-8") == text
    # The mtime is the whole of the invariant: it is the field the detector reads. The
    # access time is restored too but cannot be asserted here, because every read of the
    # file -- including the one in this line -- moves it forward again.
    assert source.stat().st_mtime_ns == stamp[1]
    assert deployment.stale_deployment(tree) is None


def test_the_journal_carries_the_stamp_for_the_crash_path() -> None:
    """``SIGKILL`` leaves the journal as the only record of the pre-mutation stamp.

    Asserted by reading the source because the recovery runs inside ``run_mutations`` and
    the state it repairs -- a tree a killed run left mutated -- cannot be reached from a
    unit test without one.
    """
    harness = (REPO_ROOT / "scripts" / "mutation_harness.py").read_text(encoding="utf-8")
    assert '"stamp": list(' in harness, "the journal no longer records the pre-mutation stamp"
    assert "os.utime(path, ns=(stamp[0], stamp[1]))" in harness, (
        "the crash recovery restores the bytes but leaves the file reading as freshly "
        "written, which is the state that makes the next live batch refuse"
    )
