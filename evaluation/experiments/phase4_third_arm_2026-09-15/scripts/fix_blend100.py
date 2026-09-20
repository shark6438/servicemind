"""Recompute the layer-1 `blend100` arm with the *retrieval* score, not the rerank score.

`run_layer1.py` phase_c fed the rerank matrix into the min-max normalisation, which
made the blend a monotone transform of the rerank score - every ranking came out
identical to pure rerank and the experiment drew the opposite conclusion. Production
(`src/servicemind/rag/service.py` final_score) normalises `hit.score`, i.e. the fused
RRF retrieval score. This script recomputes the arm from the unchanged fused ranking
plus the cached rerank matrix, so no GPU and no re-runs are needed.
"""

import importlib.util
import json
import statistics
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path("/home/shihongye/data1/servicemind")
OUT = Path("/tmp/exp_layer1")

spec = importlib.util.spec_from_file_location(
    "proxy_eval", ROOT / "scripts" / "evaluate_phase4_proxy_release.py"
)
P = importlib.util.module_from_spec(spec)
sys.modules["proxy_eval"] = P
spec.loader.exec_module(P)

payload = json.loads((OUT / "funnel_v1.json").read_text())
matrix = np.load(OUT / "rerank_tei_v1.npy")
relevant = [frozenset(x) for x in payload["relevant"]]
alpha = float(P._rerank_weight())

# The fused ranking in the artifact must be exactly what production's RRF produces.
for i, docs in enumerate(payload["rrf"]):
    assert P._rrf(payload["dense"][i], payload["bm25"][i], k=len(docs)) == list(docs), i

outcomes = []
for i, docs in enumerate(payload["rrf"]):
    row = matrix[i]
    rrf = P._rrf_scores(payload["dense"][i], payload["bm25"][i])
    ranked = list(range(len(docs)))
    by_doc = {doc: float(row[j]) for j, doc in enumerate(docs)}
    pure100 = tuple(sorted(docs, key=lambda doc: (-by_doc[doc], doc)))
    blend100 = P._blend_ranking(
        docs, by_doc, rrf, rerank_weight=alpha, depth=len(docs)
    )
    outcomes.append(
        SimpleNamespace(
            relevant=relevant[i],
            rrf100=tuple(docs),
            pure30=tuple(sorted(docs[:30], key=lambda doc: (-by_doc[doc], doc))),
            pure100=pure100,
            blend100=blend100,
        )
    )

# Reuse run_layer1's own scorer so the corrected arm carries the same bootstrap CIs.
_spec_l1 = importlib.util.spec_from_file_location("layer1", OUT / "run_layer1.py")
L1 = importlib.util.module_from_spec(_spec_l1)
sys.modules["layer1"] = L1
_spec_l1.loader.exec_module(L1)

answerable = [o for o in outcomes if o.relevant]
report = {arm: L1.score_arm(answerable, arm) for arm in ("rrf100", "pure30", "pure100", "blend100")}

identical = sum(1 for o in outcomes if o.pure100 == o.blend100)
print(json.dumps(report, indent=2))
print("identical orderings (blend100 vs pure100): %d/%d" % (identical, len(outcomes)))
print("rerank_weight (alpha) = %.2f" % alpha)
