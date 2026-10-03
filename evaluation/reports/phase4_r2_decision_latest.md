# R2 decision record: the late-interaction arm is retired at R2.0

Generated: 2026-10-02T15:05:26+08:00  
Decision: **RETIRE the late-interaction arm in its online corpus-scan form**  
Decided at: R2.0 (before any full index build)  
Status: **DECISION_RECORD**

This record decides R2 before any full index was built. It is written after the R2.0 size/latency spike returned FAIL, and after reading all 36 of the queries whose labelled document is not in the depth-100 pool -- the entire population R2 existed to recover.

## 1. R2.0 spike: FAIL, and not because of the implementation

| quantity | value |
| --- | ---: |
| corpus documents | 1,276,222 |
| sampled documents | 5,000 |
| tokens (extrapolated) | 138,219,182 |
| index bytes fp16 | 283.1 GB |
| encode throughput (this batch setting) | 3,683 tokens/s |
| projected build | 10.4 h |
| projected scan per query (numpy path) | 2,358 s |
| cost budget (204 ms = +20% of baseline p95 1020.7 ms) | 204 ms |
| **verdict** | **FAIL** |

Runtime: `BAAI/bge-m3` @ `5617a9f61b02` on cuda:1, torch 2.5.1+cu121, CUDA 12.1.

### The failure is the data size, not the code

| bound | value |
| --- | ---: |
| corpus tokens | 138,219,182 |
| fp16 bytes | 283.1 GB |
| PCIe, one pass @ 25 GB/s | 11.3 s |
| compute @ our measured 70.3 M tokens/s | 1.97 s |
| total with a perfect GPU kernel | 13.3 s |
| budget | 204 ms |
| **over budget** | **65x** |

the 70.3 M tokens/s figure is our own measured GPU MaxSim on this card, 1674x the numpy path that produced the projected scan above. Even with that kernel and zero indexing overhead the arm is ~65x over budget, so this is a property of the data size, not of the implementation.

### Why the arm cannot meet its own admission criterion at all

> Coverage@100 can only rise if the arm ranks gold documents that the existing arms do not already have in their top 100. The only way to find such a document is to score it against the whole 1,276,222-document corpus. Re-scoring the existing candidates with MaxSim is a rerank: it reorders a pool of fixed membership and therefore cannot raise coverage@100 at all, by construction. So the arm's own admission criterion requires exactly the corpus-wide scan the spike measured.

Criterion, verbatim: pool coverage@100 must rise by >= 2 percentage points, paired, with Recall@5 not regressing.

## 2. What the 36 misses actually are

R2 was justified by a miss rate. So the 36 queries were read one by one.

| bucket | n | share of 280 answerable |
| --- | ---: | ---: |
| label defective (the retriever's hit answers the question at least as well as the gold) | 17 | 6.1% |
| genuine retrieval miss (gold right, retriever missed it) | 4 | 1.4% |
| ambiguous (neither document answers) | 15 | 5.4% |

**Label defective -- the gold does not answer the question:**

| query | why the label fails |
| --- | --- |
| `DEV_Q149` | gold is a 'Late breaking updates to DataPower 7.5' landing page; the hit is an SSH-connection-failure technote |
| `TRAIN_Q016` | gold is generic ulimit guidance; the hit is literally the DASH 'too many open files' technote |
| `TRAIN_Q352` | gold is about an 'invalid XML character'; the hit is the WebSphere Adapter for Flat File technote collection |
| `TRAIN_Q044` | gold is the SPSS student-resources landing page; the hit is how to generate an SPSS authorization key |
| `TRAIN_Q366` | gold is how to *create* a backup; the question asks how to *restore* from one, which the hit addresses |
| `DEV_Q141` | gold is about NormalizeCCO merging lines; the hit is the technote for the exact reported symptom |
| `TRAIN_Q540` | gold is a 'Fix list for Content Navigator 3.0.0' changelog; the hit describes the IE textarea defect |
| `DEV_Q077` | gold is the SPSS resources landing page; the hit is the license-transfer technote |
| `TRAIN_Q024` | gold is the SPSS resources landing page; the hit is the license-transfer technote |
| `TRAIN_Q505` | gold is the SPSS resources landing page; the hit is the license-transfer technote |
| `DEV_Q283` | gold is the SPSS resources landing page; the hit is the 'what is a lock code' technote |
| `TRAIN_Q421` | gold is the SPSS resources landing page; the hit is the SPSS licensing overview |
| `TRAIN_Q497` | gold is 'enabling tracing for the DASH service'; the question is a login failure, which tracing does not answer |
| `TRAIN_Q295` | gold is 'how to determine if a TIP server is part of an HA cluster'; unrelated to the applet-permissions question |
| `DEV_Q067` | gold is 'Manage Group Add User search does not return users'; the question is an AppTarget startup hang |
| `DEV_Q027` | gold is 'batches left in running state'; the question asks how to *create* batches for bulk upload |
| `TRAIN_Q314` | gold is an SSLv3 security bulletin; the question asks how to configure a minimum TLS version |

**Genuine retrieval miss -- these are real, and there are four of them:**

| query | why the gold is right |
| --- | --- |
| `TRAIN_Q110` | gold 'Installing the Installation Manager on an NFS mounted disk' answers the question; the retriever missed it |
| `DEV_Q034` | gold 'Java Health Center Client' answers 'is a profiler provided'; the retriever missed it |
| `TRAIN_Q247` | gold is the APAR 'VMM DOES NOT CLEAR THE CACHE IF CLEARENTITY MODE IS USED', which is the exact mechanism asked about |
| `DEV_Q132` | gold 'How to enable Baselining in your monitored application' is the plausible source of the 'unknown' status |

**Ambiguous (15) -- neither document answers the question, so no arm could be scored on them:**

`DEV_Q110`, `DEV_Q123`, `DEV_Q240`, `TRAIN_Q121`, `TRAIN_Q124`, `TRAIN_Q135`, `TRAIN_Q259`, `TRAIN_Q272`, `TRAIN_Q283`, `TRAIN_Q286`, `TRAIN_Q318`, `TRAIN_Q428`, `TRAIN_Q494`, `TRAIN_Q584`, `TRAIN_Q596`

### The structural proof that the labels are crawl artefacts

`swg21592093.txt` -- *IBM SPSS Student Version and Graduate Pack Resources - United States* -- is the labelled gold for **8 different queries** in the answerable set, **6 of them in the out-of-pool group**.

a single generic 'SPSS Student Version and Graduate Pack Resources' landing page is the labelled gold for 8 different and mutually unrelated questions (authorization code, licence transfer to a new machine, lock code lookup, licensing overview). It cannot be the correct answer to all of them; the pattern is a crawl artifact, not relevance.

Separately, 36 of 36 out-of-pool golds are absent from BM25 as well. a relevant document whose terms the question repeats would be found by BM25; none of the 36 are.

### The headroom of the whole business case

| quantity | value |
| --- | ---: |
| miss rate R2 was justified by | 12.86% (36/280) |
| honest upper bound (genuine misses only) | 1.43% (4/280) |
| the arm's own admission bar | 2.00% |

R2 was justified by a 12.86% miss rate. After reading all 36, at most 4 are genuine retrieval misses, and even those assume late interaction would rank them into the top 100. The honest upper bound on the uplift is below the arm's own 2-point admission bar, so the arm cannot clear the bar even if its retriever were perfect on every real miss.

## 3. The depth control, which was already on disk

This experiment was not run for this record; it already existed in the funnel report.

| arm | candidate depth | Recall@10 | p95 |
| --- | ---: | ---: | ---: |
| `c0_sq` | 100 | 0.6893 | 927.9 ms |
| `c0_sq_ck200` | 200 | 0.6857 | 1,482.1 ms |
| `c0_sq_ck400` | 400 | 0.6821 | 2,772.1 ms |
| `c0_mq` | 100 | 0.6750 | 1,020.7 ms |
| `c0_mq_ck200` | 200 | 0.6893 | 1,668.4 ms |

Control (the same arm run twice, 280 queries): Recall@10 moved **0** queries, Recall@5 moved **1**.

quadrupling the candidate pool (100 -> 400) does not raise Recall@10 on the signal arm (0.6893 -> 0.6857 -> 0.6821) while tripling p95. The control arm moved 0 queries at Recall@10 on a repeat run, so the differences here are at or below what this harness can resolve. Widening the funnel is not where the measured loss lives.

## 4. Secondary finding: the recall columns are pack-limited

`cutoffs_the_pack_cannot_tell_apart` = ['10==20']; the pack averages 7.55 documents and never exceeds 16.

Recall@10 equals Recall@20 for every arm because the pack averages 7.5 documents and never exceeds 19, so a cutoff at or above the pack size counts the whole pack. Recall at those cutoffs is a property of the packing/token budget, not of ranking depth.

## 5. What replaces the arm

- the binding constraint on the measured number is the label set, not the retriever; the next artefact has to be a small, hand-adjudicated, tenant-signed gold set over the real corpus
- keep clean external sets (LoTTE) for component-to-component comparison, where the labels stand
- do not train a reranker (R3) on this silver set: it would be trained to reproduce the noise

## 6. Limitations

- the adjudication of the 36 is a reading by this agent, recorded per query so it can be overturned; it is not a human signoff and carries no tenant provenance
- the depth control is the proxy's arms, not OpenSearch's; the production run is what measures production
- n=36; the buckets describe this set, not a population
- the R2.0 spike is a 5000-document sample with a numpy scan path; the encode-throughput number is batch-dependent and would improve with tuning, but the FAIL is decided by the 283 GB corpus size and the 204 ms budget, neither of which the batch size moves
