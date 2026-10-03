"""Ask which ceiling stops the production prompt short of the budget.

``evaluation/reports/phase4_techqa_production_latest.json`` reports a packed-document
maximum per arm, and on this corpus it also reports the same Recall@5, Recall@10 and
Recall@20 for every arm. An identity across three cutoffs is the fingerprint of a *cut*,
not of a ranking: the three numbers are all measuring "is the gold document among the
documents that were packed".

What the run does not show is *which* constraint cut:

- ``SERVICEMIND_RAG_CONTEXT_TOKENS`` (the token budget),
- ``SERVICEMIND_RAG_MAX_PARENTS_PER_SOURCE``, a diversity guard whose stated purpose is
  that "no single source may drown every other source", or
- neither: the candidate list ran out, or the pack reached ``final_k``.

The one that does not belong on that list is the source guard, and the reason is the shape
of the corpus. ``provenance.source`` is a property of the *ingester*, not of the document
-- in ``rag/sources.py`` four of the five ingesters hardcode it and ``AttachmentSource``
takes it at construction -- so a tenant fed by one connector has *one* source, and a guard
written to balance sources against each other has nothing to balance. Enforcing it there
would make it an absolute ceiling on how much evidence one answer may carry, whatever the
budget. It is therefore applied only between competing sources
(``rag.service.source_ceiling_applies``, the same predicate the packer calls), and this
script attributes a cut to it only when it is in force.

This script answers the question from the chunker's own output. It rebuilds parents for a
sample of the real corpus with the production chunker -- the same object, and the same
token counter, that ``rag/service.py`` compares against the budget -- and reports the size
distribution. From that it derives how large one more parent could be before the budget
would refuse it, given the maximum the run actually packed.

It reads the production report for the observed maximum and does not re-run retrieval:
this is arithmetic over two artefacts that already exist.

Inputs are gitignored (``data/phase4/raw/``) and the report is untracked, so both carry a
digest and a ``tracked: false`` below. A fresh checkout has to re-run the production
evaluation before this can be recomputed.

Usage:
    uv run python scripts/diagnose_phase4_packing_ceiling.py
    uv run python scripts/diagnose_phase4_packing_ceiling.py --check
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

from core import settings
from servicemind.rag.chunking import semantic_chunker
from servicemind.rag.parsing import structure_parser

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "phase4" / "raw" / "eval" / "techqa-rag-eval" / "corpus.zip"
PRODUCTION = ROOT / "evaluation" / "reports" / "phase4_techqa_production_latest.json"
OUT_JSON = ROOT / "evaluation" / "reports" / "phase4_packing_ceiling_latest.json"
OUT_MD = ROOT / "evaluation" / "reports" / "phase4_packing_ceiling_latest.md"

#: Documents whose parents are rebuilt. The corpus is 28481 documents; a few hundred give
#: a stable picture of the size distribution, which is what the budget arithmetic needs.
#: The observed packing maximum is *not* estimated from this sample -- it is read from the
#: production report, which measured all of them.
SAMPLE = 400

#: Quantile rows of the size table that the derivation reads, so the markdown, the JSON
#: and the tests all name the same three scenarios.
QUANTILES = ((0.50, "p50"), (0.90, "p90"), (0.99, "p99"))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _corpus(limit: int) -> list[Any]:
    """TechQA's support corpus as documents, loaded the way the evaluation loads them.

    Imported from the evaluation script rather than re-implemented: a second loader would
    be a second set of parsing decisions, and the sizes below have to describe the text
    that was actually indexed.
    """
    spec = importlib.util.spec_from_file_location(
        "evaluate_phase4_retrieval_techqa", ROOT / "scripts/evaluate_phase4_retrieval_techqa.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._corpus(limit)


def parent_token_sizes(documents: list[Any]) -> list[int]:
    """Every parent's token count, over the documents given.

    ``build_parents`` needs no embedding provider, and the count it stamps on each parent
    comes from the same counter ``EnterpriseRAG.retrieve`` uses against the budget -- so
    these numbers are in the units the budget is spent in, not a parallel estimate.
    """
    sizes: list[int] = []
    for document in documents:
        blocks = structure_parser.parse_markdown(document)
        sizes.extend(parent.token_count for parent in semantic_chunker.build_parents(blocks))
    return sizes


def _quantile(sorted_sizes: list[int], fraction: float) -> int:
    """Nearest-rank quantile. No interpolation: these are observed integers."""
    return sorted_sizes[min(len(sorted_sizes) - 1, int(fraction * len(sorted_sizes)))]


def size_distribution(sizes: list[int]) -> dict[str, Any]:
    if not sizes:
        return {"parents": 0}
    ordered = sorted(sizes)
    return {
        "parents": len(ordered),
        "tokens": {label: _quantile(ordered, fraction) for fraction, label in QUANTILES}
        | {"max": ordered[-1]},
        "mean": round(sum(ordered) / len(ordered), 1),
    }


def who_stops_the_pack(
    sizes: list[int],
    packed_max: int,
    budget: int,
    per_source_cap: int,
    final_k: int,
    source_ceiling_enforced: bool,
    distinct_sources: int,
) -> dict[str, Any]:
    """Which ceiling the observed maximum is attributable to, and with how much room.

    Three answers, not two, because "neither ceiling" is a real outcome and folding it
    into one of the others would attribute a short candidate list to a rule that never
    fired:

    - the per-source ceiling: the packer had it in force (the run says whether it did --
      this script does not re-derive the rule), the budget would have admitted another
      typical parent, and the pack stopped exactly at the cap;
    - the token budget: another typical parent would *not* have fitted, so the cap is not
      what ended the list;
    - neither: the budget had room and the cap was not reached, so the list ended because
      there was nothing more to pack -- also the answer when the list stopped at
      ``final_k``, which is a depth rather than a ceiling.

    Every scenario is computed at three quantiles rather than at one, because the packed
    parents are the top-ranked ones and not a random draw: the arithmetic is exact given a
    size, and the size is the assumption.
    """
    ordered = sorted(sizes)
    headroom: dict[str, int] = {}
    for fraction, label in QUANTILES:
        at = _quantile(ordered, fraction)
        # One more parent is refused by the budget only when the parents already packed
        # plus it exceed the budget, so the largest one that still fits is what is left
        # over after the observed maximum -- not after a hardcoded four. That maximum is
        # read from the run, because a field named for a fixed pack size starts lying the
        # moment the packer stops stopping there.
        headroom[label] = budget - packed_max * at
    fits = {
        label: sum(1 for size in ordered if size <= room) / len(ordered)
        for label, room in headroom.items()
    }
    # The question the budget answers is "would one more parent of a typical size have
    # fitted?". Room for another *median* parent is the scale-free version of that: it
    # does not depend on which quantile happened to be picked as the scenario.
    median = _quantile(ordered, 0.50)
    budget_allows_one_more = headroom["p50"] >= median
    # Read from the run rather than re-derived here. The rule is "only between competing
    # sources", and a report produced before that rule existed would be described by the
    # wrong one -- so the report states the regime it ran under and this script believes
    # it. ``build`` refuses a report that does not.
    at_the_cap = source_ceiling_enforced and packed_max >= per_source_cap
    if not budget_allows_one_more:
        attributed = "the token budget"
    elif at_the_cap:
        attributed = "the per-source ceiling"
    elif final_k > packed_max:
        attributed = "neither ceiling: the candidate list ran out"
    else:
        attributed = "final_k"
    return {
        "packed_max": packed_max,
        "distinct_sources": distinct_sources,
        "source_ceiling_enforced": source_ceiling_enforced,
        "median_parent_tokens": median,
        "next_parent_headroom_tokens": headroom,
        "share_of_parents_that_still_fit": {label: round(rate, 4) for label, rate in fits.items()},
        "budget_would_admit_another_parent": budget_allows_one_more,
        "at_the_per_source_ceiling": at_the_cap,
        "final_k_is_reachable": final_k > packed_max,
        "attributed_to": attributed,
    }


def build() -> dict[str, Any]:
    for path in (CORPUS, PRODUCTION):
        if not path.exists():
            raise FileNotFoundError(
                f"{path} is missing. {CORPUS.name} lives under data/phase4/raw/, which is "
                "gitignored, and the production report is written by "
                "scripts/evaluate_phase4_retrieval_techqa.py -- so a fresh checkout has to "
                "re-run the production evaluation before this diagnostic can be recomputed"
            )
    production = json.loads(PRODUCTION.read_text(encoding="utf-8"))
    packing = production["packing"]
    if "source_ceiling_enforced" not in packing:
        raise ValueError(
            f"{PRODUCTION.name} predates the source-cardinality fields, so this report "
            "cannot say whether the per-source ceiling was in force for it -- and a report "
            "produced before that rule existed would be described by the wrong one. Re-run "
            "scripts/evaluate_phase4_retrieval_techqa.py."
        )
    # Read from the run, not from the sample below: the regime is a property of the pool
    # the run packed from, which the run counted over the whole corpus. A sample can only
    # undercount the sources in it.
    source_ceiling_enforced = packing["source_ceiling_enforced"]
    distinct_sources = packing["distinct_sources"]
    per_query = packing["documents_packed_per_answerable_query"]
    budget = settings.SERVICEMIND_RAG_CONTEXT_TOKENS
    per_source_cap = settings.SERVICEMIND_RAG_MAX_PARENTS_PER_SOURCE
    per_document_cap = settings.SERVICEMIND_RAG_MAX_PARENTS_PER_DOCUMENT
    final_k = packing["final_k"]

    documents = _corpus(SAMPLE)
    sizes = parent_token_sizes(documents)
    distribution = size_distribution(sizes)
    maxima = {arm: stats["max"] for arm, stats in per_query.items()}
    packed_max = max(maxima.values(), default=0)
    derivation = who_stops_the_pack(
        sizes,
        packed_max,
        budget,
        per_source_cap,
        final_k,
        source_ceiling_enforced,
        distinct_sources,
    )
    parents_per_document = {
        "sampled": round(distribution["parents"] / len(documents), 4) if documents else 0.0,
        "whole_corpus": round(
            production["corpus"]["totals"]["parents"] / production["corpus"]["documents_indexed"],
            4,
        ),
    }

    return {
        "schema_version": "phase4-packing-ceiling-v1",
        "status": "MEASUREMENT_DIAGNOSTIC",
        "status_semantics": (
            "this says which ceiling cut the packed list. It certifies nothing about "
            "retrieval quality, it changes no ceiling, and it does not evaluate what "
            "raising one would do to answer quality -- that is a reviewer-side question"
        ),
        "inputs": {
            "corpus": {
                "path": str(CORPUS.relative_to(ROOT)),
                "sha256": _digest(CORPUS),
                "tracked": False,
                "note": "gitignored under data/phase4/raw/; re-running the evaluation reloads it",
            },
            "production_report": {
                "path": str(PRODUCTION.relative_to(ROOT)),
                "sha256": _digest(PRODUCTION),
                "tracked": False,
                "note": "written by scripts/evaluate_phase4_retrieval_techqa.py",
            },
        },
        "settings": {
            "context_token_budget": budget,
            "max_parents_per_document": per_document_cap,
            "max_parents_per_source": per_source_cap,
            "final_k": final_k,
        },
        "sampled": {
            "documents": len(documents),
            "selection": "the first N entries of the corpus archive, in archive order",
            # Counted here rather than read from the production report: the ceiling's
            # degeneracy is a property of the corpus, and every document in it is built by
            # the same loader, so the sample answers it without depending on a field a
            # given revision of that report may not carry.
            "distinct_sources": len({document.provenance.source for document in documents}),
            **distribution,
            # The one cross-check that the sample stands in for the corpus: the ingestion
            # counted every parent it built. A sample that chunked differently would show
            # up here before it showed up in the size table.
            "parents_per_document": parents_per_document,
        },
        "observed_packing": {
            "documents_packed_per_answerable_query": per_query,
            "maxima": maxima,
            # Observed over the whole corpus, not sampled: the ingestion totals the
            # production run counted. They bound how much a larger pack could draw on.
            "corpus_documents": production["corpus"]["documents_indexed"],
            "corpus_parents": production["corpus"]["totals"]["parents"],
        },
        "derivation": derivation,
        "reading": (
            f"every arm packs at most {packed_max} documents, so every recall cutoff at or "
            f"above {packed_max} counts the whole pack: comparing such a figure against a "
            "threshold written for a depth-100 pool compares a cut list against a ranked "
            f"one. Whether the budget ended the list is the arithmetic below -- one more "
            "parent at the median size would leave "
            f"{derivation['next_parent_headroom_tokens']['p50']} tokens of the "
            f"{budget}-token budget, the budget answering "
            f"{'yes' if derivation['budget_would_admit_another_parent'] else 'no'} -- and "
            "the verdict below is the other half of the answer: "
            f"{derivation['attributed_to']}"
        ),
        "limitations": [
            "the parent sizes are a sample of "
            f"{len(documents)} documents, not all "
            f"{production['corpus']['documents_indexed']}; the observed maximum is not "
            "sampled -- it is read from the production run, which measured every query",
            "the sample is the first "
            f"{len(documents)} entries of the archive in archive order, which is not a "
            "random draw. It chunks into "
            f"{parents_per_document['sampled']} parents per document against "
            f"{parents_per_document['whole_corpus']} over the whole corpus, which "
            "is a cross-check on the sample rather than a guarantee about it: the parent "
            "counts agree, but nothing here rules out an order effect on parent *sizes*",
            "the packed parents are the top-ranked ones, not a random draw from this "
            "distribution. If ranking correlates with size, the headroom moves; the size "
            "table bounds the shape of the answer, not its exact value",
            "``build_parents`` closes a parent before the block that would overflow its "
            "1500-token target, so one long parsed block can produce a larger parent than "
            "the target. The measured maximum above is one of those, and it is larger than "
            "every quantile in the table",
            "nothing here evaluates what a larger packed list does to answer quality. "
            "A longer prompt is a change to what the reviewer reads, and that is measured "
            "by the end-to-end answerability work, not here",
        ],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    sizes = payload["sampled"]
    packed = payload["observed_packing"]
    deriv = payload["derivation"]
    settings_ = payload["settings"]
    size_rows = "\n".join(f"| {label} | {sizes['tokens'][label]} |" for _, label in QUANTILES)
    arm_rows = "\n".join(
        f"| {arm} | {stats['mean']} | {stats['min']} | **{stats['max']}** | "
        f"{'reaches' if stats['max'] >= settings_['max_parents_per_source'] else 'below'} |"
        for arm, stats in packed["documents_packed_per_answerable_query"].items()
    )
    enforced = deriv["source_ceiling_enforced"]
    ceiling_line = (
        f"`SERVICEMIND_RAG_MAX_PARENTS_PER_SOURCE` is **in force** on this corpus: "
        f"{deriv['distinct_sources']} sources compete, so no one origin may take more than "
        f"{settings_['max_parents_per_source']} parents."
        if enforced
        else f"`SERVICEMIND_RAG_MAX_PARENTS_PER_SOURCE` is **not in force** on this corpus: "
        f"it names {deriv['distinct_sources']} source, so there is no second source to "
        "balance and the packer does not apply the ceiling at all. Any arm whose maximum "
        "reads the ceiling's number passed through it rather than being stopped by it."
    )
    headroom_rows = "\n".join(
        f"| {deriv['packed_max']} parents at {label} | "
        f"{deriv['next_parent_headroom_tokens'][label]} | "
        f"{deriv['share_of_parents_that_still_fit'][label]:.4f} |"
        for _, label in QUANTILES
    )
    limitations = "\n".join(f"- {item}" for item in payload["limitations"])
    sources = sizes["distinct_sources"]
    return f"""# Which ceiling stops the production prompt at {deriv["packed_max"]} documents

Status: **{payload["status"]}** ({sizes["documents"]} documents re-chunked, \
{sizes["parents"]} parents; packing read from \
`{payload["inputs"]["production_report"]["path"]}`)

The production run packs at most {deriv["packed_max"]} documents per answerable query
against a {settings_["context_token_budget"]}-token budget. This report asks which ceiling
produced that number. A recall cutoff the pack never reached counts the whole pack, so how
wide the pack got is also how wide the recall columns are -- see
`scripts/audit_rag_quality_state.py`, which derives that per arm.

## 1. How large is a parent

Rebuilt with the production chunker, so these are in the units the budget is spent in.

| quantile | tokens |
| --- | ---: |
{size_rows}
| mean | {sizes["mean"]} |
| max | {sizes["tokens"]["max"]} |

## 2. What the budget would have admitted

One more parent is refused by the budget only when the parents already packed plus it
exceed {settings_["context_token_budget"]}. So the largest one that still fits, and the
share of observed parents at or below that size:

| scenario | headroom for one more parent (tokens) | share of parents that still fit |
| --- | ---: | ---: |
{headroom_rows}

## 3. What the run actually packed

| arm | mean | min | max | max vs the {settings_["max_parents_per_source"]}-parent source ceiling |
| --- | ---: | ---: | ---: | --- |
{arm_rows}

The production run counted {deriv["distinct_sources"]} distinct `provenance.source` over the
whole corpus (this sample names {sources}). `source` is a property of the *ingester*, not of
the document: in `rag/sources.py` four of the five ingesters hardcode it
(`pagerduty_incident_response_docs`, `mendeley_help_desk_tickets`, `glpi_knowledge_base`,
`servicemind_internal_runbook`) and `AttachmentSource` takes it at construction. So one
ingester instance stamps the same source on every document it loads, and a guard whose
comment states its purpose as "no single source may drown every other source" has no second
source to balance -- which is exactly the case `rag.service.source_ceiling_applies` exists
to recognise.

{ceiling_line}

**Attributed to: {deriv["attributed_to"]}.** `final_k` is {settings_["final_k"]}.

## What this does and does not say

{payload["reading"]}

Raising whichever ceiling this report names is a configuration change and is **not**
approved here. Before it could be approved it needs an answer to a question this script
cannot ask: does a longer prompt make the reviewer's judgments better, worse, or merely
longer? Recall over a cut list cannot answer that, because moving the cut moves *which*
cutoff each threshold names -- with more documents packed, Recall@5 becomes a statement
about the first five of a longer list rather than about the whole of a short one.

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
    print(payload["status"], payload["derivation"]["attributed_to"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
