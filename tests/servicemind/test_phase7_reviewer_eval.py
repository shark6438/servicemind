"""Contract tests for the Reviewer deterministic-gate evaluation harness.

The harness in ``scripts/evaluate_phase7_reviewer.py`` measures two error rates over
constructed inputs. These tests pin the parts of that measurement that must not drift
silently: that every branch the gate's contract names is reached by some case, that no
well-formed analysis is blocked, and that no defect the contract names is missed.

Without the first of those, the two error rates are misleading in the way a green suite
usually is -- a rate computed over cases that never reach the branch in question reads as
"this branch is correct" when it reads as anything at all. Without the second, a branch
added later that over-blocks would lower the recall figure nobody is watching.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "scripts/evaluate_phase7_reviewer.py"

#: Every reason code ``_deterministic_gate`` can emit. A case must reach each one.
CONTRACT_BRANCHES = {
    "DEGRADED_ANALYSIS",
    "UNKNOWN_EVIDENCE_REFERENCE",
    "ACTION_POLICY_REJECTED",
    "MISSING_REQUIRED_EVIDENCE",
    "MISSING_KNOWLEDGE_CITATION",
    "INVALID_KNOWLEDGE_CITATION",
    "CITATION_ID_MISMATCH",
    "CITATION_EVIDENCE_MISMATCH",
    "EVIDENCE_CONFLICT",
    "UNKNOWN_SUPPORT_GROUP",
    "HUMAN_REVIEW_REQUIRED",
    "MISSING_ACTION_PROPOSAL",
}


def _harness():
    spec = importlib.util.spec_from_file_location("_reviewer_eval", HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_reviewer_eval"] = module
    spec.loader.exec_module(module)
    return module


def _built():
    module = _harness()
    agent = module.ReviewerAgent()
    return [module._run_case(agent, case) for case in module.CASES]


def test_every_branch_the_contract_names_is_reached():
    metrics = _harness().summarise(_built())
    assert metrics["unexercised_branches"] == []
    assert set(metrics["branch_coverage"]) == CONTRACT_BRANCHES


def test_no_well_formed_analysis_is_blocked():
    blocked = [row["id"] for row in _built() if row["family"] == "sound" and row["flagged"]]
    assert blocked == [], f"the gate blocked sound analyses: {blocked}"


def test_no_defect_the_contract_names_is_missed():
    missed = [row["id"] for row in _built() if row["family"] == "defective" and not row["flagged"]]
    assert missed == [], f"the gate missed named defects: {missed}"


def test_the_decision_and_the_reason_both_match_the_contract():
    wrong = [
        row["id"]
        for row in _built()
        if row["family"] != "evasion" and not (row["decision_matches"] and row["reason_matches"])
    ]
    assert wrong == [], f"the gate decided against its contract: {wrong}"


def test_defects_only_a_model_can_see_are_deferred_rather_than_decided():
    decided = [row["id"] for row in _built() if row["family"] == "semantic_only" and row["flagged"]]
    assert decided == [], f"the gate ruled on a semantic defect: {decided}"


def test_the_evasion_boundary_is_the_one_that_was_measured():
    """Which evasion cases the deterministic gate lets through; a change is a coverage change.

    Recorded as an equality rather than a bound because both directions matter: a case
    that starts being caught is new capability worth reporting, and one that stops being
    listed is a measurement that quietly narrowed.

    ``REV-EVA-02`` moved from this set to caught on 2026-10-02. Its row marks itself
    ``degraded_rag`` -- which exempts a row from citation validation -- and carried a
    hollow citation as well, and the exemption was granted on the flag alone, so the one
    check that could have seen the citation was skipped before it ran. The exemption is
    now conditioned on the invariant its own docstring states: a degraded fallback
    "carries no citation by design", so a row with a citation has one that is checked.
    The other four still escape here and are measured by the semantic sweep.
    """
    escaped = {row["id"] for row in _built() if row["family"] == "evasion" and not row["flagged"]}
    assert escaped == {
        "REV-EVA-01",
        "REV-EVA-03",
        "REV-EVA-04",
        "REV-EVA-05",
    }
    # The one that stopped escaping is decided, not deferred -- it must be blocked for a
    # named reason and must no longer cost a judge call.
    caught = next(row for row in _built() if row["id"] == "REV-EVA-02")
    assert caught["flagged"]
    assert caught["actual_decision"] == "ESCALATE"
    assert caught["actual_reason"] == "INVALID_KNOWLEDGE_CITATION"


def test_the_recorded_report_is_the_one_the_harness_renders(tmp_path):
    """The report on disk must be the harness' own output, not a hand-edited copy."""
    module = _harness()
    if not module.REPORT_JSON.exists():
        return
    import json

    payload = json.loads(module.REPORT_JSON.read_text())
    assert module.REPORT_MD.read_text() == module.render_markdown(payload)


# --- the zero-call replay probe -------------------------------------------------------
#
# ``scripts/replay_phase7_reviewer_semantic.py`` re-adjudicates the semantic sweep's cases
# from the verdicts the judge already returned, so it costs nothing to run and is the only
# thing that can be run when a contract change lands. That makes its correctness a
# precondition of every conclusion drawn from it -- and it fails silently: the verdict is
# *rebuilt*, and a field the rebuild forgets falls back to its pre-widening default, which
# is indistinguishable in the output from a criterion the judge genuinely left clear.


def _replay():
    spec = importlib.util.spec_from_file_location(
        "_reviewer_semantic_replay", REPO_ROOT / "scripts/replay_phase7_reviewer_semantic.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_reviewer_semantic_replay"] = module
    spec.loader.exec_module(module)
    return module


def test_the_replay_reads_every_field_the_adjudicator_reads():
    """A field added to the verdict contract must reach the replay, not default there."""
    from servicemind.agents.reviewer import SemanticReview

    module = _replay()
    judged = set(SemanticReview.model_fields) - set(module._BOOKKEEPING_KEYS)
    assert set(module.JUDGE_FIELDS) == judged, (
        "the replay rebuilds the verdict field by field; anything it does not name falls "
        "back to its pre-widening value and understates the adjudicator"
    )


def test_the_replay_refuses_a_record_missing_a_judged_field():
    """Refusal, not defaulting -- the two produce the same output and mean the opposite."""
    import json

    module = _replay()
    if not module.REPORT_JSON.exists():
        return
    payload = json.loads(module.REPORT_JSON.read_text(encoding="utf-8"))
    victim = next(case for case in payload["cases"] if case["family"] == "evasion")
    del victim["judge"]["citation_integrity_ok"]
    try:
        module.build(payload, module._load_harness())
    except module.RecordedVerdictIncomplete as incomplete:
        assert "citation_integrity_ok" in str(incomplete)
    else:
        raise AssertionError("a record missing a judged field was replayed anyway")


def test_the_replay_reproduces_the_recorded_semantic_sweep():
    """Zero calls, and the same decisions the sweep recorded -- including the evasion rows."""
    import json

    module = _replay()
    if not module.REPORT_JSON.exists():
        return
    payload = module.build(
        json.loads(module.REPORT_JSON.read_text(encoding="utf-8")), module._load_harness()
    )
    assert payload["summary"]["mismatched"] == []
    assert payload["summary"]["evasion_cleared_by_the_judge"] == 0
