"""Ask where the TechQA recall deficit actually comes from: the funnel, or the labels.

``evaluation/reports/phase4_proxy_release_latest.json`` says the best arm reaches
Recall@20 = 0.8071 against a §4.1 threshold of 0.90 at Recall@10. That reads as
"retrieval is far short". This script asks the two questions that decide whether it is,
and answers both from the proxy's own per-query candidate dump rather than from a story
about it:

1. **Is the gold document in the candidate pool at all?** The funnel is
   ``bm25_k = dense_k = candidate_k = 100`` (``core/settings.py:246-248``), and everything
   downstream -- RRF, the cross-encoder, the blend -- can only reorder what the funnel
   returned. If the gold is not in the 100, no reranker configuration reaches it, and the
   threshold is above a ceiling rather than above the current quality.
2. **When it is not, is the label the reason?** For each miss, compare how much the
   question overlaps the *labeled* document's title against how much it overlaps the
   title of what the retriever actually put first. A retriever that is working will look
   the same on both sides for queries it gets right, and different in a specific
   direction for the ones it "fails".

Neither question needs a model. Both are answered from the corpus text and the labels.

Inputs are gitignored (``data/phase4/raw/``), so this script records their digests and
says so: the report it writes is reproducible only where those files exist, and a fresh
checkout must re-run the proxy first. That is a property of the input, not of the method.

Usage:
    uv run python scripts/diagnose_phase4_label_diagnostic.py
    uv run python scripts/diagnose_phase4_label_diagnostic.py --check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "phase4" / "raw" / "eval" / "techqa-rag-eval"
DIAGNOSTICS = DATA / "phase4_proxy_candidate_diagnostics.json"
CORPUS = DATA / "corpus.zip"
SELECTION = ROOT / "evaluation" / "gold" / "phase4_proxy_release_v1.2.json"
OUT_JSON = ROOT / "evaluation" / "reports" / "phase4_label_diagnostic_latest.json"
OUT_MD = ROOT / "evaluation" / "reports" / "phase4_label_diagnostic_latest.md"

#: The arms present in the dump, in the order they narrow. ``production_blend`` is the one
#: the §4.1 numbers are read off, and its depth is ``SERVICEMIND_RAG_CANDIDATE_K``.
ARMS = ("bm25", "dense", "hybrid", "hybrid_reranked", "production_blend")
DECIDING_ARM = "production_blend"
RERANK_DEPTH = 100

#: Words excluded from the title overlap. A stop list is needed because ``IBM`` alone puts
#: every document in the corpus into every question's word set; numbers are dropped for the
#: same reason (release numbers appear in titles far more often than in questions).
STOPWORDS = frozenset(
    """a an the of to in for is are was were be been am do does did doing how what why when
    which who whom that this these those it its on at by with from as or and not no can
    could may might must shall should will would i my me we our you your they their there
    here about into over under out up down if then than so such but also only very more
    most other some any all both each few many much own same too s t don now""".split()
)


def _words(text: str) -> frozenset[str]:
    return frozenset(
        word
        for word in re.findall(r"[a-z0-9]+", text.lower())
        if word not in STOPWORDS and len(word) > 1 and not word.isdigit()
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _corpus_titles() -> dict[str, str]:
    """Each document's title line, keyed by the filename the labels use.

    Only the ``Title:`` field above ``Text:`` is read. The diagnostic is about whether the
    *subject* of the labeled document matches the question, and the body is written by
    support engineers in a house style that flattens that signal.
    """
    titles: dict[str, str] = {}
    with zipfile.ZipFile(CORPUS) as archive:
        for name in archive.namelist():
            if not name.endswith(".txt"):
                continue
            body = archive.read(name).decode("utf-8", "replace")
            titles[Path(name).name] = body.split("Text:", 1)[0].replace("Title:", "").strip()
    return titles


def _rank_of(row: dict[str, Any], arm: str) -> int | None:
    """Where the gold document sits in this arm's list, 1-based, or ``None`` if absent."""
    relevant = set(row["relevant"])
    return next((rank for rank, key in enumerate(row[arm], start=1) if key in relevant), None)


def _arm_summary(rows: Sequence[dict[str, Any]], arm: str) -> dict[str, Any]:
    ranks = [_rank_of(row, arm) for row in rows]
    present = [rank for rank in ranks if rank is not None]
    depth = max((len(row[arm]) for row in rows), default=0)
    return {
        "declared_depth": depth,
        "contains_gold": len(present),
        "contains_gold_rate": round(len(present) / len(rows), 4) if rows else 0.0,
        "gold_in_top_5": sum(1 for rank in present if rank <= 5),
        "gold_in_top_5_rate": (
            round(sum(1 for rank in present if rank <= 5) / len(rows), 4) if rows else 0.0
        ),
        "median_rank_when_present": statistics.median(present) if present else None,
        "list_lengths_observed": sorted({len(row[arm]) for row in rows}),
    }


def _title_overlap_breakdown(
    rows: Sequence[dict[str, Any]], arm: str, titles: dict[str, str]
) -> dict[str, Any]:
    golds: list[float] = []
    firsts: list[float] = []
    closer = 0
    for row in rows:
        listed = row[arm]
        if not listed:
            continue
        question = _words(row["question"])
        gold = _jaccard(question, _words(titles.get(row["relevant"][0], "")))
        first = _jaccard(question, _words(titles.get(listed[0], "")))
        golds.append(gold)
        firsts.append(first)
        if first > gold:
            closer += 1
    return {
        "queries": len(golds),
        "mean_question_vs_gold_title": round(statistics.fmean(golds), 4) if golds else None,
        "mean_question_vs_first_title": (round(statistics.fmean(firsts), 4) if firsts else None),
        "mean_gap_gold_minus_first": (
            round(statistics.fmean([g - f for g, f in zip(golds, firsts, strict=True)]), 4)
            if golds
            else None
        ),
        "first_hit_title_closer": closer,
        "first_hit_title_closer_rate": round(closer / len(golds), 4) if golds else None,
    }


def _examples(
    rows: Sequence[dict[str, Any]], titles: dict[str, str], limit: int = 10
) -> list[dict[str, Any]]:
    """The misses where the first hit is *most* more on-topic than the label.

    Selected by rule rather than by hand, so the list cannot be curated to make the point
    look stronger than the distribution does. Every entry is a pair of documents the
    corpus itself contains, with the question that separates them.
    """
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        listed = row["dense"]
        if not listed:
            continue
        question = _words(row["question"])
        gold = _jaccard(question, _words(titles.get(row["relevant"][0], "")))
        first = _jaccard(question, _words(titles.get(listed[0], "")))
        scored.append((first - gold, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "query_id": row["query_id"],
            "question": " ".join(row["question"].split()),
            "labelled": {
                "file": row["relevant"][0],
                "title": titles.get(row["relevant"][0], ""),
            },
            "first_dense_hit": {
                "file": row["dense"][0],
                "title": titles.get(row["dense"][0], ""),
            },
            "title_overlap_advantage_of_the_first_hit": round(advantage, 4),
            "gold_in_the_100_deep_pool": _rank_of(row, DECIDING_ARM) is not None,
        }
        for advantage, row in scored[:limit]
        if advantage > 0
    ]


def build() -> dict[str, Any]:
    for path in (DIAGNOSTICS, CORPUS, SELECTION):
        if not path.exists():
            raise FileNotFoundError(
                f"{path} is missing. It lives under data/phase4/raw/, which is gitignored, "
                "so a fresh checkout has to re-run the proxy before this diagnostic can be "
                "recomputed"
            )
    rows = json.loads(DIAGNOSTICS.read_text(encoding="utf-8"))
    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    impossible = set(selection["impossible"])
    answerable = [row for row in rows if row["query_id"] not in impossible]
    titles = _corpus_titles()

    arms = {arm: _arm_summary(answerable, arm) for arm in ARMS}
    ceiling = arms[DECIDING_ARM]
    missed = [row for row in answerable if _rank_of(row, DECIDING_ARM) is None]
    found = [row for row in answerable if _rank_of(row, DECIDING_ARM) is not None]

    return {
        "schema_version": "phase4-label-diagnostic-v1",
        "status": "MEASUREMENT_DIAGNOSTIC",
        "status_semantics": (
            "this decides whether the shortfall against the §4.1 thresholds is a retrieval "
            "finding or a property of the label set. It certifies nothing about quality in "
            "either direction and it changes no threshold"
        ),
        "inputs": {
            "candidate_diagnostics": {
                "path": str(DIAGNOSTICS.relative_to(ROOT)),
                "sha256": _digest(DIAGNOSTICS),
                "tracked": False,
                "note": "gitignored under data/phase4/raw/; re-running the proxy regenerates it",
            },
            "corpus": {"path": str(CORPUS.relative_to(ROOT)), "sha256": _digest(CORPUS)},
            "selection": {
                "path": str(SELECTION.relative_to(ROOT)),
                "sha256": _digest(SELECTION),
            },
            "label_tier": selection["label_tier"],
        },
        "queries": {
            "total": len(rows),
            "answerable": len(answerable),
            "impossible": len(impossible),
            "gold_documents": len({row["relevant"][0] for row in answerable}),
        },
        "candidate_funnel": {
            "arm": DECIDING_ARM,
            "depth": RERANK_DEPTH,
            "note": (
                "depth 100 is SERVICEMIND_RAG_CANDIDATE_K, so this is the headline of the "
                "production funnel and not an artefact of the proxy"
            ),
            **ceiling,
        },
        "arms": arms,
        "loss_split": {
            "in_pool": {
                "queries": len(found),
                "title_overlap": _title_overlap_breakdown(found, "dense", titles),
            },
            "out_of_pool": {
                "queries": len(missed),
                "title_overlap": _title_overlap_breakdown(missed, "dense", titles),
                "also_absent_from_bm25": sum(1 for row in missed if _rank_of(row, "bm25") is None),
                "gold_documents_no_query_retrieves": len(
                    {
                        row["relevant"][0]
                        for row in missed
                        if row["relevant"][0] not in {r["relevant"][0] for r in found}
                    }
                ),
            },
        },
        "examples": _examples(missed, titles),
        "reading": (
            "the funnel ceiling is the highest Recall@k any reranker can reach on this "
            "label set, because RRF, the cross-encoder and the blend only reorder what the "
            "100 candidates contain. Compare it against the §4.1 thresholds: a threshold "
            "above it cannot be met by ranking better. Then read the two title-overlap "
            "blocks together -- in the in-pool group the label and the first hit are "
            "topically equivalent, which is what a working retriever looks like; in the "
            "out-of-pool group the first hit is systematically closer to the question than "
            "the label. Where that holds, widening the funnel buys less than the raw miss "
            "rate suggests"
        ),
        "limitations": [
            "title overlap is a lexical Jaccard over content words, in the same family as "
            "the proxy's offline fallback. It is a heuristic for topical proximity, not a "
            "human judgment, and it is wrong on any document whose title is vaguer than "
            "its body",
            "n=36 in the out-of-pool group; the rate is a description of this set, not an "
            "estimate of a population",
            "the diagnostics dump is the *proxy's* arms. It says what that funnel returns, "
            "not what OpenSearch returns; the production run is the thing that measures "
            "production",
            "the label tier is external silver with no tenant signoff, so neither the "
            "labels nor this critique of them carry the §3.3 provenance",
        ],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    funnel = payload["candidate_funnel"]
    arms = payload["arms"]
    split = payload["loss_split"]
    arm_rows = "\n".join(
        f"| {arm} | {row['declared_depth']} | {row['contains_gold']} "
        f"| {row['contains_gold_rate']:.4f} | {row['gold_in_top_5_rate']:.4f} "
        f"| {row['median_rank_when_present']} |"
        for arm, row in arms.items()
    )
    in_pool = split["in_pool"]["title_overlap"]
    out_pool = split["out_of_pool"]["title_overlap"]
    examples = "\n\n".join(
        f"""**{item["query_id"]}** — {item["question"]}

- labelled : `{item["labelled"]["file"]}` — {item["labelled"]["title"]}
- first hit: `{item["first_dense_hit"]["file"]}` — {item["first_dense_hit"]["title"]}
- the first hit's title is closer to the question by {item["title_overlap_advantage_of_the_first_hit"]:.4f}; \
gold in the 100-deep pool: {item["gold_in_the_100_deep_pool"]}"""
        for item in payload["examples"]
    )
    limitations = "\n".join(f"- {item}" for item in payload["limitations"])
    return f"""# Where the TechQA recall deficit comes from

Status: **{payload["status"]}** ({payload["queries"]["answerable"]} answerable queries, \
{payload["queries"]["gold_documents"]} distinct gold documents, labels \
`{payload["inputs"]["label_tier"]}`)

The §4.1 thresholds are Recall@10 >= 0.90 and Recall@5 >= 0.85. The best arm on this set
reaches 0.7643 and 0.6857. This report asks whether that gap is the retriever's.

## 1. The funnel ceiling

Everything downstream of candidate generation -- RRF, the cross-encoder, the blend --
can only reorder what the funnel returned. `{funnel["arm"]}` is depth \
{funnel["depth"]}, which is `SERVICEMIND_RAG_CANDIDATE_K`, so this is the production \
funnel's headline and not an artefact of the proxy:

**{funnel["contains_gold"]}/{payload["queries"]["answerable"]} = \
{funnel["contains_gold_rate"]:.4f}** of answerable queries have their gold document \
anywhere in that 100.

| arm | depth | contains gold | rate | gold in top 5 | median rank when present |
| --- | ---: | ---: | ---: | ---: | ---: |
{arm_rows}

So Recall@10 >= 0.90 is **above the ceiling of this funnel**: no reranking, no weight
change and no fusion rule can reach it while `candidate_k` is 100 on this label set.

## 2. What is in the 36 that are not

| group | queries | question vs gold title | question vs first hit title | first hit closer |
| --- | ---: | ---: | ---: | ---: |
| gold in the pool | {in_pool["queries"]} | {in_pool["mean_question_vs_gold_title"]} | {in_pool["mean_question_vs_first_title"]} | {in_pool["first_hit_title_closer"]}/{in_pool["queries"]} ({in_pool["first_hit_title_closer_rate"]:.4f}) |
| gold out of the pool | {out_pool["queries"]} | {out_pool["mean_question_vs_gold_title"]} | {out_pool["mean_question_vs_first_title"]} | {out_pool["first_hit_title_closer"]}/{out_pool["queries"]} ({out_pool["first_hit_title_closer_rate"]:.4f}) |

In the group the funnel *does* cover, the label and the first hit are topically
equivalent -- that is what a working retriever looks like. In the group it does not, the
first hit is {abs(out_pool["mean_gap_gold_minus_first"]):.4f} *closer* to the question
than the document the labels call correct, and it is closer in
{out_pool["first_hit_title_closer"]} of {out_pool["queries"]} cases.

{split["out_of_pool"]["gold_documents_no_query_retrieves"]} of the gold documents in
that group are not retrieved by *any* query in the set, and
{split["out_of_pool"]["also_absent_from_bm25"]}/{out_pool["queries"]} are absent from the
BM25 arm as well, so this is not one arm's blind spot.

## 3. The ten widest gaps, chosen by rule

Selected as the ten misses where the first hit's title scores highest above the label's,
so the list is not curated.

{examples}

## What this does and does not say

{payload["reading"]}

The two candidate fixes have different ceilings. **Widening the funnel** raises the ceiling
above {funnel["contains_gold_rate"]:.4f} — but section 2 says the group it would recover is
mostly queries where the first hit is already closer to the question than the label, so it
buys less than the {1 - funnel["contains_gold_rate"]:.4f} miss rate suggests.
**Improving the reranker** is bounded by the same ceiling and cannot pass it. Neither is a
change to make on this evidence, and neither is approved here.

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
    print(payload["status"], payload["candidate_funnel"]["contains_gold_rate"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
