"""The reliability report's limitations are read out of its own corpus, not asserted beside it.

``scripts/report_phase7_reliability_recorded.py`` reads repeats that the load batch left behind,
and until 2026-10-03 it stated its confounds as a module constant. Re-recording the load batch put
every observation on one revision and gave the tier with no repeats the most, at which point two of
those constants were false: the report said its corpus spanned two revisions, and said the one
observed instability was not in it -- directly above a table showing one revision and a 60% floor.

A limitation that contradicts the table it sits under is worse than no limitation, because a reader
who checks the numbers concludes the whole section is boilerplate. So the ones that are statements
about the corpus are computed from it, and these tests hold the two directions of that: one revision
must not be described as several, several must not be described as one, and the floor must quote the
number that is actually in the summary rather than a copy of it that can drift.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"

ONE_REVISION = "b385df7c2ef6818f24d5f173ca92158989ba3c66+patch(96133a9537b6)"
OTHER_REVISION = "cf08ac8a7b731054e492ed81ba5f3164dc381863"


def load_reporter() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_report_phase7_reliability_recorded",
        SCRIPTS / "report_phase7_reliability_recorded.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REPORTER = load_reporter()


def observations(revision: str, count: int) -> list[dict[str, object]]:
    return [{"deployed_revision": revision, "case_id": f"Q-{i:03d}"} for i in range(count)]


def summary(disagreement_rate: float, repeats: list[int]) -> dict[str, object]:
    return {
        "strata": [{"repeats_per_case": {"min": r, "max": r}} for r in repeats],
        "floor": {"disagreement_rate": disagreement_rate},
    }


def test_one_revision_is_not_described_as_a_corpus_of_several() -> None:
    """The regression this locks: the sentence survived a re-record that made it false."""
    items = REPORTER.limitations(observations(ONE_REVISION, 120), summary(0.6, [1, 2, 3]))
    joined = " ".join(items)
    assert "spans" not in joined, joined
    assert "one revision" in joined
    assert ONE_REVISION[:12] in joined, "the limitation must name the revision it is describing"


def test_several_revisions_are_not_described_as_one() -> None:
    """The other direction, so the derivation cannot be replaced by a hard-coded 'one revision'."""
    mixed = observations(ONE_REVISION, 60) + observations(OTHER_REVISION, 60)
    items = REPORTER.limitations(mixed, summary(0.6, [1, 2, 3]))
    joined = " ".join(items)
    assert "spans 2 revisions" in joined, joined


def test_the_floor_quotes_the_number_in_the_summary() -> None:
    """Read from the summary, so the two cannot drift apart the way the constants did."""
    for rate in (0.6, 0.25, 0.0):
        items = REPORTER.limitations(observations(ONE_REVISION, 120), summary(rate, [1, 2, 3]))
        assert any(REPORTER._pct(rate) in item for item in items), (rate, items)


def test_the_repeat_counts_come_from_the_strata() -> None:
    items = REPORTER.limitations(observations(ONE_REVISION, 120), summary(0.6, [1, 4, 9]))
    assert any("1, 4, 9" in item for item in items), items


def test_a_nonzero_floor_is_not_still_described_as_an_absent_one() -> None:
    """The second false constant: it claimed the instability was not in the corpus."""
    items = REPORTER.limitations(observations(ONE_REVISION, 120), summary(0.6, [1, 2, 3]))
    joined = " ".join(items)
    assert "not in this corpus" not in joined
    assert "not an estimate of the platform" in joined
