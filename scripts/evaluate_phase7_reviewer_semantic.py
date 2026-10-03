#!/usr/bin/env python
"""What the Reviewer's independent semantic judge does with what the rule gate hands it.

``scripts/evaluate_phase7_reviewer.py`` measures the deterministic layer and stops at the
handoff. Of its 46 cases, 27 are named by the gate's own contract and are decided without
a model; the remaining 19 return ``None``, and with the judge disabled -- the unit-test
default -- the rule-gate node writes a deferral out as ``PASSED``. That harness says so
itself: the judge "is measured separately and live". This is that measurement.

What it runs is the *production* construction -- ``ReviewerAgent(enable_semantic_review=
True)``, the same call as the ``reviewer_agent`` singleton (``reviewer.py``) -- over the
whole compiled graph, so the verdict is adjudicated exactly as a live run adjudicates it.
Only the cases the gate defers are sent: the 27 it decides never reach the judge, and
sending them would spend calls to re-measure a layer already measured.

The expectation is set by the family, not by the outcome:

* ``sound`` -- the judge must clear it. Anything else is a **false reject**: the platform
  refused an answer its evidence supported, which is the failure mode the judge's own
  prompt spends most of its length guarding against.
* ``evasion`` -- a real defect of a class the gate's contract does not name (an action on
  a resource no evidence describes, a self-declared ``degraded_rag`` covering a broken
  citation, a group name that is a substring, a directory row cited as the incident, a
  document withdrawn after indexing). The judge must catch it.
* ``semantic_only`` -- a defect only a model can see (prose the evidence contradicts, an
  injected passage). The judge must catch it.

Anything the judge fails to catch is a **false accept**. Two caveats the report keeps
separate rather than averaging away:

* The judge is a model, so this is a sample, not a property. The outcome is recorded
  verbatim per case, and repeating the run is expected to move the borderline ones.
* A provider outage is not a Reviewer failure. The semantic node converts every exception
  into ``SEMANTIC_REVIEW_UNAVAILABLE`` and keeps the exception's class *name*; this driver
  probes the provider once before the loop and refuses (exit 3) rather than publishing a
  measurement of an outage. A 402/connection failure mid-run is recorded as
  ``provider_unavailable`` and excluded from the rates.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

REPO_ROOT = Path(__file__).resolve().parents[1]

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from core import get_model, settings  # noqa: E402
from servicemind.agents.reviewer import ReviewerAgent  # noqa: E402
from servicemind.domain.invocation import AgentInvocationContext  # noqa: E402
from servicemind.domain.review import ReviewDecision  # noqa: E402
from servicemind.evaluation.refusal import stamp_refusal  # noqa: E402
from servicemind.evaluation.source_revision import source_revision  # noqa: E402
from servicemind.runtime.structured import structured_output  # noqa: E402

REPORT_JSON = REPO_ROOT / "evaluation/reports/phase7_reviewer_semantic_latest.json"
REPORT_MD = REPO_ROOT / "evaluation/reports/phase7_reviewer_semantic_latest.md"

HARNESS = REPO_ROOT / "scripts/evaluate_phase7_reviewer.py"

TENANT = UUID("22222222-2222-4222-8222-222222222222")

#: The semantic node's own reason code for "the judge never answered".
UNAVAILABLE = "SEMANTIC_REVIEW_UNAVAILABLE"

#: Exception class names that mean the call never reached a judgement: the provider was
#: unreachable, refusing, or rate-limiting. The node stores only the class name, so this
#: is the whole signal available for separating an outage from a Reviewer finding.
PROVIDER_FAILURES = frozenset(
    {
        "APIError",
        "APIStatusError",
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
        "AuthenticationError",
        "PermissionDeniedError",
        "NotFoundError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "ReadTimeoutError",
        "RemoteProtocolError",
        "ServiceUnavailableError",
        "TimeoutError",
    }
)

#: The record's decision is PASSED only when nothing was found. Everything else is a block.
PASSED = ReviewDecision.PASSED.name


def _load_harness() -> Any:
    """Import the deterministic harness for its case registry and its state builders.

    The 46 cases, their families and the hand-built analyses are the ground truth for both
    layers, and there is exactly one copy of them: the case list and its checksum are what
    make the two reports comparable, so a second definition here would be a second source
    of truth. Importing runs no measurement -- the module builds its agent inside
    ``build()`` -- so this costs nothing.
    """
    spec = importlib.util.spec_from_file_location("_phase7_reviewer_harness", HARNESS)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load the reviewer harness from {HARNESS}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _invocation(case_id: str, *, now: datetime) -> AgentInvocationContext:
    """The dispatch context a live run would hand the Reviewer.

    ``max_model_calls=2`` is what the reviewer is budgeted in production: one judgement,
    plus the one re-ask the node is allowed when the judge omits its self-rating
    (``_rating_retry_allowed``). Declaring 1 here would silently disable that retry and
    score the judge on a path production does not take.
    """
    return AgentInvocationContext(
        run_id=uuid5(NAMESPACE_URL, f"servicemind-reviewer-semantic:{case_id}"),
        tenant_id=TENANT,
        user_id="reviewer-semantic-eval",
        task_id="T1",
        trace_id=f"reviewer-semantic-{case_id}",
        deadline=now + timedelta(minutes=5),
        max_model_calls=2,
    )


class _Probe(BaseModel):
    """The smallest schema that still exercises the governed structured path."""

    ok: bool


#: Failures that mean the request never reached a judgement. Distinguished from a parser
#: failure on purpose: the first is the provider being down, the second is the model not
#: answering the schema -- which is a Reviewer outcome (fail closed), not an outage.
UNPARSEABLE = frozenset({"JSONDecodeError", "ValidationError", "OutputParserException"})


def _probe_verdict(error_class: str | None) -> str:
    if error_class is None:
        return "ok"
    if error_class in UNPARSEABLE:
        return "answered_but_unparseable"
    if error_class in PROVIDER_FAILURES:
        return "unavailable"
    return "unavailable"


async def _probe_provider() -> dict[str, Any]:
    """One tiny call, so an outage is diagnosed as an outage before the loop starts.

    Measured on 2026-10-02 the provider returned 402 for fourteen minutes. Without this,
    that window reads as nineteen Reviewer failures: every deferred case escalates with
    ``SEMANTIC_REVIEW_UNAVAILABLE``, which is fail-closed and *looks* like the judge
    rejecting sound work. One probe call out of twenty is the cheapest way to keep the
    report honest.

    The prompt has to ask for JSON explicitly. The gateway routes DeepSeek through
    ``json_mode`` (``_structured``, ``gateway.py``), and a bare "reply with ok=true" is
    answered in prose -- which is how the first version of this probe refused a provider
    that was answering fine. So it carries the same instruction shape the judge's own
    prompt carries.
    """
    started = datetime.now(tz=UTC)
    schema_json = json.dumps(_Probe.model_json_schema())
    messages = [
        SystemMessage(content=f"Reply with JSON matching this schema: {schema_json}"),
        HumanMessage(content="Is the provider reachable? Answer with JSON."),
    ]
    try:
        runnable = structured_output(get_model(settings.DEFAULT_MODEL), _Probe)
        verdict = await runnable.ainvoke(messages)
    except Exception as exc:  # noqa: BLE001 - the point is to classify any failure
        error_class = type(exc).__name__
        return {
            "ok": False,
            "verdict": _probe_verdict(error_class),
            "model": str(settings.DEFAULT_MODEL),
            "error_class": error_class,
            "reason": str(exc)[:400],
        }
    return {
        "ok": True,
        "verdict": "ok",
        "model": str(settings.DEFAULT_MODEL),
        "error_class": None,
        "reason": None,
        "reply": bool(verdict.ok),
        "latency_ms": round((datetime.now(tz=UTC) - started).total_seconds() * 1000, 1),
    }


def _deferred(harness: Any, agent: ReviewerAgent) -> list[dict[str, Any]]:
    """The cases the deterministic gate hands to the judge, and only those."""
    return [case for case in harness.CASES if agent._deterministic_gate(case["state"]()) is None]


async def _run_case(agent: ReviewerAgent, case: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Drive the compiled graph once and record the verdict next to the expectation."""
    state = dict(case["state"]())
    state["invocation"] = _invocation(case["id"], now=now)
    state["model_calls"] = 0
    outcome = await agent.graph.ainvoke(state)
    result = outcome["result"]
    decision = ReviewDecision(result.decision).name
    findings = list(result.findings)
    reason = findings[0].reason_code if findings else None
    semantic = outcome.get("semantic")
    unavailable_class = (
        (findings[0].explanation if findings else None) if reason == UNAVAILABLE else None
    )
    return {
        "id": case["id"],
        "family": case["family"],
        "why": case["why"],
        "expected": "PASSED" if case["family"] == "sound" else "not PASSED",
        "decision": decision,
        "flagged": decision != PASSED,
        "reason_code": reason,
        "reason_codes": [finding.reason_code for finding in findings],
        "severity": [finding.severity for finding in findings],
        "risk_level": result.risk_level.name if result.risk_level is not None else None,
        "degraded": bool(result.degraded),
        "reviewer_model": result.reviewer_model,
        "model_calls": int(outcome.get("model_calls") or 0),
        "judge_unavailable_class": unavailable_class,
        "judge": None
        if semantic is None
        else {
            "claims_supported": semantic.claims_supported,
            "action_consistent": semantic.action_consistent,
            "prompt_injection_detected": semantic.prompt_injection_detected,
            "contradictions": list(semantic.contradictions),
            "unsupported_claim_ids": list(semantic.unsupported_claim_ids),
            "action_target_grounded": semantic.action_target_grounded,
            "citation_integrity_ok": semantic.citation_integrity_ok,
            "unbacked_assertions": [
                {"field": a.field, "detail": a.detail} for a in semantic.unbacked_assertions
            ],
            "missing_criteria": list(semantic.missing_criteria),
            "confidence": semantic.confidence,
            "rating_supplied": semantic.rating_supplied,
            "feedback": semantic.feedback,
        },
    }


def _judge(row: dict[str, Any]) -> bool:
    """Whether the judge returned a verdict at all, as opposed to failing to be reached."""
    return row["judge"] is not None and not _provider_unavailable(row)


def _provider_unavailable(row: dict[str, Any]) -> bool:
    return (row["judge_unavailable_class"] or "") in PROVIDER_FAILURES


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The two errors, kept apart, over the cases the judge actually answered.

    A case whose call never reached a judgement is counted in ``provider_unavailable`` and
    left out of both rates: folding an outage into a false-reject rate would report the
    network as the Reviewer's behaviour.
    """
    measured = [row for row in rows if not _provider_unavailable(row)]
    sound = [row for row in measured if row["family"] == "sound"]
    defective = [row for row in measured if row["family"] in {"evasion", "semantic_only"}]
    false_reject = [row["id"] for row in sound if row["flagged"]]
    false_accept = [row["id"] for row in defective if not row["flagged"]]
    confidences = [
        row["judge"]["confidence"]
        for row in measured
        if row["judge"] is not None and row["judge"]["rating_supplied"]
    ]
    return {
        "cases_run": len(rows),
        "cases_measured": len(measured),
        "provider_unavailable": [row["id"] for row in rows if _provider_unavailable(row)],
        "sound_cases": len(sound),
        "defective_cases": len(defective),
        "judge_false_reject_rate": _rate(len(false_reject), len(sound)),
        "judge_false_accept_rate": _rate(len(false_accept), len(defective)),
        "false_reject_cases": false_reject,
        "false_accept_cases": false_accept,
        "evasion_caught": [
            row["id"] for row in measured if row["family"] == "evasion" and row["flagged"]
        ],
        "evasion_cases": [row["id"] for row in measured if row["family"] == "evasion"],
        "semantic_caught": [
            row["id"] for row in measured if row["family"] == "semantic_only" and row["flagged"]
        ],
        "semantic_cases": [row["id"] for row in measured if row["family"] == "semantic_only"],
        "model_calls": sum(row["model_calls"] for row in rows),
        "cases_that_re_asked": [row["id"] for row in measured if row["model_calls"] > 1],
        "rating_missing": [
            row["id"]
            for row in measured
            if row["judge"] is not None and not row["judge"]["rating_supplied"]
        ],
        "decision_counts": dict(sorted(Counter(row["decision"] for row in rows).items())),
        "reason_counts": dict(
            sorted(Counter(row["reason_code"] for row in rows if row["reason_code"]).items())
        ),
        "contract_fill_rate": _rate(
            len([row for row in measured if _judge(row) and not row["judge"]["missing_criteria"]]),
            len([row for row in measured if _judge(row)]),
        ),
        "criteria_the_judge_left_out": {
            name: len(
                [
                    row
                    for row in measured
                    if _judge(row) and name in row["judge"]["missing_criteria"]
                ]
            )
            for name in ("action_target_grounded", "citation_integrity_ok", "unbacked_assertions")
        },
        "confidence": {
            "count": len(confidences),
            "min": min(confidences) if confidences else None,
            "median": sorted(confidences)[len(confidences) // 2] if confidences else None,
            "max": max(confidences) if confidences else None,
        },
    }


async def build() -> dict[str, Any]:
    harness = _load_harness()
    now = datetime.now(tz=UTC)
    probe = await _probe_provider()
    if probe["verdict"] == "unavailable":
        return {
            "producer": "scripts/evaluate_phase7_reviewer_semantic.py",
            "observed_at": now.isoformat(),
            "tool_revision": source_revision(REPO_ROOT),
            "verdict": "REFUSED",
            "reason": "the model provider did not answer the pre-flight probe",
            "provider_probe_verdict": probe["verdict"],
            "provider_probe": probe,
        }

    agent = ReviewerAgent(enable_semantic_review=True)
    deferred = _deferred(harness, agent)
    rows = []
    for case in deferred:
        rows.append(await _run_case(agent, case, now=now))
        print(f"  {case['id']} ({case['family']}) -> {rows[-1]['decision']}")

    metrics = summarise(rows)
    return {
        "producer": "scripts/evaluate_phase7_reviewer_semantic.py",
        "observed_at": now.isoformat(),
        "tool_revision": source_revision(REPO_ROOT),
        "provider_probe": probe,
        "graph": "ReviewerAgent(enable_semantic_review=True).graph",
        "harness": {
            "path": str(HARNESS.relative_to(REPO_ROOT)),
            "cases": len(harness.CASES),
            "deferred_to_the_judge": len(deferred),
            "decided_by_the_rule_gate": len(harness.CASES) - len(deferred),
        },
        "invocation": {
            "max_model_calls": 2,
            "note": "the reviewer's production budget: one judgement plus the self-rating re-ask",
        },
        "metrics": metrics,
        "cases": rows,
        "expected_note": (
            "sound cases must be cleared; evasion and semantic_only cases must be flagged. "
            "The expectation comes from the case family, not from this run's outcome."
        ),
        "limitations": [
            "One sample per case. The judge is a model, so a borderline case may move on a "
            "repeat; treat the caught/failed split as evidence about these inputs, not as a "
            "rate for the population.",
            "The rule gate decides 27 of the 46 cases and those calls cost nothing; their "
            "measurement is in phase7_reviewer_eval_latest.json, not repeated here.",
            "REV-EVA-05 cites a document withdrawn after indexing. No model sees the index, "
            "so a judge that clears it is not necessarily wrong -- read that row's "
            "explanation before treating it as a Reviewer defect.",
            "Nothing here is a stack test: the evidence is hand-built, so this measures the "
            "judge's reading of a given input, not the retrieval that would produce one.",
        ],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    def percent(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1%}"

    if payload.get("verdict") == "REFUSED":
        return (
            "# Reviewer semantic layer\n\n"
            f"- producer: {payload['producer']}\n"
            f"- observed: {payload['observed_at']}\n"
            "- verdict: **REFUSED**\n\n"
            "## Why\n\n"
            f"{payload['reason']}\n\n"
            "```json\n"
            + json.dumps(payload["provider_probe"], ensure_ascii=False, indent=2)
            + "\n```\n"
        )

    metrics = payload["metrics"]
    harness = payload["harness"]
    lines = [
        "# Reviewer semantic layer",
        "",
        f"- producer: {payload['producer']}",
        f"- observed: {payload['observed_at']}",
        f"- tool revision: `{payload['tool_revision']}`",
        f"- graph: `{payload['graph']}`",
        f"- judge model: `{payload['provider_probe']['model']}`",
        f"- cases in the deterministic harness: {harness['cases']}",
        f"- decided by the rule gate, no model call: {harness['decided_by_the_rule_gate']}",
        f"- deferred to the judge, measured here: {harness['deferred_to_the_judge']}",
        "",
        "## Headline",
        "",
        "| metric | value | reads as |",
        "| --- | --- | --- |",
        f"| Judge false reject rate | {percent(metrics['judge_false_reject_rate'])} | "
        f"{len(metrics['false_reject_cases'])}/{metrics['sound_cases']} sound analyses were "
        "blocked by the judge |",
        f"| Judge false accept rate | {percent(metrics['judge_false_accept_rate'])} | "
        f"{len(metrics['false_accept_cases'])}/{metrics['defective_cases']} defects the rules "
        "cannot name were cleared by the judge |",
        f"| Model calls | {metrics['model_calls']} | for {metrics['cases_run']} cases; "
        "the rule gate spent none |",
        "",
        "## Layer split",
        "",
        "| family | cases | caught | missed |",
        "| --- | --- | --- | --- |",
        f"| sound (must pass) | {metrics['sound_cases']} | "
        f"{metrics['sound_cases'] - len(metrics['false_reject_cases'])} | "
        f"{len(metrics['false_reject_cases'])} |",
        f"| evasion (must be caught) | {len(metrics['evasion_cases'])} | "
        f"{len(metrics['evasion_caught'])} | "
        f"{len(metrics['evasion_cases']) - len(metrics['evasion_caught'])} |",
        f"| semantic_only (must be caught) | {len(metrics['semantic_cases'])} | "
        f"{len(metrics['semantic_caught'])} | "
        f"{len(metrics['semantic_cases']) - len(metrics['semantic_caught'])} |",
        "",
        "## Cases",
        "",
        "| case | family | expected | decision | reason | conf | calls | verdict |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in payload["cases"]:
        judge = row["judge"] or {}
        confidence = judge.get("confidence")
        conf = "-" if confidence is None else f"{confidence:.2f}"
        if _provider_unavailable(row):
            verdict = "PROVIDER"
        elif row["family"] == "sound":
            verdict = "ok" if not row["flagged"] else "FALSE REJECT"
        else:
            verdict = "ok" if row["flagged"] else "FALSE ACCEPT"
        lines.append(
            f"| {row['id']} | {row['family']} | {row['expected']} | {row['decision']} | "
            f"{row['reason_code'] or '-'} | {conf} | {row['model_calls']} | {verdict} |"
        )
    lines += ["", "## Why each case exists", ""]
    for row in payload["cases"]:
        lines.append(f"- **{row['id']}** ({row['family']}): {row['why']}")
    if metrics["false_accept_cases"] or metrics["false_reject_cases"]:
        lines += ["", "## What the judge got wrong", ""]
        for row in payload["cases"]:
            if row["id"] in metrics["false_accept_cases"]:
                judge = row["judge"] or {}
                lines.append(
                    f"- **{row['id']}** cleared a real defect with "
                    f"`claims_supported={judge.get('claims_supported')}`, "
                    f"`action_consistent={judge.get('action_consistent')}`, "
                    f"`confidence={judge.get('confidence')}`: {judge.get('feedback', '')}"
                )
            if row["id"] in metrics["false_reject_cases"]:
                judge = row["judge"] or {}
                lines.append(
                    f"- **{row['id']}** blocked a sound analysis with "
                    f"`claims_supported={judge.get('claims_supported')}`, "
                    f"`action_consistent={judge.get('action_consistent')}`, "
                    f"`confidence={judge.get('confidence')}`: {judge.get('feedback', '')}"
                )
    lines += ["", "## Limitations", ""]
    for item in payload["limitations"]:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the markdown on disk matches the recorded json, without re-running",
    )
    args = parser.parse_args()

    if args.check:
        if not REPORT_JSON.exists():
            print(f"missing report: {REPORT_JSON}", file=sys.stderr)
            return 2
        payload = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
        if payload.get("verdict") == "REFUSED":
            print("the recorded report is a refusal, not a measurement", file=sys.stderr)
            return 3
        if not REPORT_MD.exists():
            print(f"missing markdown: {REPORT_MD}", file=sys.stderr)
            return 2
        if REPORT_MD.read_text(encoding="utf-8") != render_markdown(payload):
            print("markdown does not match the recorded json", file=sys.stderr)
            return 1
        print("check ok")
        return 0

    payload = asyncio.run(build())
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    if payload.get("verdict") == "REFUSED":
        stamp_refusal(
            REPORT_JSON,
            REPORT_MD,
            gate="reviewer-semantic",
            reason=str(payload["reason"]),
            argv=sys.argv[1:],
        )
        print(f"refused: {payload['reason']}", file=sys.stderr)
        print(json.dumps(payload["provider_probe"], ensure_ascii=False), file=sys.stderr)
        return 3
    REPORT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    REPORT_MD.write_text(render_markdown(payload), encoding="utf-8")
    metrics = payload["metrics"]
    print(f"deferred to the judge: {payload['harness']['deferred_to_the_judge']}")
    print(f"false reject rate: {metrics['judge_false_reject_rate']}")
    print(f"false accept rate: {metrics['judge_false_accept_rate']}")
    print(f"model calls: {metrics['model_calls']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
