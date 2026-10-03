"""The report checker names the revision it graded, and says when it did not check currency.

``scripts/check_phase7_gate_reports.py`` exits 0 when every gate agrees with its committed
report, and that exit code is the thing a reader is most likely to over-read. On
2026-10-01 it was green while the acceptance verdict rested on 28 replays recorded at
``cf08ac8+...`` and ``HEAD`` was five commits later: the script was confirming that the
observations are mutually consistent and that the grader still accepts them. It was not
confirming that the tree beside it passes.

Neither half can be inferred from the exit code, so both are printed. These tests hold the
two facts that make the output honest -- the graded revision travels with the verdict, and
``currency_checked`` says whether anything compared it against an expected revision -- and
the parsing they rest on, which is the part that would fail quietly: a gate that refuses
prints no summary, and reading its silence as a revision would invent one.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
#: What the acceptance replays actually record. A ``+dirty(N)`` count cannot be recomputed
#: -- it is a fingerprint taken when the run was recorded -- which is precisely why the
#: checker has to print it rather than the reader having to trust it.
RECORDED = "cf08ac8a7b731054e492ed81ba5f3164dc381863+dirty(26 files)"
OTHER = "7a6e6758d2e4966771546e57482e8b0a5d8b19dc"


def load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_check_phase7_gate_reports", SCRIPTS / "check_phase7_gate_reports.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="checker")
def checker_fixture(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The checker with its gates stubbed out.

    The real ``judge`` shells out to four gates and takes about thirteen seconds; what is
    under test here is what the script does with a verdict, not how a verdict is reached --
    that is ``test_phase7_gate_revisions.py`` and the gates' own suites.
    """
    module = load_checker()

    def fake_judge(gate: str, scratch: Path, *, expect_revision: str | None = None) -> object:
        passing = gate == "acceptance"
        return module.Verdict(
            gate=gate,
            exit_code=0 if passing else 3,
            fresh="PASS" if passing else "REFUSED",
            committed="PASS" if passing else "REFUSED",
            committed_path=REPO_ROOT / "evaluation" / "reports" / f"phase7_{gate}_latest.json",
            revisions=(RECORDED,) if passing else (),
        )

    monkeypatch.setattr(module, "judge", fake_judge)
    return module


def test_the_graded_revision_is_printed_beside_the_verdict(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["check", "--format", "markdown"])
    assert checker.main() == 0

    printed = capsys.readouterr().out
    assert RECORDED in printed, (
        "the exit code was printed without the revision it was computed over, so a reader "
        "has no way to tell which platform the verdict describes"
    )
    assert checker.CURRENCY_HEADING in printed
    assert "Currency was **not** checked" in checker.CURRENCY_UNCHECKED, (
        "the unchecked-currency statement no longer says currency was not checked"
    )
    assert checker.CURRENCY_UNCHECKED in printed, (
        "a run that checked currency over nothing must say so; silence reads as "
        "'the current tree passes'"
    )


def test_expect_revision_turns_currency_into_a_checked_fact(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["check", "--format", "json", "--expect-revision", RECORDED])
    assert checker.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["currency_checked"] is True
    assert payload["expect_revision"] == RECORDED
    by_gate = {entry["gate"]: entry for entry in payload["gates"]}
    assert by_gate["acceptance"]["deployed_revisions"] == [RECORDED]


def test_a_run_without_expect_revision_reports_currency_as_unchecked(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["check", "--format", "json"])
    assert checker.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["currency_checked"] is False
    assert payload["expect_revision"] is None
    assert payload["disagreements"] == 0, "homogeneity-only runs still grade the same reports"


def test_a_gate_that_refused_is_not_given_a_revision_it_never_recorded(
    checker: ModuleType,
) -> None:
    """A refusal prints no summary, and silence must not be read as a revision.

    ``judge`` forwards the gate's exit code and stdout verbatim, so the refusal path is the
    one where the parsing has nothing to read. An empty tuple is what the output renders as
    ``<nothing graded>``; inventing a revision there would attribute a verdict to a platform.
    """
    assert checker._graded_revisions("") == ()
    assert checker._graded_revisions("Traceback (most recent call last):\n  ...") == ()
    assert checker._graded_revisions('{"configuration_error": "no replays"}') == ()
    assert checker._graded_revisions('{"deployed_revisions": "not-a-list"}') == ()
    assert checker._graded_revisions('{"deployed_revisions": ["a", 7, null]}') == ("a",)
    assert checker._graded_revisions(json.dumps({"deployed_revisions": [OTHER]})) == (OTHER,)


def test_a_missing_committed_report_is_not_agreement() -> None:
    """Deleting the artefact must not be a way to pass the check.

    ``_read_verdict`` returns ``None`` for a file that is gone and for one that will not
    parse. Reading that as agreement made the check green in the one case where its
    subject is absent -- the same class of failure the tool was built to catch (a report
    saying PASS while the gate refused), in the case a reader is least able to see.
    """
    checker = load_checker()
    path = REPO_ROOT / "evaluation" / "reports" / "phase7_quality_latest.json"
    present = checker.Verdict(
        gate="quality", exit_code=0, fresh="PASS", committed="PASS", committed_path=path
    )
    absent = checker.Verdict(
        gate="quality", exit_code=0, fresh="PASS", committed=None, committed_path=path
    )
    assert present.agrees is True
    assert absent.committed_is_missing is True
    assert absent.agrees is False


def test_the_disagreement_names_a_missing_report_as_missing(
    checker: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``None`` printed as a verdict reads like a verdict; the report says what it is."""
    monkeypatch.setattr(sys, "argv", ["check", "--format", "markdown"])
    monkeypatch.setattr(
        checker,
        "judge",
        lambda gate, scratch, *, expect_revision=None: checker.Verdict(
            gate=gate,
            exit_code=0,
            fresh="PASS",
            committed=None,
            committed_path=REPO_ROOT / "evaluation" / "reports" / f"phase7_{gate}_latest.json",
        ),
    )
    assert checker.main() == 1
    printed = capsys.readouterr().out
    assert "missing or unreadable" in printed
