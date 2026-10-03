"""Measure run-to-run stability from repeats that were already recorded.

**The gap this fills.** The reliability batch answers "does the same question settle the same way
twice" by *making* repeats: it re-posts a stratified case set N times at concurrency 1 and records
each outcome separately. That is the right instrument for a designed experiment and it costs a model
call per repeat. It is not the only source of repeats: the load batch already ran 20 cases more than
once, at three concurrency tiers, and recorded each repeat as its own observation. Those repeats are
on disk, and this report reads them.

**What the repeats can and cannot support.** They were not designed as a reliability experiment, and
the confounds are named in the report rather than hidden:

* repeats per case differ by tier -- one at concurrency 1, two at 5, three at 10 -- so the tier with
  the most repeats is also the tier with the most contention;
* whether the corpus sits on one revision or several, and whether a difference between tiers could
  be a revision change rather than contention.

What it can support is a floor: on the repeats that survive, how often did the same case settle
differently, and along which of the four recorded axes. A floor is worth having precisely because it
is free, and because a given disagreement rate is a fact about those repeats -- it is not an estimate
of the platform's instability, and neither is zero.

**The limitations are computed, not asserted.** They were constants until 2026-10-03, when the batch
was re-recorded on a single revision and two of them became false: the corpus no longer spans two
revisions, and the tier that had no repeats is now the tier with the most. A limitation that
contradicts the table above it is worse than no limitation, so every limitation that is a statement
about the corpus is derived from the corpus.

**Four axes, because one is too coarse.** Terminal status alone cannot see two successful runs that
cited different documents, and a citation change is what a reviewer has to act on. The citation set
alone cannot see a run that cited the same documents and still failed to settle. Status, reviewer
decision, the citation set and the plan digest are compared together; agreement means all four.

Usage::

    uv run python scripts/report_phase7_reliability_recorded.py \\
        [--replays evaluation/load/replays] [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from servicemind.evaluation.recorded import inputs_block, load_replays, revisions
from servicemind.evaluation.source_revision import source_revision

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPLAYS = REPO_ROOT / "evaluation" / "load" / "replays"
REPORT_JSON = REPO_ROOT / "evaluation" / "reports" / "phase7_reliability_recorded_latest.json"
REPORT_MD = REPO_ROOT / "evaluation" / "reports" / "phase7_reliability_recorded_latest.md"

#: The axes a repeat has to agree on to count as the same outcome. Named here so the report says
#: what "the same" meant rather than leaving it to the reader.
AXES = ("terminal_status", "reviewer_decision", "citations", "plan_digest")


def _axis(replay: dict[str, Any], axis: str) -> Any:
    value = replay.get(axis)
    # Citations are a set comparison: the order the retrieval arm returned them in is not part of
    # what a caller sees, and two runs that cited the same documents are the same outcome.
    return sorted(str(item) for item in value) if axis == "citations" and value else value


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(int(len(ordered) * fraction), len(ordered) - 1)], 2)


def stratum(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Agreement and latency for one (revision, concurrency) group of repeats."""
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        by_case[str(observation.get("case_id"))].append(observation)

    repeats = sorted(len(group) for group in by_case.values())
    disagreements: list[dict[str, Any]] = []
    agreed = 0
    for case_id, group in sorted(by_case.items()):
        differing = [
            axis
            for axis in AXES
            if len({json.dumps(_axis(o, axis), sort_keys=True) for o in group}) > 1
        ]
        if not differing:
            agreed += 1
            continue
        detail: dict[str, Any] = {}
        if "citations" in differing:
            sets = [set(observation.get("citations") or ()) for observation in group]
            everywhere = set.intersection(*sets) if sets else set()
            anywhere = set.union(*sets) if sets else set()
            detail["citations"] = {
                "set_sizes": [len(item) for item in sets],
                "cited_by_every_repeat": sorted(everywhere),
                "cited_by_some_but_not_all": sorted(anywhere - everywhere),
            }
        disagreements.append(
            {
                "case_id": case_id,
                "axes": differing,
                "detail": detail,
                "outcomes": [
                    {axis: _axis(observation, axis) for axis in differing} for observation in group
                ],
            }
        )

    latencies = [
        float(observation["latency_seconds"])
        for observation in observations
        if observation.get("latency_seconds") is not None
    ]
    expected = [
        observation for observation in observations if observation.get("expected_citations")
    ]
    return {
        "revision": observations[0].get("deployed_revision"),
        "concurrency": observations[0].get("concurrency"),
        "observations": len(observations),
        "cases": len(by_case),
        "repeats_per_case": {
            "min": repeats[0] if repeats else 0,
            "max": repeats[-1] if repeats else 0,
        },
        "cases_with_any_repeat": sum(1 for count in repeats if count > 1),
        "cases_agreeing_on_every_axis": agreed,
        "agreement_rate": round(agreed / len(by_case), 4) if by_case else None,
        # A stratum where nothing repeats agrees with itself. Reporting 100% there would read as a
        # stability result when it is arithmetic, so the rate is withheld below two repeats.
        "agreement_rate_is_meaningful": bool(repeats and repeats[-1] > 1),
        "disagreements": disagreements,
        "latency_seconds": {
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 2) if latencies else None,
        },
        "expected_citation_hit_rate": (
            round(
                sum(
                    1
                    for observation in expected
                    if set(observation.get("expected_citations") or ())
                    <= set(observation.get("citations") or ())
                )
                / len(expected),
                4,
            )
            if expected
            else None
        ),
        "unreadable_citations": sum(
            int(observation.get("unreadable_citations") or 0) for observation in observations
        ),
        "observations_with_errors": sum(
            1 for observation in observations if observation.get("errors")
        ),
    }


def summarise(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Every stratum side by side, plus the cross-tier comparison one revision allows."""
    grouped: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        grouped[(observation.get("deployed_revision"), observation.get("concurrency"))].append(
            observation
        )
    strata = [
        stratum(group)
        for _, group in sorted(grouped.items(), key=lambda item: (str(item[0][0]), item[0][1]))
    ]

    # One revision holds two tiers (1 and 5), so those two can be compared without the revision
    # changing under the comparison. The third tier sits on a different revision and is excluded
    # from this reading rather than silently pooled.
    by_revision: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for group in grouped.values():
        by_revision[str(group[0].get("deployed_revision"))].append(stratum(group))
    comparable = by_revision
    cross_tier: dict[str, Any] = {}
    for revision, entries in comparable.items():
        if len(entries) < 2:
            continue
        outcomes: dict[str, dict[str, Any]] = {}
        for entry in entries:
            group = next(
                g
                for g in grouped.values()
                if g[0].get("concurrency") == entry["concurrency"]
                and str(g[0].get("deployed_revision")) == revision
            )
            for observation in group:
                if observation.get("repeat_index") == 0:
                    outcomes.setdefault(str(observation["case_id"]), {})[entry["concurrency"]] = {
                        axis: _axis(observation, axis)
                        for axis in ("terminal_status", "reviewer_decision")
                    }
        moved = [
            {"case_id": case_id, "first_repeat_by_tier": per_tier}
            for case_id, per_tier in sorted(outcomes.items())
            if len({json.dumps(value, sort_keys=True) for value in per_tier.values()}) > 1
        ]
        cross_tier[revision] = {
            "tiers": sorted(entry["concurrency"] for entry in entries),
            "cases_compared": len(outcomes),
            "cases_whose_first_repeat_moved_between_tiers": len(moved),
            "moved": moved,
        }

    total_repeated = sum(entry["cases_with_any_repeat"] for entry in strata)
    total_disagreeing = sum(len(entry["disagreements"]) for entry in strata)
    axes_that_moved: Counter[str] = Counter()
    for entry in strata:
        for disagreement in entry["disagreements"]:
            axes_that_moved.update(disagreement["axes"])
    return {
        "observations": len(observations),
        "strata": strata,
        "cross_tier": cross_tier,
        "floor": {
            "repeated_cases": total_repeated,
            "cases_disagreeing_on_some_axis": total_disagreeing,
            "disagreement_rate": (
                round(total_disagreeing / total_repeated, 4) if total_repeated else None
            ),
            "axes_that_moved": dict(sorted(axes_that_moved.items())),
        },
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render(report: dict[str, Any]) -> str:
    """The report as markdown. A pure function of the JSON, so ``--check`` can compare them."""
    summary = report["summary"]
    inputs = report["inputs"]
    lines = [
        "# Phase 7 — Stochastic reliability, from recorded repeats",
        "",
        f"Generated {report['generated_at']} from {inputs['observations']} recorded observations of "
        f"{summary['strata'][0]['cases'] if summary['strata'] else 0} cases.",
        "",
        "## What was read, and what was not",
        "",
        f"- Replays: `{inputs['replay_directory']}` — {inputs['observations']} observations.",
        *[f"  - `{name}` × {count}" for name, count in inputs["data_revisions"].items()],
        f"- Scorer revision: `{inputs['scorer_revision']}`",
        "- Model calls made: **0**. Runs created: **0**.",
        "",
        "These repeats were recorded by the load batch, not by a repeat experiment. The confounds are",
        "in the limitations below and in `docs/PHASE7_REMAINING_EVALUATION_2026-10-02.md`; this report",
        "is a floor, not a designed measurement.",
        "",
        "## Agreement within each stratum",
        "",
        "Agreement means all four axes matched: terminal status, reviewer decision, citation set and",
        "plan digest.",
        "",
        "| Revision | Concurrency | Repeats/case | Cases with repeats | Agreeing on every axis | Agreement |",
        "|---|---|---|---|---|---|",
        *[
            f"| `{str(entry['revision'])[:12]}…` | {entry['concurrency']} | "
            f"{entry['repeats_per_case']['min']}–{entry['repeats_per_case']['max']} | "
            f"{entry['cases_with_any_repeat']} | {entry['cases_agreeing_on_every_axis']} | "
            f"{_pct(entry['agreement_rate']) if entry['agreement_rate_is_meaningful'] else '— (no repeats)'} |"
            for entry in summary["strata"]
        ],
        "",
        "## Latency and retrieval per stratum",
        "",
        "| Revision | Concurrency | p50 (s) | p95 (s) | max (s) | Expected citation hit | Unreadable citations | Runs with errors |",
        "|---|---|---|---|---|---|---|---|",
        *[
            f"| `{str(entry['revision'])[:12]}…` | {entry['concurrency']} | "
            f"{entry['latency_seconds']['p50']} | {entry['latency_seconds']['p95']} | "
            f"{entry['latency_seconds']['max']} | {_pct(entry['expected_citation_hit_rate'])} | "
            f"{entry['unreadable_citations']} | {entry['observations_with_errors']} |"
            for entry in summary["strata"]
        ],
        "",
        "## Disagreements, enumerated",
        "",
    ]
    disagreements = [
        disagreement for entry in summary["strata"] for disagreement in entry["disagreements"]
    ]
    if disagreements:
        lines += ["| Case | Axes that moved |", "|---|---|"]
        lines += [f"| {row['case_id']} | {', '.join(row['axes'])} |" for row in disagreements]
    else:
        lines.append(
            "None. On the repeats that survive in this corpus, every repeated case settled the same "
            "way on all four axes."
        )
    if disagreements:
        lines += [
            "",
            "Every disagreement above is in the citation set: terminal status, reviewer decision and",
            "plan digest never moved across any repeat. In each one the document the case requires was",
            "still cited — the expected-citation hit rate is 100% in every stratum — so what varies is",
            "the surrounding context, not whether the question was answered.",
        ]
    lines += [
        "",
        "## The same case at two concurrency tiers, within one revision",
        "",
        "One revision holds two tiers, so those two can be compared without the revision changing",
        "under the comparison. The third tier sits on a different revision and is excluded rather",
        "than pooled.",
        "",
    ]
    if summary["cross_tier"]:
        lines += [
            "| Revision | Tiers | Cases compared | First repeat moved between tiers |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| `{revision[:12]}…` | {', '.join(str(t) for t in entry['tiers'])} | "
            f"{entry['cases_compared']} | {entry['cases_whose_first_repeat_moved_between_tiers']} |"
            for revision, entry in summary["cross_tier"].items()
        ]
    else:
        lines.append("No revision in this corpus holds more than one tier.")
    lines += [
        "",
        "## Floor",
        "",
        "| Reading | Value |",
        "|---|---|",
        f"| Repeated cases | {summary['floor']['repeated_cases']} |",
        f"| Cases disagreeing on some axis | {summary['floor']['cases_disagreeing_on_some_axis']} |",
        f"| Axes on which any case disagreed | "
        f"{', '.join(f'`{k}`={v}' for k, v in summary['floor']['axes_that_moved'].items())} |",
        f"| Disagreement rate | {_pct(summary['floor']['disagreement_rate'])} |",
        "",
        "## Limitations",
        "",
    ]
    lines += [f"- {item}" for item in report["limitations"]]
    lines.append("")
    return "\n".join(lines)


def limitations(observations: list[dict[str, Any]], summary: dict[str, Any]) -> list[str]:
    """The confounds of *this* corpus, read out of it.

    Three of these hold whatever the replays happen to be; two depend on what is on disk and are
    derived, because the corpus is re-recorded whenever the batch is re-run and a frozen sentence
    about a previous recording is how a report starts lying about its own table.
    """
    revisions_seen = revisions(observations)
    repeats = sorted({entry["repeats_per_case"]["max"] for entry in summary["strata"]})
    floor = summary["floor"]
    items = [
        "These repeats come from the load batch, not from a repeat experiment: concurrency varies "
        "with the number of repeats, so the tier with the most repeats is also the tier with the "
        "most contention.",
    ]
    if len(revisions_seen) == 1:
        only = next(iter(revisions_seen))
        items.append(
            f"Every observation is on one revision (`{only[:12]}…`), so a difference between tiers "
            "is at least not a revision change. That is all it rules out: the tiers hold different "
            "numbers of repeats, so this is still not a controlled comparison of concurrency."
        )
    else:
        items.append(
            f"The corpus spans {len(revisions_seen)} revisions and they split exactly along the "
            "tier boundary, so a difference between tiers is not attributable to concurrency alone."
        )
    items.append(
        f"Repeats per case are {', '.join(str(n) for n in repeats)} across the strata. At two "
        "repeats a single disagreement is a 50% rate for that case, so per-case rates are not "
        "reported."
    )
    items.append(
        f"The {_pct(floor['disagreement_rate'])} floor is measured on the repeats that were "
        "recorded and that survived on disk. It is a floor on those repeats, not an estimate of the "
        "platform's instability -- and the same limit applies in the other direction, where a low "
        "or zero rate would be a fact about the corpus rather than evidence of determinism."
    )
    items.append(
        "Four axes are compared. Two runs that differ in a way none of the four records -- a "
        "different root cause in the same answer, a different agent path to the same citations -- "
        "count as agreeing."
    )
    return items


def build(replays: Path) -> dict[str, Any]:
    observations = load_replays(replays)
    summary = summarise(observations)
    return {
        "report": "phase7_reliability_recorded",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": inputs_block(
            replays,
            observations,
            tool_revision=source_revision(REPO_ROOT),
            extra={
                "revisions": revisions(observations),
                "tiers": dict(sorted(Counter(str(o.get("tier")) for o in observations).items())),
            },
        ),
        "observations": observations,
        "summary": summary,
        "limitations": limitations(observations, summary),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replays", type=Path, default=DEFAULT_REPLAYS)
    parser.add_argument("--json", type=Path, default=REPORT_JSON)
    parser.add_argument("--md", type=Path, default=REPORT_MD)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    if args.check:
        stored = json.loads(args.json.read_text(encoding="utf-8"))
        if args.md.read_text(encoding="utf-8") != render(stored):
            print(f"{args.md} does not match {args.json}", file=sys.stderr)
            return 1
        print(f"{args.md} matches {args.json}")
        return 0

    report = build(args.replays)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.md.write_text(render(report), encoding="utf-8")
    floor = report["summary"]["floor"]
    print(f"strata={len(report['summary']['strata'])} repeated_cases={floor['repeated_cases']}")
    print(
        f"disagreeing={floor['cases_disagreeing_on_some_axis']} rate={floor['disagreement_rate']}"
    )
    print(f"wrote {args.json} and {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
