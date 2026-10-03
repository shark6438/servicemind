"""Put the two TechQA production runs side by side, across the source-ceiling fix.

One production run was recorded with the per-source ceiling applied unconditionally
(``evaluation/reports/phase4_techqa_production_before_source_ceiling_fix.json``) and one
with it applied only between competing sources
(``evaluation/reports/phase4_techqa_production_latest.json``). This script is the pair of
them, and it exists because the second run on its own cannot be read: a packed count that
moved is not evidence that anything improved.

The reading the two columns support, and the one they do not:

- They support *how many documents reach the prompt* and *how much of the token budget the
  pack is now free to use*. Before, every arm stopped at exactly the cap with more than a
  third of the budget unspent; after, the pack continues past it.
- They do **not** support a claim that retrieval got better. Recall@5 before was computed
  over a pack of four documents; after, over a longer list. "Top 5" names a different
  quantity in each column, so the recall delta is partly a change of definition, and this
  script prints the packed counts beside every recall figure for exactly that reason.

The run context is checked, not assumed: a before/after across a different corpus, query
set or model revision compares two things and calls it one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BEFORE = ROOT / "evaluation/reports/phase4_techqa_production_before_source_ceiling_fix.json"
AFTER = ROOT / "evaluation/reports/phase4_techqa_production_latest.json"
OUT_JSON = ROOT / "evaluation/reports/phase4_source_ceiling_effect_latest.json"
OUT_MD = ROOT / "evaluation/reports/phase4_source_ceiling_effect_latest.md"

METRICS = ("recall_at_5", "recall_at_10", "recall_at_20", "mrr_at_10", "ndcg_at_10")
#: The cutoffs the run reports. Recall at each of them is only the same number before and
#: after if the pack is the same length, which is the thing that changed.
CUTOFFS = (5, 10, 20)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_context(report: dict[str, Any]) -> dict[str, Any]:
    """The facts that have to match for the two runs to be comparable at all."""
    return {
        "documents_indexed": report["corpus"]["documents_indexed"],
        "parents": report["corpus"]["totals"]["parents"],
        "queries": report["queries"]["total"],
        "answerable": report["queries"]["answerable"],
        "models": report["models"],
        "label_source": report["queries"].get("label_source"),
    }


def check_the_two_runs_are_comparable(before: dict[str, Any], after: dict[str, Any]) -> dict:
    """Refuse to publish a difference that a second variable could have caused.

    Everything except the code under test has to be identical, and the code under test has
    to be the only thing that *did* change -- in both directions. The two columns are only
    worth subtracting if the "before" run really had the ceiling binding the pack and the
    "after" run really does not, so those are checked against the pack the runs observed
    rather than against the field each run used to describe itself.

    An absent ``source_ceiling_enforced`` counts as *in force*, not as unknown: the field
    was added in the same change as the predicate, so a report that carries the source
    cardinality but not the field was produced by code that enforced the ceiling
    unconditionally. Reading the absence the other way would let a run that already had
    the fix be published as the "before" column.
    """
    left, right = _run_context(before), _run_context(after)
    differing = sorted(key for key in left if left[key] != right[key])
    if differing:
        raise ValueError(
            f"the two runs differ in {differing}; a before/after across them compares two things"
        )
    for label, report in (("before", before), ("after", after)):
        packing = report["packing"]
        if packing["distinct_sources"] != 1:
            raise ValueError(
                f"the {label} run names {packing['distinct_sources']} sources; this report "
                "is about the single-source case the fix changes"
            )
    if before["packing"].get("source_ceiling_enforced", True) is not True:
        raise ValueError(
            "the before run reports the ceiling as out of force, so it already contains "
            "the fix and cannot be the column the fix is measured against"
        )
    if after["packing"].get("source_ceiling_enforced") is not False:
        raise ValueError(
            "the after run still reports the ceiling as in force; the fix did not take "
            "effect in the run being compared"
        )
    # The self-description above is a field the run wrote about itself. This is the same
    # claim read off what the packer actually did: bound at the cap before, past it after.
    for label, report, expect_binding in (
        ("before", before, True),
        ("after", after, False),
    ):
        cap = report["packing"]["max_parents_per_source"]
        observed = max(
            block["max"]
            for block in report["packing"]["documents_packed_per_answerable_query"].values()
        )
        if (observed <= cap) is not expect_binding:
            raise ValueError(
                f"the {label} run packed at most {observed} documents against a "
                f"{cap}-parent ceiling, which is not what the {label} side of this "
                f"comparison requires ({'bound by' if expect_binding else 'past'} the cap)"
            )
    return {"before": left, "after": right}


def _arm(report: dict[str, Any]) -> dict[str, Any]:
    packed = report["packing"]["documents_packed_per_answerable_query"]
    results = report["results"]
    return {
        arm: {
            **{metric: round(results[arm][metric], 4) for metric in METRICS},
            "packed_mean": packed[arm]["mean"],
            "packed_min": packed[arm]["min"],
            "packed_max": packed[arm]["max"],
            # Whether the three recall cutoffs are one measurement: true exactly when the
            # pack never reached any of them, i.e. when the cut is what produced the pack.
            "cutoffs_are_one_measurement": len(
                {round(results[arm][f"recall_at_{k}"], 6) for k in CUTOFFS}
            )
            == 1,
        }
        for arm in sorted(results)
    }


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    context = check_the_two_runs_are_comparable(before, after)
    left, right = _arm(before), _arm(after)
    cap = before["packing"]["max_parents_per_source"]
    arms = {
        arm: {
            "before": left[arm],
            "after": right[arm],
            "delta": {
                **{m: round(right[arm][m] - left[arm][m], 4) for m in METRICS},
                "packed_mean": round(right[arm]["packed_mean"] - left[arm]["packed_mean"], 3),
                "packed_max": right[arm]["packed_max"] - left[arm]["packed_max"],
            },
        }
        for arm in left
    }
    before_at_cap = sorted(a for a, row in left.items() if row["packed_max"] >= cap)
    return {
        "arms": arms,
        "headline": {
            "ceiling": cap,
            "context_token_budget": after["packing"]["context_token_budget"],
            "final_k": after["packing"]["final_k"],
            "before_packed_max": max(row["packed_max"] for row in left.values()),
            "after_packed_max": max(row["packed_max"] for row in right.values()),
            "before_arms_stopped_at_the_ceiling": before_at_cap,
            "after_arms_stopped_at_the_ceiling": sorted(
                a for a, row in right.items() if row["packed_max"] >= cap
            ),
            "before_cutoffs_are_one_measurement": all(
                row["cutoffs_are_one_measurement"] for row in left.values()
            ),
            "after_cutoffs_are_one_measurement": all(
                row["cutoffs_are_one_measurement"] for row in right.values()
            ),
        },
        "run_context": context,
    }


def build() -> dict[str, Any]:
    for path in (BEFORE, AFTER):
        if not path.exists():
            raise FileNotFoundError(
                f"{path} is missing. Both are written by "
                "scripts/evaluate_phase4_retrieval_techqa.py, and the 'before' one has to be "
                "preserved by hand from a run of the pre-fix revision: re-running the "
                "current code cannot reproduce it"
            )
    before = json.loads(BEFORE.read_text(encoding="utf-8"))
    after = json.loads(AFTER.read_text(encoding="utf-8"))
    comparison = compare(before, after)
    headline = comparison["headline"]
    return {
        "schema_version": "phase4-source-ceiling-effect-v1",
        "status": "MEASUREMENT_DIAGNOSTIC",
        "status_semantics": (
            "this records what changed between two runs. It certifies no retrieval quality, "
            "and the recall columns are not comparable across it: the pack grew, so each "
            "cutoff names a different quantity in the two halves"
        ),
        "change": {
            "what": (
                "SERVICEMIND_RAG_MAX_PARENTS_PER_SOURCE is applied only when the candidate "
                "pool names more than one source, through the shared predicate "
                "servicemind.rag.service.source_ceiling_applies. The ceiling is a fairness "
                "bound between sources; on a single-source corpus it had nothing to balance "
                "and became a ceiling on the whole prompt"
            ),
            "kind": "semantics of the guard, not the value of the setting",
            "setting_value_unchanged": headline["ceiling"],
        },
        "inputs": {
            "before_report": {
                "path": str(BEFORE.relative_to(ROOT)),
                "sha256": _digest(BEFORE),
                "tracked": False,
                "note": "a pre-fix run, preserved by hand; the current code cannot regenerate it",
            },
            "after_report": {
                "path": str(AFTER.relative_to(ROOT)),
                "sha256": _digest(AFTER),
                "tracked": False,
                "note": "written by scripts/evaluate_phase4_retrieval_techqa.py",
            },
        },
        "run_context": comparison["run_context"],
        "headline": headline,
        "arms": comparison["arms"],
        "reading": (
            f"before, every arm packed at most {headline['before_packed_max']} documents -- "
            f"the ceiling of {headline['ceiling']} -- with the budget of "
            f"{headline['context_token_budget']} tokens more than a third unspent; after, the "
            f"pack continues to {headline['after_packed_max']} and the budget is what bounds "
            "it. The recall columns moved, and the move is not evidence of better retrieval: "
            "with a longer pack, Recall@5 counts the first five of a list that no longer "
            "stops at four. Read each recall beside the packed mean on the same row"
        ),
        "limitations": [
            "neither run records the tokens a pack actually spent, only how many documents it "
            "held. The budget arithmetic is measured separately, over parent sizes rebuilt "
            "with the production chunker, in "
            "evaluation/reports/phase4_packing_ceiling_latest.json",
            "the hybrid arm issues LLM-written lexical sub-queries, so it is not reproducible "
            "run to run; a repeated run of the *same* revision moved its Recall@5 by about "
            "0.004 (one query in 280), which is the floor on any hybrid delta here, before "
            "and after alike",
            "one corpus, one label set, one model revision. Nothing here generalises to a "
            "corpus with more than one source, where the ceiling is in force in both halves "
            "and the fix changes nothing",
        ],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    arms = payload["arms"]
    head = payload["headline"]
    rows = "\n".join(
        f"| {arm} | {row['before']['packed_mean']} | **{row['before']['packed_max']}** | "
        f"{row['after']['packed_mean']} | **{row['after']['packed_max']}** | "
        f"{row['after']['packed_max'] - row['before']['packed_max']:+d} |"
        for arm, row in arms.items()
    )
    recall_rows = "\n".join(
        f"| {arm} | {row['before']['recall_at_5']:.4f} | {row['after']['recall_at_5']:.4f} | "
        f"{row['delta']['recall_at_5']:+.4f} | {row['before']['recall_at_10']:.4f} | "
        f"{row['after']['recall_at_10']:.4f} | {row['delta']['recall_at_10']:+.4f} |"
        for arm, row in arms.items()
    )
    limitations = "\n".join(f"- {item}" for item in payload["limitations"])
    context = payload["run_context"]["before"]
    return f"""# The per-source ceiling, before and after

Status: **{payload["status"]}** — two TechQA production runs over
{context["documents_indexed"]} documents and {context["queries"]} queries
({context["answerable"]} answerable), identical in corpus, query set and model revisions.

| arm | packed mean before | packed max before | packed mean after | packed max after | delta |
| --- | ---: | ---: | ---: | ---: | ---: |
{rows}

The ceiling is {head["ceiling"]} parents per source and the value was **not** changed. What
changed is when it applies: {payload["change"]["what"]}.

Before, all {len(head["before_arms_stopped_at_the_ceiling"])} arms stopped at exactly
{head["before_packed_max"]}; after, the pack continues to {head["after_packed_max"]} and the
token budget of {head["context_token_budget"]} is what bounds it.

## The recall columns, which are not comparable

| arm | R@5 before | R@5 after | delta | R@10 before | R@10 after | delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
{recall_rows}

{payload["reading"]}

**Every arm on the before side returned one number at all three recall cutoffs**
(`before_cutoffs_are_one_measurement` = `{head["before_cutoffs_are_one_measurement"]}`,
computed from the runs, not asserted here). On the after side that identity
{"" if head["after_cutoffs_are_one_measurement"] else "no longer "}holds
(`{head["after_cutoffs_are_one_measurement"]}`). This is the clearest statement of what the
fix did to the *measurement*: the wider cutoffs stopped being a second name for the pack.
It is also why the delta column cannot be read as retrieval improving on its own -- where a
cutoff was previously unreachable, part of the movement is the column starting to measure
something it could not measure before.

## Limitations

{limitations}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    payload = build()
    json_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    markdown = render_markdown(payload)
    if args.check:
        matches = (
            OUT_JSON.exists()
            and OUT_MD.exists()
            and OUT_JSON.read_text(encoding="utf-8") == json_text
            and OUT_MD.read_text(encoding="utf-8") == markdown
        )
        print("PASS" if matches else "FAIL", payload["status"])
        return 0 if matches else 1
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json_text, encoding="utf-8")
    OUT_MD.write_text(markdown, encoding="utf-8")
    print(
        payload["status"],
        payload["headline"]["before_packed_max"],
        "->",
        payload["headline"]["after_packed_max"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
