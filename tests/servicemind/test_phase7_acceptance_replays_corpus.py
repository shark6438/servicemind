"""A sweep may write a subset of the cases, but never into the standing corpus.

The driver used to have exactly one destination, the standing ``replays`` directory, and
the gate that grades it reads that directory as *one* corpus. Those two facts are only
compatible while every sweep runs all 28 cases. The moment a sweep runs a subset -- which
the read-only sweep does, because the other 8 cases write real followups into a real GLPI
-- writing it to the standing directory would leave that directory holding replays from
two deployments at once. Every gate here refuses a batch that spans more than one
revision, so the result is not a quiet wrong answer: it is a corpus nothing can grade
until someone works out by hand which files belong to which sweep.

So ``--replays`` exists on both sides, and what these tests pin is the default. The
option was added to let a partial sweep go *somewhere else*, not to let anyone move the
standing corpus by accident: a default that drifted to a scratch directory or to a
sibling would make ``--check`` grade the wrong corpus, or nothing, and report it as a
clean sweep.

The default is required to *be* the ``REPLAYS`` constant, so writing the same path out
inline also fails. That is deliberate: the point is that the driver and the gate name the
same corpus through the same binding, and a second copy of the path is a second thing to
keep in sync.

The assertions read the syntax tree rather than the text. ``"--replays" in source`` and
``"default=REPLAYS" in source`` both still hold for ``default=REPLAYS.parent`` -- checked,
and it passed -- so a substring is not a statement about where the default points.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STANDING = REPO_ROOT / "evaluation" / "acceptance" / "replays"

#: What ``REPLAYS`` must be bound to, as ``ast.unparse`` renders it.
STANDING_EXPRESSION = "REPO_ROOT / 'evaluation' / 'acceptance' / 'replays'"

#: The two sides that must agree on where the standing corpus is: the driver writes it,
#: the gate grades it. A default that differed between them grades the wrong corpus.
REPLAYS_AWARE_SCRIPTS = (
    "verify_phase7_acceptance_live.py",
    "gate_phase7_acceptance.py",
)


def _module_source(script: str) -> str:
    return (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")


def _replays_binding(tree: ast.Module) -> ast.expr | None:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "REPLAYS" for target in node.targets
        ):
            return node.value
    return None


def _replays_default(tree: ast.Module) -> ast.expr | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not any(isinstance(arg, ast.Constant) and arg.value == "--replays" for arg in node.args):
            continue
        for keyword in node.keywords:
            if keyword.arg == "default":
                return keyword.value
    return None


@pytest.mark.parametrize("script", REPLAYS_AWARE_SCRIPTS)
def test_the_standing_corpus_is_the_default_destination(script: str) -> None:
    tree = ast.parse(_module_source(script), filename=script)

    binding = _replays_binding(tree)
    assert binding is not None, f"{script} no longer defines REPLAYS"
    assert ast.unparse(binding) == STANDING_EXPRESSION, (
        f"{script} binds REPLAYS to {ast.unparse(binding)!r} rather than the standing corpus"
    )

    default = _replays_default(tree)
    assert default is not None, (
        f"{script} must offer --replays defaulting to the standing corpus, so an ordinary "
        "sweep needs no argument and a partial sweep has to say where it is going"
    )
    assert isinstance(default, ast.Name) and default.id == "REPLAYS", (
        f"{script} defaults --replays to {ast.unparse(default)!r}; a default derived from "
        "REPLAYS points somewhere else while still reading as if it did not"
    )


def test_the_standing_corpus_holds_one_deployment() -> None:
    """The invariant ``--replays`` exists to protect, asserted on the corpus itself.

    This is the state a careless partial sweep destroys, and it is checkable without
    running anything: every replay carries the deployment it was recorded against, and a
    single corpus may only carry one of them.
    """
    replays = sorted(STANDING.glob("*.json"))
    assert replays, f"no replays under {STANDING}"

    revisions = {
        json.loads(path.read_text(encoding="utf-8"))["environment"]["deployed_revision"]
        for path in replays
    }
    assert len(revisions) == 1, (
        "the standing corpus spans more than one deployed revision "
        f"({sorted(revisions)}); a sweep wrote into it instead of its own directory, and "
        "every gate that grades it will now refuse"
    )
