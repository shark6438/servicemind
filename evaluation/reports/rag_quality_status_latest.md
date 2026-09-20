# RAG quality status

Overall: **DOMAIN_QUALITY_NOT_CERTIFIED**  
Tenant release gate: **NOT_EVALUATED** (0/6 applicable gates evaluated)  
External silver: **BELOW_TARGET_DIAGNOSTIC**

| metric | external silver | tenant reference | diagnostic |
| --- | ---: | ---: | --- |
| recall_at_5 | 0.6857 | 0.85 | below |
| recall_at_10 | 0.7643 | 0.90 | below |
| mrr_at_10 | 0.5848 | 0.75 | below |
| ndcg_at_10 | 0.6279 | 0.80 | below |

The 0.275 answerable answer rate is a **held-out retrieval-score threshold proxy**, not an
end-to-end Reviewer result. Its ROC-AUC is 0.6035 and the best balanced
accuracy over all score cuts is 0.5979; Reviewer semantic
calibration remains **NOT_EVALUATED**.

The committed 8-document / 15-query gold set is
**SMOKE_ONLY_SATURATED**. All four retrieval arms saturate Recall@5/10/20 and MRR, while
Precision@5 is 0.2667 and mean dedupe rate is
0.5000. It is valid only as a deterministic regression smoke test.

## Release blockers

- tenant-domain human qrels are unavailable
- Reviewer answerability and abstention correctness are not calibrated on domain labels
- production evidence/context-cap impact has no non-synthetic observations
