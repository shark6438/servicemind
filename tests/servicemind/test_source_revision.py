"""The revision string the four gates compare names the code, not the act of recording it.

``deployed_revision`` is the only evidence that a batch of observations describes one
platform (``servicemind.evaluation.revisions``), so its discriminating power is the whole
provenance check. Until 2026-10-01 it was ``<sha>+dirty(N files)``, with ``N`` the line count
of ``git status --porcelain``. Because every driver writes its evidence into that same
working tree, recording changed the revision being recorded: one build went out as five
different revisions, and a batch of one platform could not be told from a batch of several.

These tests run a real repository rather than patching ``subprocess``, because the property
under test is what ``git`` reports over a tree that a test mutates -- and the mutation is the
point. A mock would assert that the function calls git in a certain order; only a real tree
can assert that writing a replay leaves the answer alone.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from servicemind.evaluation.source_revision import (
    DIGEST_LENGTH,
    NON_SOURCE_PREFIXES,
    changed_source_paths,
    fingerprint,
    source_revision,
)

MR_SHA = 40
PATCHED = re.compile(rf"^[0-9a-f]{{{MR_SHA}}}\+patch\([0-9a-f]{{{DIGEST_LENGTH}}}\)$")


def run(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A committed checkout with one source file, one replay directory and one doc.

    All three kinds are present from the start, so a test that changes any one of them is
    comparing against a tree that already contained the others.
    """
    run(tmp_path, "init", "--quiet")
    run(tmp_path, "config", "user.email", "test@example.invalid")
    run(tmp_path, "config", "user.name", "test")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "workflow.py").write_text("STEPS = 1\n", encoding="utf-8")
    (tmp_path / "evaluation" / "quality" / "replays").mkdir(parents=True)
    (tmp_path / "evaluation" / "quality" / "replays" / "Q-001.json").write_text(
        '{"attempt": 1}\n', encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "BASELINE.md").write_text("# baseline\n", encoding="utf-8")
    run(tmp_path, "add", "-A")
    run(tmp_path, "commit", "--quiet", "-m", "base")
    return tmp_path


def test_a_clean_tree_is_named_by_its_commit_alone(repo: Path) -> None:
    """The commit is what a reader searches for, so it is recorded whenever it is enough."""
    revision = source_revision(repo)
    assert revision is not None
    assert "patch" not in revision
    assert re.fullmatch(rf"[0-9a-f]{{{MR_SHA}}}", revision)


def test_recording_evidence_does_not_change_the_revision(repo: Path) -> None:
    """The regression this module exists for.

    A driver rewrites its own replays, and then the next corpus is recorded. If that moved
    the revision, four corpora recorded in one sitting could never agree on one platform and
    the gates' own remedy -- re-run the batch against a single deployment -- could not be
    carried out.
    """
    before = source_revision(repo)
    (repo / "evaluation" / "quality" / "replays" / "Q-001.json").write_text(
        '{"attempt": 2, "passed": true}\n', encoding="utf-8"
    )
    (repo / "evaluation" / "quality" / "replays" / "Q-002.json").write_text(
        '{"attempt": 1}\n', encoding="utf-8"
    )
    (repo / "evaluation" / "reports").mkdir()
    (repo / "evaluation" / "reports" / "latest.json").write_text("{}\n", encoding="utf-8")
    assert source_revision(repo) == before


def test_writing_the_report_does_not_change_the_revision(repo: Path) -> None:
    """``docs/`` is narrative about the run, written while the run is being recorded."""
    before = source_revision(repo)
    (repo / "docs" / "BASELINE.md").write_text("# baseline, revised\n", encoding="utf-8")
    assert source_revision(repo) == before


def test_editing_source_changes_the_revision(repo: Path) -> None:
    """The other half: a change to code that runs has to be visible, or the batch is
    attributed to a platform that never served it."""
    before = source_revision(repo)
    (repo / "src" / "workflow.py").write_text("STEPS = 2\n", encoding="utf-8")
    after = source_revision(repo)
    assert after != before
    assert after is not None and PATCHED.match(after)


def test_the_commit_is_kept_alongside_the_patch(repo: Path) -> None:
    """A reader needs both: the commit to find the tree, the digest to know it was modified."""
    (repo / "src" / "workflow.py").write_text("STEPS = 2\n", encoding="utf-8")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert source_revision(repo) == f"{head}+patch({fingerprint(repo, changed_source_paths(repo))})"


def test_two_edits_of_the_same_size_are_not_the_same_revision(repo: Path) -> None:
    """What the line count could never do.

    Two trees whose ``git status`` is the same length are the same shape and a different
    platform, and a string that calls them equal is a provenance check that passes when it
    should refuse.
    """
    (repo / "src" / "workflow.py").write_text("STEPS = 2\n", encoding="utf-8")
    first = source_revision(repo)
    (repo / "src" / "workflow.py").write_text("STEPS = 3\n", encoding="utf-8")
    second = source_revision(repo)
    # Same path, same size, same line count in ``git status``: the old string was identical.
    assert len("STEPS = 2\n") == len("STEPS = 3\n")
    assert first != second


def test_a_new_source_file_changes_the_revision(repo: Path) -> None:
    """A module written but not ``git add``-ed is importable, so it is part of the platform.

    ``git status`` counts untracked files and the old string noticed them; a fingerprint
    built from ``git diff`` alone would not, so this pins the second ``git ls-files`` call.
    """
    before = source_revision(repo)
    (repo / "src" / "new_agent.py").write_text("NAME = 'new'\n", encoding="utf-8")
    assert source_revision(repo) != before


def test_deleting_a_source_file_changes_the_revision(repo: Path) -> None:
    """Removal is a change to the code. A digest over readable files only would miss it."""
    before = source_revision(repo)
    (repo / "src" / "workflow.py").unlink()
    assert source_revision(repo) != before


def test_a_renamed_source_file_changes_the_revision(repo: Path) -> None:
    """Task types are declared per module, so the path is part of what was deployed."""
    before = source_revision(repo)
    (repo / "src" / "workflow.py").rename(repo / "src" / "orchestration.py")
    assert source_revision(repo) != before


def test_an_untracked_non_source_file_is_ignored(repo: Path) -> None:
    """The exclusion is by path, so it holds for files git has never seen either."""
    before = source_revision(repo)
    (repo / "evaluation" / "quality" / "replays" / "Q-003.json").write_text(
        "{}\n", encoding="utf-8"
    )
    assert source_revision(repo) == before


def test_the_answer_is_stable_across_repeated_reads(repo: Path) -> None:
    """Four corpora are recorded hours apart and have to arrive at the same string.

    Nothing in the fingerprint may depend on scan order, timestamps or the filesystem's
    own ordering, which is not sorted on every platform this runs on.
    """
    (repo / "src" / "workflow.py").write_text("STEPS = 2\n", encoding="utf-8")
    (repo / "src" / "new_agent.py").write_text("NAME = 'new'\n", encoding="utf-8")
    assert len({source_revision(repo) for _ in range(5)}) == 1


def test_the_excluded_prefixes_are_the_ones_the_drivers_write() -> None:
    """A path that is not excluded is a path whose changes move the revision.

    Written as a test because the constant is the whole policy: adding a directory here
    silently stops recording changes to it, and the four drivers all write under these.
    """
    assert set(NON_SOURCE_PREFIXES) == {"evaluation/", "docs/"}
    assert all(prefix.endswith("/") for prefix in NON_SOURCE_PREFIXES)


def test_something_that_is_not_a_checkout_has_no_revision(tmp_path: Path) -> None:
    """``None``, not a string, so a gate refuses the batch rather than reading a name that
    describes nothing."""
    assert source_revision(tmp_path) is None
