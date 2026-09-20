from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "audit_rag_quality_state", ROOT / "scripts/audit_rag_quality_state.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_quality_state_separates_domain_gate_proxy_and_smoke() -> None:
    state = MODULE.build_status(
        json.loads((ROOT / "evaluation/reports/phase4_proxy_release_latest.json").read_text()),
        json.loads((ROOT / "evaluation/reports/phase4_retrieval_latest.json").read_text()),
    )
    assert state["overall_status"] == "DOMAIN_QUALITY_NOT_CERTIFIED"
    assert state["tenant_release_gate"] == {
        "status": "NOT_EVALUATED",
        "gate_count": 6,
        "evaluated_count": 0,
        "required_input": "tenant-domain release queries with independent human qrels",
    }
    assert state["external_silver"]["status"] == "BELOW_TARGET_DIAGNOSTIC"
    assert len(state["external_silver"]["below_reference_targets"]) == 4
    assert state["answerability_signal"]["answerable_answer_rate"] == 0.275
    assert state["answerability_signal"]["is_end_to_end_reviewer_measurement"] is False
    assert state["committed_gold"]["status"] == "SMOKE_ONLY_SATURATED"
    assert set(state["committed_gold"]["saturated_arms"]) == {
        "dense",
        "bm25",
        "hybrid",
        "hybrid_rerank",
    }


def test_a_tenant_gate_cannot_silently_become_applicable() -> None:
    proxy = json.loads((ROOT / "evaluation/reports/phase4_proxy_release_latest.json").read_text())
    smoke = json.loads((ROOT / "evaluation/reports/phase4_retrieval_latest.json").read_text())
    proxy["gates"]["recall_at_5"]["applicable"] = True
    proxy["gates"]["recall_at_5"]["passed"] = False
    try:
        MODULE.build_status(proxy, smoke)
    except ValueError as exc:
        assert "not evaluated" in str(exc)
    else:
        raise AssertionError("mixed tenant/proxy gate semantics were accepted")
