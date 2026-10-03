"""The coordination report names the corpus it read, instead of restating a size from memory.

``scripts/report_phase7_coordination_recorded.py`` closes with the caveat that its distributions
describe one corpus rather than a population -- and until 2026-10-03 it said that corpus was "121
observations". The quality batch then grew to 200 recorded runs, and the re-rendered report carried
a sentence counting 121 one line under a table counting 200.

This is the same failure as the reliability report's frozen limitations, and it is worth a test of
its own because the sentence is *true when written*: nothing about the code looked wrong, and only
re-reading it beside its own output reveals it. The size is now read from the rows that were read,
so the two cannot disagree.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


def load_reporter() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_report_phase7_coordination_recorded",
        SCRIPTS / "report_phase7_coordination_recorded.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REPORTER = load_reporter()


def test_the_corpus_size_is_read_from_the_rows() -> None:
    """Change the corpus, change the sentence. A constant would keep saying 121."""
    for size in (1, 121, 200, 2000):
        items = REPORTER.limitations([{} for _ in range(size)])
        closing = items[-1]
        assert f"{size} observations" in closing, (size, closing)
        for other in (121, 200):
            if other != size:
                assert f"{other} observations" not in closing, (size, other, closing)


def test_the_caveat_itself_survives() -> None:
    """Deriving the number must not quietly drop the statement it was attached to."""
    closing = REPORTER.limitations([{} for _ in range(7)])[-1]
    assert "not population estimates" in closing
    assert "one tenant at one revision" in closing


def test_no_limitation_is_a_bare_restatement_of_a_size() -> None:
    """The four corpus-independent caveats must not carry a count of their own."""
    for item in REPORTER.LIMITATIONS:
        assert "observations" not in item, item
