# Tenant-domain release set through the production retrieval path

Status: **PIPELINE_COMPARISON_ONLY** — 14 documents, 16 queries, ingest 1.5s.

This corpus has what the public silver sets do not: 2 tenants, 3 group-restricted documents, 1 withdrawn, 1 past their effective end and 1 not yet in force. The §4.1 clauses each have something here to be about.

## Retrieval over the strata

| arm | recall@5 | recall@10 | answered-unanswerable | abstention | wrong tenant | unauthorized group | expired version | total |
|---|---|---|---|---|---|---|---|---|
| dense | 1.0000 | 1.0000 | 5 | 0.000 | 0 | 0 | 0 | 0 |
| bm25 | 1.0000 | 1.0000 | 5 | 0.000 | 0 | 0 | 0 | 0 |
| hybrid | 1.0000 | 1.0000 | 5 | 0.000 | 0 | 0 | 0 | 0 |
| hybrid_rerank | 1.0000 | 1.0000 | 5 | 0.000 | 0 | 0 | 0 | 0 |

The violation columns are the §4.1 count, computed against the ACLs the loader assigned. They are zero here for a different reason than they are zero on a single-tenant corpus: there are documents they could be nonzero about.

## ACL probes

Each protected document is asked for twice — once by a caller who must not see it, once by a caller who must. The control is what makes the first column mean something.

| document | axis | isolation | bases that leaked it | control | violations |
|---|---|---|---|---|---|
| KB-ACME-VPN-MFA-G3 | group | held | none | held | 0 |
| KB-ACME-VPN-MFA-G4 | group | held | none | held | 0 |
| KB-GLOBEX-CITRIX-BREAKGLASS | tenant | held | none | held | 0 |
| KB-ACME-VPN-MFA-LEGACY | retirement flag | held | none | n/a (no caller may see it) | 0 |
| KB-ACME-VPN-MFA-V1 | effective window (superseded) | held | none | n/a (no caller may see it) | 0 |
| KB-ACME-VPN-MFA-Q3 | effective window (not yet in force) | held | none | n/a (no caller may see it) | 0 |

- **KB-ACME-VPN-MFA-G3** (group): group 3's runbook; an acme caller holding no groups is inside the tenant and still outside the group
- **KB-ACME-VPN-MFA-G4** (group): the counterpart probe: group 3 holds a group, just not this one, so a filter that let any group through would pass the first probe and fail this one
- **KB-GLOBEX-CITRIX-BREAKGLASS** (tenant): another tenant's document. Both callers hold the same groups, so nothing but the tenant filter can be what excludes the denied one
- **KB-ACME-VPN-MFA-LEGACY** (retirement flag): withdrawn, and unrestricted by group, so the group filter passes for every caller and only the retirement flag can exclude it. There is no allowed caller: the document must not be served to anyone
- **KB-ACME-VPN-MFA-V1** (effective window (superseded)): active in the directory but outside its own effective window at the query time -- a different exclusion from the retirement flag, and a filter that only reads is_active would serve it
- **KB-ACME-VPN-MFA-Q3** (effective window (not yet in force)): approved and not yet in force; the other end of the clock check, which a filter comparing only against effective_from would serve

## Why §4.1 still does not apply

- labels are tier silver with 0 annotator(s) and kappa None; §3.3 requires two domain annotators over a stratified 20% sample with kappa >= 0.80

## Limitations

- the labels are synthetic and the provenance says so; no human signed them
- the corpus is fixture-sized, so no stratum here is large enough to estimate a per-stratum rate from -- the strata are present, not powered
- the recall figures are saturated: each query was authored against a document whose title shares its vocabulary, over a corpus of fourteen. Recall@5 of 1.000 is a property of the fixture, not a quality measurement, and nothing in this report should be read as one
- `abstention_rate` counts unanswerable queries that returned any context. It is not the Reviewer's semantic abstention and must not be reported as an answerable answer rate
- parents are held in memory rather than read under PostgreSQL row-level security, so this exercises the expansion call and not the RLS policy

## Per-arm detail

# Phase 4 retrieval evaluation — tenant-domain-release-v1

Top-k cutoffs: 5, 10.

| baseline | Recall@5 | Recall@10 | Recall@20 | MRR@10 | NDCG@10 | Precision@5 | answered-unanswerable | abstention | latency(ms) |
|---|---|---|---|---|---|---|---|---|---|
| dense | 1.000 | 1.000 | 1.000 | 0.955 | 0.968 | 0.236 | 5 | 0.000 | 54 |
| bm25 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.236 | 5 | 0.000 | 32 |
| hybrid | 1.000 | 1.000 | 1.000 | 1.000 | 0.989 | 0.236 | 5 | 0.000 | 50 |
| hybrid_rerank | 1.000 | 1.000 | 1.000 | 1.000 | 0.989 | 0.236 | 5 | 0.000 | 156 |

