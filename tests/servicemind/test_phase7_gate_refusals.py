"""A gate that refuses to judge must not leave a verdict behind that it will not stand by.

Each gate returns ``EXIT_CONFIGURATION`` when its own preconditions fail -- the case list
moved under the report, the replays span several revisions, a batch header no longer
matches. That return happens before anything is written, so whatever report was already on
disk stays there, and it usually says ``PASS``.

Measured on 2026-10-01: ``evaluation/reports/phase7_quality_latest.json`` said
``"verdict": "PASS"`` while ``scripts/gate_phase7_quality.py --replay-only --check`` refused
to judge the same replays with exit 3. Nothing programmatic is misled -- the one cross-gate
consumer re-derives the case digest and refuses a report bound to another case list -- so
the harm is a person opening the report and concluding the opposite of what the gate just
said. A refusal that contradicts its own exit code on disk is the defect; these tests hold
the record to the exit code.

The helper is tested on its own first, then through each gate's ``main``, because a helper
that exists and is not called is the same defect one level down.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from servicemind.evaluation.refusal import REFUSED, stamp_refusal

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
GATES = ("acceptance", "quality", "security", "load")

STALE_REPORT = {
    "verdict": "PASS",
    "generated_at": "2026-09-30T14:44:07.014266+00:00",
    "cases_digest": "d02040916da13337891e1c31d16ae51adb60744d5b056a653b514eacd3a931ee",
    "observation_digest": "9f4752c78c810c03586a5060f802afcaa7b33d2b17bea9d62cefee4ec56f6893",
    "cases": [{"case_id": "Q-001", "verdict": "PASS"}],
}
STALE_MARKDOWN = (
    "# P7.6.6 业务质量集 —— 端到端观测报告\n\n- 判定：**PASS** —— PASS 200 / FAIL 0 / BLOCKED 0\n"
)


def load_gate(name: str) -> ModuleType:
    """The gate script as a module, so ``main`` can be called the way the shell calls it."""
    spec = importlib.util.spec_from_file_location(
        f"_refusal_gate_{name}", SCRIPTS / f"gate_phase7_{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def refusal_args(name: str, tmp_path: Path) -> list[str]:
    """An argv each gate genuinely exits 3 on, with the report redirected out of the repo.

    Three gates refuse ``--live`` outright: the observations have to come from whatever is
    deployed, and grading older replays under an invocation that says "live" would report a
    run that never happened. The security gate has no such flag, so it is handed a replay
    recorded against a scenario list that is not the one on disk -- the refusal its loader
    actually raises.
    """
    argv = [f"gate_phase7_{name}.py", "--check", "--report", str(tmp_path / "report.md")]
    return argv + ([] if name == "security" else ["--live"])


def security_replay_against_another_scenario_list(gate: ModuleType, tmp_path: Path) -> Path:
    """One real replay, with its binding to the scenario list rewritten to a digest no set has."""
    source = sorted((REPO_ROOT / "evaluation" / "security" / "replays").glob("*.json"))[0]
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["scenarios_digest"] = "deadbeef" * 8
    replays = tmp_path / "replays"
    replays.mkdir(exist_ok=True)
    (replays / source.name).write_text(json.dumps(payload), encoding="utf-8")
    return replays


# --------------------------------------------------------------------------------------
# The helper.
# --------------------------------------------------------------------------------------


def test_a_refusal_supersedes_a_pass_without_erasing_it(tmp_path: Path) -> None:
    report = tmp_path / "phase7_quality_latest.json"
    report.write_text(json.dumps(STALE_REPORT), encoding="utf-8")
    markdown = tmp_path / "phase7_quality_latest.md"
    markdown.write_text(STALE_MARKDOWN, encoding="utf-8")

    stamp_refusal(
        report,
        markdown,
        gate="scripts/gate_phase7_quality.py",
        reason="the report was generated from a different case list",
        argv=["gate_phase7_quality.py", "--check"],
    )

    document = json.loads(report.read_text(encoding="utf-8"))
    assert document["verdict"] == REFUSED
    assert document["superseded_verdict"] == "PASS"
    assert document["configuration_refusal"]["reason"] == (
        "the report was generated from a different case list"
    )
    assert document["configuration_refusal"]["exit_code"] == 3
    assert document["gate"] == "scripts/gate_phase7_quality.py"
    # The old document survives verbatim, verdicts and digests included, so nothing that was
    # measured is lost by recording that it is no longer reproducible.
    assert document["superseded_report"] == STALE_REPORT

    # And the binding that made it citable is *not* carried to the top level. A reader that
    # checks the digest and then reads ``cases`` gets an empty table instead of a refusal,
    # which is the silent direction; with no digest at the top level the same reader refuses.
    assert "cases_digest" not in document
    assert "cases" not in document

    text = markdown.read_text(encoding="utf-8")
    assert "拒绝判定" in text
    assert "the report was generated from a different case list" in text
    assert "PASS 200 / FAIL 0" in text  # the history is quoted, not deleted
    assert text.index("拒绝判定") < text.index("判定：**PASS**")  # the banner leads


def test_a_refusal_with_nothing_on_disk_still_records_itself(tmp_path: Path) -> None:
    """``--report`` pointing somewhere fresh is a refusal that happened, not an absent gate."""
    report = tmp_path / "phase7_security_latest.json"
    markdown = tmp_path / "phase7_security_latest.md"

    stamp_refusal(report, markdown, gate="scripts/gate_phase7_security.py", reason="no replays")

    document = json.loads(report.read_text(encoding="utf-8"))
    assert document["verdict"] == REFUSED
    assert "superseded_report" not in document
    assert document["configuration_refusal"]["note"] == "no report was on disk"
    assert "磁盘上原本没有报告" in markdown.read_text(encoding="utf-8")


def test_an_unreadable_report_does_not_take_the_refusal_down_with_it(tmp_path: Path) -> None:
    """The gate is leaving with exit 3 already; annotating the old file must not raise."""
    report = tmp_path / "phase7_load_latest.json"
    report.write_text("{ not json", encoding="utf-8")

    document = stamp_refusal(report, None, gate="scripts/gate_phase7_load.py", reason="stale batch")

    assert document["verdict"] == REFUSED
    assert "could not be read back" in document["configuration_refusal"]["note"]
    assert json.loads(report.read_text(encoding="utf-8"))["verdict"] == REFUSED


# --------------------------------------------------------------------------------------
# The wiring: each gate's own ``main``, over a real refusal, with a PASS already on disk.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", GATES)
def test_a_gate_that_exits_three_leaves_no_pass_behind(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    gate = load_gate(name)
    report = tmp_path / "report.json"
    report.write_text(json.dumps(STALE_REPORT), encoding="utf-8")
    (tmp_path / "report.md").write_text(STALE_MARKDOWN, encoding="utf-8")

    if name == "security":
        monkeypatch.setattr(
            gate, "REPLAYS", security_replay_against_another_scenario_list(gate, tmp_path)
        )
    monkeypatch.setattr(sys, "argv", refusal_args(name, tmp_path))
    capsys.readouterr()

    assert gate.main() == gate.EXIT_CONFIGURATION

    document = json.loads(report.read_text(encoding="utf-8"))
    assert document["verdict"] == REFUSED
    assert document["superseded_verdict"] == "PASS"
    assert document["gate"].endswith(f"gate_phase7_{name}.py")
    # The reason is the gate's own, carried through rather than re-worded by the handler.
    assert document["configuration_refusal"]["reason"]
    assert "拒绝判定" in (tmp_path / "report.md").read_text(encoding="utf-8")


#: Where each gate reads its evidence, and the batch file that names one revision for the
#: whole sweep -- absent for the two gates that keep no batch.
EVIDENCE_PATHS = {
    "quality": ("evaluation/quality/replays", "BATCH"),
    "acceptance": ("evaluation/acceptance/replays", None),
    "security": ("evaluation/security/replays", None),
    "load": ("evaluation/load/replays", "BATCH"),
}

ONE_REVISION = "deadbeef+copied-by-the-test"


def one_revision_corpus(gate: ModuleType, name: str, replays: Path) -> str:
    """Every real replay, restamped as one revision, in a directory of their own.

    The corpus on disk is not homogeneous -- the quality and load replays span two source
    revisions, which is the very refusal this module is about. A test that reached for the
    real directory to get a non-refusing run would therefore be asking the corpus to be
    something it is not. Homogeneity is what the second half needs, so it is constructed.
    """
    relative, batch_attribute = EVIDENCE_PATHS[name]
    for source in sorted((REPO_ROOT / relative).glob("*.json")):
        if source.name.startswith("_"):  # the batch header, which no loader reads as a replay
            continue
        payload = json.loads(source.read_text(encoding="utf-8"))
        where = payload["environment"] if "environment" in payload else payload
        where["deployed_revision"] = ONE_REVISION
        (replays / source.name).write_text(json.dumps(payload), encoding="utf-8")
    if batch_attribute is not None:
        # Pointed at a name no run ever wrote, so the batch left on disk cannot put a second
        # revision back into a corpus this test just made homogeneous.
        setattr(gate, batch_attribute, getattr(gate, batch_attribute).with_name("_absent.json"))
    return ONE_REVISION


@pytest.mark.parametrize("name", GATES)
def test_a_gate_that_does_not_refuse_leaves_its_report_alone(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The other half: stamping must be the refusal path's, not something every run does.

    Without this, a gate that stamped ``REFUSED`` over every report -- including the ones it
    had just written a verdict into -- would pass the test above.
    """
    gate = load_gate(name)
    report = tmp_path / "report.json"
    report.write_text(json.dumps(STALE_REPORT), encoding="utf-8")
    replays = tmp_path / "replays"
    replays.mkdir()
    monkeypatch.setattr(gate, "REPLAYS", replays)

    argv = [f"gate_phase7_{name}.py", "--check", "--report", str(tmp_path / "report.md")]
    if name == "security":
        # An empty corpus is not a configuration error: nothing observed is insufficient
        # observation, which is exit 2. What matters here is only that it is not exit 3.
        pass
    else:
        revision = one_revision_corpus(gate, name, replays)
        argv += ["--force", "--expect-revision", revision, "--format", "json"]
    monkeypatch.setattr(sys, "argv", argv)
    capsys.readouterr()

    assert gate.main() != gate.EXIT_CONFIGURATION
    assert json.loads(report.read_text(encoding="utf-8"))["verdict"] != REFUSED
