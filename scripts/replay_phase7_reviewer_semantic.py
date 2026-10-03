#!/usr/bin/env python
"""Re-run the semantic layer's adjudication from the judge's *recorded* verdicts.

``scripts/evaluate_phase7_reviewer_semantic.py`` spends one model call per case, because
the judge is a model and only a model can answer "what did it say". Everything after that
is not a model: ``ReviewerAgent._adjudicate_node`` is a pure function of
``(judge verdict, analysis, evidence, retrieval_round, replan_count, max_replans)``. The
report already stores every verdict the judge returned, so the whole adjudication half of
that measurement can be reproduced **with no model call at all** -- which is what this does,
and what makes it usable as a regression probe while the fix for the contract gap is built.

It exists for one finding in particular. The report records the verdicts that let five
``evasion`` cases through, and each one is a judge that either narrated the defect in prose
while returning every structured field clear, or never had a field to record it in --
``claims_supported``/``unsupported_claim_ids`` are indexed by ``claim_id``, and all five
analyses carry an empty ``claims`` list. Replaying those verdicts here shows the loss is in
the *contract between the judge and the adjudicator*, not in the model's reading: the
adjudicator has no branch that can reach a defect the verdict does not name.

So this is a reproduction, not a re-measurement. Nothing here re-asks the model, and a
green run does **not** mean the semantic layer is correct -- it means the recorded verdicts
still adjudicate to the recorded decisions.

That property is what makes it a regression probe across a contract change, and it is also
what makes it dangerous. Replaying a verdict means *rebuilding* it, and a rebuild that drops
a field does not fail loudly: ``SemanticReview`` fills an omitted widened criterion with its
pre-widening value, so the rebuilt verdict reads as sound and the adjudicator is blamed for
clearing a case it would in fact have blocked. An earlier version of this file did exactly
that -- it rebuilt the verdict from the eight fields the contract carried before the
widening, and reported three evasion cases as ``PASSED``. The verdict is now rebuilt from
every field the adjudicator reads (:data:`JUDGE_FIELDS`), and a report missing any of them is
refused with exit 3 instead of defaulted.

Read the two artifacts together: ``..._prefix_2026-10-02.json`` is the pre-widening replay
(four evasion cases cleared by the judge, every one recorded ``PASSED``), and
``..._latest.json`` is the same corpus of cases replayed through the widened verdict. The
evasion rows are the thing that had to change, and they did.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

from servicemind.agents.reviewer import ReviewerAgent  # noqa: E402
from servicemind.domain.review import ReviewDecision  # noqa: E402

REPORT_JSON = REPO_ROOT / "evaluation/reports/phase7_reviewer_semantic_latest.json"
HARNESS = REPO_ROOT / "scripts/evaluate_phase7_reviewer.py"

#: The family whose rows this replay exists for. Named once, so the summary cannot drift
#: from the table it prints.
NARRATED_BUT_CLEARED = ("evasion",)


def _load_harness() -> Any:
    spec = importlib.util.spec_from_file_location("_phase7_reviewer_harness", HARNESS)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load the reviewer harness from {HARNESS}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Every field the judge is asked for and the adjudicator reads. The replay rebuilds the
#: verdict from the report's record of it, so all of these must be present in that record.
#:
#: A field that quietly falls back to its default is indistinguishable, in the output, from
#: a field the judge genuinely left out -- and the two mean opposite things. That is how the
#: first version of this file reported three evasion cases as ``PASSED``: it rebuilt the
#: verdict from the eight fields the contract carried before the widening, the three widened
#: criteria fell back to their status-quo values, and the widening read as if it had never
#: happened. The probe was measuring its own reconstruction, not the adjudicator.
JUDGE_FIELDS: tuple[str, ...] = (
    "claims_supported",
    "action_consistent",
    "prompt_injection_detected",
    "contradictions",
    "unsupported_claim_ids",
    "feedback",
    "confidence",
    "rating_supplied",
    "action_target_grounded",
    "citation_integrity_ok",
    "unbacked_assertions",
)

#: Keys the report carries that the judge does not answer -- they are how the runner recorded
#: the exchange, not part of the verdict. Listed rather than inferred so that a new judged
#: field shows up as "missing from the record" instead of being waved through.
_BOOKKEEPING_KEYS = ("missing_criteria",)


class RecordedVerdictIncomplete(RuntimeError):
    """A report is missing a field the adjudicator reads, so its verdict cannot be replayed."""


def _verdict(recorded: dict[str, Any]) -> Any:
    """Rebuild the judge's verdict object from the report's own record of it.

    Refuses a record that is missing any :data:`JUDGE_FIELDS` entry. ``SemanticReview``
    tolerates an omitted widened field by filling in its *pre-widening* value, which is the
    right default when adjudicating a live judge response and exactly the wrong one here:
    a report written before the contract grew would replay as though every case were sound.
    """
    from servicemind.agents.reviewer import SemanticReview

    absent = [name for name in JUDGE_FIELDS if name not in recorded]
    if absent:
        raise RecordedVerdictIncomplete(
            f"the recorded verdict is missing {absent}; replaying it would default those "
            "fields to their pre-widening values and understate the adjudicator"
        )
    return SemanticReview(
        claims_supported=recorded["claims_supported"],
        action_consistent=recorded["action_consistent"],
        prompt_injection_detected=recorded["prompt_injection_detected"],
        contradictions=list(recorded["contradictions"]),
        unsupported_claim_ids=list(recorded["unsupported_claim_ids"]),
        feedback=recorded["feedback"],
        confidence=recorded["confidence"],
        rating_supplied=recorded["rating_supplied"],
        action_target_grounded=recorded["action_target_grounded"],
        citation_integrity_ok=recorded["citation_integrity_ok"],
        unbacked_assertions=[dict(assertion) for assertion in recorded["unbacked_assertions"]],
    )


async def _adjudicate(agent: ReviewerAgent, case: dict[str, Any], verdict: Any) -> Any:
    state = dict(case["state"]())
    state["semantic"] = verdict
    state["model_calls"] = 1
    return (await agent._adjudicate_node(state))["result"]


def _cleared(verdict: Any) -> bool:
    """Whether the judge returned every criterion clear.

    Covers the widened criteria too -- before the widening this was the five original fields,
    and an evasion case the judge had flagged through one of the new fields would still have
    counted as "cleared by the judge".
    """
    return (
        verdict.claims_supported
        and not verdict.unsupported_claim_ids
        and not verdict.contradictions
        and not verdict.prompt_injection_detected
        and verdict.action_consistent
        and verdict.action_target_grounded
        and verdict.citation_integrity_ok
        and not verdict.unbacked_assertions
    )


def build(payload: dict[str, Any], harness: Any) -> dict[str, Any]:
    agent = ReviewerAgent()
    cases = {case["id"]: case for case in harness.CASES}
    rows: list[dict[str, Any]] = []
    for recorded in payload["cases"]:
        case = cases[recorded["id"]]
        verdict = _verdict(recorded["judge"])
        result = asyncio.run(_adjudicate(agent, case, verdict))
        decision = ReviewDecision(result.decision).name
        rows.append(
            {
                "id": recorded["id"],
                "family": recorded["family"],
                "recorded_decision": recorded["decision"],
                "replayed_decision": decision,
                "reproduced": decision == recorded["decision"],
                "judge_cleared": _cleared(verdict),
                "judge_gap_phrase": _names_a_defect(verdict.feedback),
                "feedback": verdict.feedback,
            }
        )
    evasion = [row for row in rows if row["family"] in NARRATED_BUT_CLEARED]
    return {
        "producer": "scripts/replay_phase7_reviewer_semantic.py",
        "source_report": str(REPORT_JSON.relative_to(REPO_ROOT)),
        "source_tool_revision": payload.get("tool_revision"),
        "judge_model": payload["provider_probe"]["model"],
        "model_calls": 0,
        "verdict_contract": {
            "fields_read": list(JUDGE_FIELDS),
            "source": "each case's recorded judge verdict",
            "refuses": "a record missing any of the above, rather than defaulting it",
        },
        "cases": rows,
        "summary": {
            "cases_replayed": len(rows),
            "reproduced": sum(1 for row in rows if row["reproduced"]),
            "mismatched": [row["id"] for row in rows if not row["reproduced"]],
            "evasion_replayed": len(evasion),
            "evasion_cleared_by_the_judge": sum(1 for row in evasion if row["judge_cleared"]),
            "evasion_cleared_despite_narrating_the_defect": [
                row["id"] for row in evasion if row["judge_cleared"] and row["judge_gap_phrase"]
            ],
        },
        "reading": (
            "The adjudicator is a pure function of the judge's verdict, so every decision here "
            "follows from the verdict alone. When an evasion case is cleared by the judge and "
            "still recorded as PASSED, the loss is in the verdict's vocabulary -- there is no "
            "branch that can reach a defect the verdict does not name. That is what the "
            "pre-widening replay shows and what the widened contract removed: the same judge "
            "readings now carry the defects the adjudicator needs to see them."
        ),
    }


#: Phrases that positively assert a gap between the analysis and its evidence.
_GAP_PHRASES = (
    "not carried by",
    "not supported by",
    "no evidence supports",
    "does not follow from",
    "is not scored",
    "since no claim",
)

#: Phrases that *deny* a defect. The judge writes these in boilerplate -- "no claims were
#: submitted for review, so there is nothing to mark unsupported" -- and a naive substring
#: match reads that as the judge naming an unsupported claim. The first version of this
#: file did exactly that and reported 5/5 evasion cases as "named the defect in prose" when
#: only one had. Sentences carrying any of these are dropped before the gap phrases are
#: looked for, so the count can only come from an affirmative statement.
_NEGATED = (
    "nothing to mark",
    "nothing unsupported",
    "nothing to flag",
    "no contradictions found",
    "not contradictory",
    "no unsupported claims",
)


def _sentences(feedback: str) -> list[str]:
    return [part.strip() for part in feedback.replace("\n", " ").split(".") if part.strip()]


def _names_a_defect(feedback: str) -> str | None:
    """The gap phrase the review used, or ``None``.

    Returns the phrase rather than a boolean so every row can be read back against the
    sentence it came from -- a count of "the judge saw it and said nothing" is worthless
    if the reader cannot check what was counted.
    """
    for sentence in _sentences(feedback):
        folded = sentence.casefold()
        if any(negation in folded for negation in _NEGATED):
            continue
        for phrase in _GAP_PHRASES:
            if phrase in folded:
                return phrase
    return None


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Reviewer semantic layer — replay of the recorded verdicts",
        "",
        f"- producer: {payload['producer']}",
        f"- source report: `{payload['source_report']}` (tool revision `{payload['source_tool_revision']}`)",
        f"- judge model: `{payload['judge_model']}`",
        f"- **model calls: {payload['model_calls']}**",
        "",
        "## Reproduction",
        "",
        f"- cases replayed: {summary['cases_replayed']}",
        f"- decisions reproduced: {summary['reproduced']}",
        f"- mismatched: {summary['mismatched'] or 'none'}",
        "",
        "## The evasion rows",
        "",
        f"Of {summary['evasion_replayed']} evasion cases, "
        f"{summary['evasion_cleared_by_the_judge']} were cleared by the judge, and "
        f"{len(summary['evasion_cleared_despite_narrating_the_defect'])} of those cleared it "
        "while asserting the gap in prose: "
        f"{', '.join(summary['evasion_cleared_despite_narrating_the_defect']) or 'none'}.",
        "",
        "| case | decision | judge cleared | the gap phrase in the review |",
        "| --- | --- | --- | --- |",
    ]
    for row in payload["cases"]:
        if row["family"] != "evasion":
            continue
        lines.append(
            f"| {row['id']} | {row['replayed_decision']} | "
            f"{'yes' if row['judge_cleared'] else 'no'} | "
            f"{row['judge_gap_phrase'] or '—'} |"
        )
    lines += [
        "",
        "## All rows",
        "",
        "| case | family | decision | reproduced |",
        "| --- | --- | --- | --- |",
    ]
    for row in payload["cases"]:
        lines.append(
            f"| {row['id']} | {row['family']} | {row['replayed_decision']} | "
            f"{'ok' if row['reproduced'] else 'MISMATCH'} |"
        )
    lines += ["", "## Reading", "", payload["reading"], ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="write evaluation/reports/phase7_reviewer_semantic_replay_latest.json",
    )
    args = parser.parse_args()
    if not REPORT_JSON.exists():
        print(f"missing report: {REPORT_JSON}", file=sys.stderr)
        return 2
    try:
        payload = build(json.loads(REPORT_JSON.read_text(encoding="utf-8")), _load_harness())
    except RecordedVerdictIncomplete as incomplete:
        print(f"{REPORT_JSON.name}: {incomplete}", file=sys.stderr)
        return 3
    summary = payload["summary"]
    print(f"replayed: {summary['cases_replayed']} cases, {payload['model_calls']} model calls")
    print(f"reproduced: {summary['reproduced']}/{summary['cases_replayed']}")
    if summary["mismatched"]:
        print(f"mismatched: {summary['mismatched']}", file=sys.stderr)
    print(
        "evasion cleared by the judge: "
        f"{summary['evasion_cleared_by_the_judge']}/{summary['evasion_replayed']}"
    )
    print(
        "  of which asserted the gap in prose: "
        f"{summary['evasion_cleared_despite_narrating_the_defect']}"
    )
    if args.write:
        out = REPO_ROOT / "evaluation/reports/phase7_reviewer_semantic_replay_latest.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        out.with_suffix(".md").write_text(render_markdown(payload))
        print(f"wrote {out.relative_to(REPO_ROOT)}")
    return 1 if summary["mismatched"] else 0


if __name__ == "__main__":
    sys.exit(main())
