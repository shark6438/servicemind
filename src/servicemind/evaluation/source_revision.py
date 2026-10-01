"""Which revision of the platform's *code* a run graded, as one reproducible string.

Every live driver writes this string into each observation's ``deployed_revision``, and the
four gates read it back as the only evidence that a batch describes one platform rather than
several (:mod:`servicemind.evaluation.revisions`). The whole provenance check is worth what
this name is worth.

Until 2026-10-01 each driver recorded ``<sha>+dirty(N files)``, where ``N`` was the number of
lines in ``git status --porcelain``. That number is a property of the working tree, and the
working tree is what the drivers write: every recording run rewrites its own evidence under
``evaluation/``, so the act of recording changed the revision that was recorded. Measured on
this repository, one build -- ``cf08ac8``, source unchanged -- went out as five revisions:

    dirty(8)   dirty(26)   dirty(56)   dirty(69)   dirty(130)

and the *older* commit ``688dd90`` went out as ``dirty(155)``, because the batch that graded
it ran later. The string was wrong in both directions at once. It separated batches of one
platform, so ``--expect-revision`` could never be satisfied across corpora and a homogeneous
set was refused as mixed -- the gates' own remedy, "re-run the batch against a single
deployment", could not be carried out. And it did not separate two platforms at all when
their trees happened to differ in the same number of files.

The count was the defect, not the level of detail. What the gates need is a name for the code
the process loaded, so this module fingerprints the *content* of the working tree's delta
from HEAD: a digest over the changed source paths and their bytes, stable across everything
that is not source. ``evaluation/`` is excluded because it is what the drivers themselves
write, and ``docs/`` because it is narrative about the run rather than code that runs. That
exclusion also keeps the two provenance questions apart, which is how the gates already use
them: ``cases_digest`` names what was asked, ``deployed_revision`` names what answered.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

#: Repository paths that are not code, and are rewritten by the measurement itself. A change
#: here does not change the platform under test:
#:
#: * ``evaluation/`` -- case lists, fixtures *and* replays. The replays are the drivers' own
#:   output, which is what made the old count self-referential. The case lists are input, but
#:   they are already bound to a batch by ``cases_digest``; naming them here as well would
#:   report one corpus edit as two independent provenance failures.
#: * ``docs/`` -- the written record of the run, edited while the run is being recorded.
#:
#: An exclusion list rather than the paths that *are* source, deliberately: a new top-level
#: directory is then covered the moment it appears, where an inclusion list would go on
#: reporting a revision that never mentioned it. ``tests/`` is *not* excluded even though the
#: deployment never loads it, so that one revision covers the platform together with the
#: suite the report pairs it with -- the cost is that editing a test calls for a re-record,
#: which is conservative and, during a recording campaign, rare.
NON_SOURCE_PREFIXES = ("evaluation/", "docs/")

#: How much of the digest is kept in the revision string. Twelve hex digits is short enough to
#: read in a report and wide enough that two working trees colliding is not a live risk.
DIGEST_LENGTH = 12

#: What a path that is changed but cannot be read contributes. Deletions and anything else
#: absent from the tree fingerprint as this, so that removing a source file is a change.
_ABSENT = b"<absent>"


def source_revision(repo_root: Path) -> str | None:
    """The source revision under ``repo_root``, or ``None`` if it is not a checkout.

    ``<sha>`` when the source matches HEAD, ``<sha>+patch(<12 hex>)`` when it does not. The
    commit alone is not enough in the second case -- the deployed code is the tree, not the
    commit, and a report naming only HEAD would describe code the process never loaded --
    and the digest is not enough in the first, because a reader has to be able to find the
    commit. Both are recorded.
    """
    # Stripped here rather than inside ``_git``, whose other callers read NUL-separated
    # output where a filename may legitimately end in whitespace. Unstripped, rev-parse's
    # newline puts the ``+patch(...)`` suffix on a second line of the recorded revision.
    head = _git(repo_root, "rev-parse", "HEAD").strip()
    if not head:
        return None
    changed = changed_source_paths(repo_root)
    if not changed:
        return head
    return f"{head}+patch({fingerprint(repo_root, changed)})"


def changed_source_paths(repo_root: Path) -> list[str]:
    """Paths under ``repo_root`` that differ from HEAD and are code, sorted.

    Modified, staged, deleted and untracked alike: a module that was written but not yet
    ``git add``-ed is source the process can import, and leaving it out would let a
    brand-new file change the platform without changing the revision.
    """
    listed = "".join(
        [
            _git(repo_root, "diff", "HEAD", "--name-only", "-z"),
            _git(repo_root, "ls-files", "--others", "--exclude-standard", "-z"),
        ]
    )
    return sorted(
        path
        for path in {entry for entry in listed.split("\0") if entry}
        if not path.startswith(NON_SOURCE_PREFIXES)
    )


def fingerprint(repo_root: Path, paths: list[str]) -> str:
    """A stable digest of ``paths`` and their contents, as ``DIGEST_LENGTH`` hex digits.

    Path-and-content rather than content alone, so moving a module is a different revision --
    which matters here because task types are declared per module. Deterministic: the same
    tree fingerprints identically however it was reached, which is what lets four corpora
    recorded hours apart share one revision.
    """
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        target = repo_root / path
        # ``is_file`` is false for a directory, a dangling symlink and a deletion, and all
        # three fingerprint the same way. None of them is a change to code that runs.
        digest.update(hashlib.sha256(target.read_bytes()).digest() if target.is_file() else _ABSENT)
        digest.update(b"\0")
    return digest.hexdigest()[:DIGEST_LENGTH]


def _git(repo_root: Path, *arguments: str) -> str:
    """One ``git`` invocation's stdout, or ``""`` if git could not answer.

    A failure is not raised: the callers record a revision into evidence, and a driver that
    died here would turn "this is not a checkout" into a lost measurement. ``None`` and a
    short string are both handled by the gates, which refuse a batch whose revision they
    cannot read.
    """
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else ""
