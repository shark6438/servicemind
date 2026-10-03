# RAG quality status

Overall: **DOMAIN_QUALITY_NOT_CERTIFIED**
Tenant release gate: **NOT_EVALUATED** (0/6 applicable gates evaluated; 7 unmet preconditions, derived from the selection rather than transcribed -- they head the Release blockers list)
External silver: **BELOW_TARGET_DIAGNOSTIC**
Tenant-domain strata: **STRATA_PRESENT_NOT_POWERED** (2 tenants, 3 group-restricted, 1 withdrawn, 1 past their window, 1 not yet in force)

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

## What the external-silver figures are counting

Everything downstream of candidate generation only reorders what the funnel returned, so
the share of answerable queries whose gold document is anywhere in the 100-deep pool bounds every Recall@k on this set. That share is **244/280 = 0.8714** (`production_blend`, depth 100).

Recall thresholds that sit **above** that ceiling and therefore cannot be reached by
ranking better on this label set: **recall_at_10**.

| group | queries | question vs gold title | question vs first hit title | first hit closer |
| --- | ---: | ---: | ---: | ---: |
| gold in the pool | 244 | 0.2372 | 0.2404 | 0.2008 |
| gold out of the pool | 36 | 0.0636 | 0.1325 | 0.8611 |

In the group the funnel does cover, the label and the first hit are topically equivalent.
In the group it does not, the first hit is systematically closer to the question than the
document the labels call correct, and 28 of those
gold documents are retrieved by no query in the set at all. Detail and the rule-selected
examples: `evaluation/reports/phase4_label_diagnostic_latest.json`.

### Which ceiling the production prompt actually hits

The external-silver columns above are the *proxy's*. On the production path the arms pack
at most **19** documents against a budget of
8000 tokens (1107 tokens at the
median parent; one more would leave -13033 at the
median and -20519 at p90). The corpus names
1 distinct `provenance.source`, and
`SERVICEMIND_RAG_MAX_PARENTS_PER_SOURCE` is 4 — a
diversity guard with no second source to balance. **Attributed to:
the token budget** (`evaluation/reports/phase4_packing_ceiling_latest.json`).

every arm packs at most 19 documents, so every recall cutoff at or above 19 counts the whole pack: comparing such a figure against a threshold written for a depth-100 pool compares a cut list against a ranked one. Whether the budget ended the list is the arithmetic below -- one more parent at the median size would leave -13033 tokens of the 8000-token budget, the budget answering no -- and the verdict below is the other half of the answer: the token budget

This identified a defect, not a lever: the guard's own comment says it exists so that no
single source drowns the others, and with one source there is nothing to drown. Its
*value* is unchanged (`configuration_change_approved` is
**false**); what
changed is where it applies, so how much evidence one answer carries is now the token
budget's decision rather than an unrelated counter's.

### The recall cutoffs the production path can be measured at

`recall_at_k` counts gold inside the first `k` items the pipeline returned, so an arm that
packed at most `n` documents reports the same number at every cutoff above `n`. Read
against the report the production path itself wrote (`evaluation/reports/phase4_techqa_production_latest.json`, `final_k`
= 24, 8000-token budget,
1 distinct source):

| arm | mean packed | min | max | cutoffs the pack cannot tell apart | §4.1 metrics the pack is too short for |
| --- | ---: | ---: | ---: | --- | --- |
| dense | 7.439 | 4 | 16 | 20 | none |
| bm25 | 8.104 | 4 | 19 | 20 | none |
| hybrid | 7.857 | 5 | 17 | 20 | none |
| hybrid_rerank | 7.496 | 4 | 16 | 20 | none |

No arm's three recall cutoffs are one measurement, so each §4.1 recall bar is a window the pack reached rather than the whole pack under a wider name.

Recall thresholds no arm's own numbers can distinguish from the cutoff below them: **none**.

Every §4.1 metric was scored over the window its name claims: no arm in this run was cut short of the depth a threshold is stated at.

The §4.1 clauses are bars on the ranking; the delivery path decides how much of that ranking a tenant's prompt carries.

the proxy has no token budget and no diversity ceilings, so its top-k is a ranking cut; production's is a prompt-fitting cut. Read every recall figure beside the packed count: where it is below the cutoff, that recall number is reporting the budget, not the retriever.

## §4.1 visible evidence, over a corpus that has strata

The tenant-domain set carries the strata the gate's clauses are about, so its count of
wrong-tenant / unauthorized-group / expired-version evidence could have been nonzero:

| arm | wrong tenant | unauthorized group | expired version | total |
| --- | ---: | ---: | ---: | ---: |
| dense | 0 | 0 | 0 | 0 |
| bm25 | 0 | 0 | 0 | 0 |
| hybrid | 0 | 0 | 0 | 0 |
| hybrid_rerank | 0 | 0 | 0 | 0 |

6/6 ACL probes held — each asks a protected
document's question once as a caller who must not see it and once as a caller who must,
and 3/3 controls held, so the
first number is not a pipeline that returned nothing.

the §4.1 clauses each have a document here to be about, and the ACL count is computed against the loader's own coordinates. The set is synthetic, so its blockers still name human signoff -- which is what makes it evidence that the only unmet clause is the one no fixture can supply. It is not a quality measurement: the corpus is fixture-sized and its recall is saturated

The committed 8-document / 15-query gold set is
**SMOKE_ONLY_SATURATED**. All four retrieval arms saturate Recall@5/10/20 and MRR, while
Precision@5 is 0.2667 and mean dedupe rate is
0.5000. It is valid only as a deterministic regression smoke test.

## Release blockers

- labels are tier silver with 0 annotator(s) and kappa None; §3.3 requires two domain annotators over a stratified 20% sample with kappa >= 0.80
- no query carries 0--4 judgments, so the set can only be scored by the binary metrics and cannot express the §4.1 wrong-ACL/expired-version count
- the set has no hard-negative stratum (§3.2 asks for 80)
- the set has no must-refuse query of kind insufficient_evidence; a single abstention rate cannot tell the three apart
- the set has no must-refuse query of kind version_conflict; a single abstention rate cannot tell the three apart
- the set has no must-refuse query of kind must_refuse_access; a single abstention rate cannot tell the three apart
- the set has no development split, so nothing could have been tuned without touching the queries that are supposed to stay hidden
- Reviewer answerability and abstention correctness are not calibrated on domain labels
- production evidence/context-cap impact has no non-synthetic observations
