"""Run the Phase 5 memory quality evaluation over the committed scenario corpus.

Offline and deterministic: the harness drives the real governance policy, the real
repository state machine and the real retriever against ``evaluation/memory/scenarios.v2.json``
with no database, no model server and no network. Writes
``evaluation/reports/phase5_memory_latest.{json,md}``.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/evaluate_phase5_memory.py

``--embedding tei`` swaps the retriever's lexical fallback for the production embedding
provider, and writes to ``phase5_memory_tei_latest.{json,md}`` so it cannot overwrite
the published lexical report in place. The report records both the mode and the exact
scorer (``BAAI/bge-m3@<revision>``), because only the safety block is comparable across
scorers and the ranking block is only reproducible if the scorer is named.

The default corpus is v2, which adds the discriminating-power probes to v1. v1 is kept
in the tree and still runs (``--corpus evaluation/memory/scenarios.v1.json``) because the
published v1 report names it; changing v1 in place would have made that report
irreproducible instead of merely superseded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from core import settings
from servicemind.evaluation.memory import evaluate, load_corpus, render_markdown
from servicemind.memory.service import CachedMemoryEmbeddingProvider

CORPUS = Path("evaluation/memory/scenarios.v2.json")
REPORTS_DIR = Path("evaluation/reports")


def _embedding(mode: str):
    """Return ``(provider, scoring_model)``; ``(None, "lexical-jaccard")`` for lexical.

    The provider is built exactly as :class:`Phase5Governance` builds it for the live
    path, so a ranking block produced here describes the scorer production would use.
    """
    if mode == "lexical":
        return None, "lexical-jaccard"
    if mode != "tei":
        raise SystemExit(f"unknown embedding mode: {mode}")
    if not settings.SERVICEMIND_EMBEDDING_URL:
        raise SystemExit(
            "--embedding tei requires SERVICEMIND_EMBEDDING_URL; unset means the run "
            "would silently fall back to lexical and mislabel its own report"
        )
    # Imported lazily so the default offline path never touches the RAG extra.
    from servicemind.rag.models import TeiEmbeddingProvider

    provider = TeiEmbeddingProvider(
        settings.SERVICEMIND_EMBEDDING_URL,
        model_revision=settings.SERVICEMIND_EMBEDDING_REVISION,
    )
    return (
        CachedMemoryEmbeddingProvider(provider),
        f"{provider.model_name}@{provider.model_revision}",
    )


def _report_stem(mode: str) -> str:
    """Name the report after the mode that produced it.

    The lexical report is the published one and keeps its filename. An embedding run
    gets its own: otherwise a production-scorer run would overwrite the published
    numbers in place, and nothing in the filename would record that it had happened.
    """
    return "phase5_memory_latest" if mode == "lexical" else f"phase5_memory_{mode}_latest"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--embedding", choices=("lexical", "tei"), default="lexical")
    parser.add_argument("--out", type=Path, default=REPORTS_DIR)
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "exit non-zero on a leak, an unsafe activation, a rank gate violation, "
            "a delivery gate violation or a missing required record"
        ),
    )
    options = parser.parse_args()

    corpus = load_corpus(options.corpus)
    embedding, scoring_model = _embedding(options.embedding)
    report = asyncio.run(
        evaluate(
            corpus,
            anchor=datetime.now(UTC),
            embedding=embedding,
            scoring_model=scoring_model,
        )
    )

    options.out.mkdir(parents=True, exist_ok=True)
    stem = _report_stem(options.embedding)
    payload = report.model_dump(mode="json")
    (options.out / f"{stem}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (options.out / f"{stem}.md").write_text(render_markdown(report), encoding="utf-8")

    print(render_markdown(report))
    if options.check:
        # Five independent ways to fail. The rank gate is one because a saturated
        # ranking block reports a clean average while the answer slides down the
        # list; the delivery gate is one because a record can rank first and still
        # be dropped on the way into the prompt, which every other block calls green.
        missing = sum(len(outcome.missing_required) for outcome in report.probe_outcomes)
        breaches = (
            report.probe.leaked_records
            + len(report.write.unsafe_activations)
            + len(report.rank_gate.violations)
            + len(report.delivery_gate.violations)
            + missing
        )
        if breaches:
            print(
                f"FAIL: {report.probe.leaked_records} leak(s), "
                f"{len(report.write.unsafe_activations)} unsafe activation(s), "
                f"{len(report.rank_gate.violations)} rank gate violation(s), "
                f"{len(report.delivery_gate.violations)} delivery gate violation(s), "
                f"{missing} missing required record(s)",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
