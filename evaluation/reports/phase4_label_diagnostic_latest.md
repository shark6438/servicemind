# Where the TechQA recall deficit comes from

Status: **MEASUREMENT_DIAGNOSTIC** (280 answerable queries, 251 distinct gold documents, labels `external_silver_no_tenant_human_signoff`)

The §4.1 thresholds are Recall@10 >= 0.90 and Recall@5 >= 0.85. The best arm on this set
reaches 0.7643 and 0.6857. This report asks whether that gap is the retriever's.

## 1. The funnel ceiling

Everything downstream of candidate generation -- RRF, the cross-encoder, the blend --
can only reorder what the funnel returned. `production_blend` is depth 100, which is `SERVICEMIND_RAG_CANDIDATE_K`, so this is the production funnel's headline and not an artefact of the proxy:

**244/280 = 0.8714** of answerable queries have their gold document anywhere in that 100.

| arm | depth | contains gold | rate | gold in top 5 | median rank when present |
| --- | ---: | ---: | ---: | ---: | ---: |
| bm25 | 100 | 221 | 0.7893 | 0.5286 | 2 |
| dense | 100 | 239 | 0.8536 | 0.7143 | 1 |
| hybrid | 30 | 222 | 0.7929 | 0.6464 | 1.0 |
| hybrid_reranked | 30 | 222 | 0.7929 | 0.6786 | 1.0 |
| production_blend | 100 | 244 | 0.8714 | 0.6857 | 1.0 |

So Recall@10 >= 0.90 is **above the ceiling of this funnel**: no reranking, no weight
change and no fusion rule can reach it while `candidate_k` is 100 on this label set.

## 2. What is in the 36 that are not

| group | queries | question vs gold title | question vs first hit title | first hit closer |
| --- | ---: | ---: | ---: | ---: |
| gold in the pool | 244 | 0.2372 | 0.2404 | 49/244 (0.2008) |
| gold out of the pool | 36 | 0.0636 | 0.1325 | 31/36 (0.8611) |

In the group the funnel *does* cover, the label and the first hit are topically
equivalent -- that is what a working retriever looks like. In the group it does not, the
first hit is 0.0688 *closer* to the question
than the document the labels call correct, and it is closer in
31 of 36 cases.

28 of the gold documents in
that group are not retrieved by *any* query in the set, and
36/36 are absent from the
BM25 arm as well, so this is not one arm's blind spot.

## 3. The ten widest gaps, chosen by rule

Selected as the ten misses where the first hit's title scores highest above the label's,
so the list is not curated.

**DEV_Q149** — Why SSH connections fail after upgrade to v7.5.2 and above? Why SSH connections fail after upgrade to v7.5.2 and above? The same configuration works on v7.2.

- labelled : `swg21974106.txt` — IBM Late breaking updates to DataPower 7.5 documentation - United States
- first hit: `nas8N1021014.txt` — IBM After Applying PTFs for 5733SC1, SSH/SFTP/SCP Connections to/from IBM i May Fail with Cipher Errors - United States
- the first hit's title is closer to the question by 0.2105; gold in the 100-deep pool: False

**TRAIN_Q016** — What can be done about "Too many open files" messages in the DASH systemOut? What can be done about "Too many open files" messages in the DASH systemOut log?

- labelled : `swg21469413.txt` — IBM Guidelines for setting ulimits (WebSphere Application Server) - United States
- first hit: `swg21968787.txt` — IBM DASH Services Stops Automatically After Few hours.  "Too many open files" - United States
- the first hit's title is closer to the question by 0.2000; gold in the 100-deep pool: False

**TRAIN_Q283** — What is the latest Alcatel 5620 SAM probe? We have the nco_p_alcatel_5620_sam_v13 probe, is there a newer version of the probe. Does this probe support Release 14 of the 5620 SAM?

- labelled : `swg22005197.txt` — IBM Tivoli Netcool/OMINbus Integrations Release Notice - Probe for Nokia Network Functions Manager for Packet (nco-p-nokia-nfmp-1_0) - United States
- first hit: `swg21902388.txt` — IBM Netcool Probe for Alcatel-Lucent 5620 SAM support for SAM Release 12.0 - United States
- the first hit's title is closer to the question by 0.1925; gold in the 100-deep pool: False

**TRAIN_Q352** — How to Create an Application with the the Websphere Adapter for Flat File using Hex05 delimeter? How to Create an Application with the the Websphere Adapter for Flat File using Hex05 delimeter?

- labelled : `swg21425772.txt` — IBM "An invalid XML character" error is thrown when handling data originating from a WebSphere Adapter - United States
- first hit: `swg27024018.txt` — IBM Collection of WebSphere Adapter for Flat File Technotes - United States
- the first hit's title is closer to the question by 0.1857; gold in the 100-deep pool: False

**TRAIN_Q044** — Authorization code missing for SPSS 25? I purchased the IBM SPSS from Amazon, and I do not know where to locate the authorization code of license code/key. Can anyone help me?

- labelled : `swg21592093.txt` — IBM SPSS Student Version and Graduate Pack Resources - United States
- first hit: `swg21968944.txt` — IBM How To Generate an Authorization or License Key for your SPSS product - United States
- the first hit's title is closer to the question by 0.1825; gold in the 100-deep pool: False

**TRAIN_Q366** — How do to restore corrupted Object server DB using data from Backup Object server in Linux? Error: E-REG-002-025: Region 'table_store', from directory '/opt/IBM/tivoli/netcool/omnibus/db/NCOMS/', has been recovered in a corrupt state: Extent statistics mismatch Error: E-OBX-102-020: Failed to start the storage system. (-490:Extent statistics mismatch)

- labelled : `swg21631606.txt` — IBM Create backup of the ObjectServer database - United States
- first hit: `swg21421816.txt` — IBM Object Server table_store recovered in a corrupt state - United States
- the first hit's title is closer to the question by 0.1759; gold in the 100-deep pool: False

**TRAIN_Q584** — Help with Security Bulletin: Vulnerability identified in IBM WebSphere Application Server shipped with WSRR (CVE-2017-1741) I need to understand details regarding Security Bulletin: Vulnerability identified in IBM WebSphere Application Server shipped with IBM WebSphere Service Registry and Repository (CVE-2017-1741). We are running WAS traditional V9.0.0.0. What is the recommended fix?

- labelled : `swg22012345.txt` — IBM Security Bulletin: Potential Privilege Escalation in WebSphere Application Server Admin Console (CVE-2017-1731) - United States
- first hit: `swg22016822.txt` — IBM Security Bulletin: A security vulnerability has been identified in IBM Websphere Application Server shipped with IBM Security Directory Server (CVE-2017-1741) - United States
- the first hit's title is closer to the question by 0.1313; gold in the 100-deep pool: False

**DEV_Q141** — Why is OCR is putting multiple lines on one line? I am running an APT application, and multiple detail lines are getting put on a single line within my .TXT file. Is there something I can do about this?

- labelled : `swg21701910.txt` — IBM NormalizeCCO merging lines with IBM Datacap Taskmaster Capture - United States
- first hit: `swg22008456.txt` — IBM Why is the Description entered over multiple lines saved/shown as a single line? - United States
- the first hit's title is closer to the question by 0.1267; gold in the 100-deep pool: False

**TRAIN_Q596** — Why is ITCAM MQ agent shutting down often? We have an MQ agent instance that keeps shutting down randomly. Below is the version of agent: mq WebSphere MQ Monitoring Agent lx8263 Version: 07.30.01.00

- labelled : `swg24043057.txt` — IBM ITCAM Agents for WebSphere Messaging Version 7.3.0 Fix Pack 02 (7.3.0-TIV-XEforMsg-FP0002) - United States
- first hit: `swg24044438.txt` — IBM ITCAM Agent for WebSphere MQ Version 7.3.0 Fix Pack 02 Provisional Fix 01 - United States
- the first hit's title is closer to the question by 0.1250; gold in the 100-deep pool: False

**DEV_Q077** — I need to transfer my SPSS 24 licence to a new computer I need to transfer my SPSS 24 license to a new machine. I have only used my activation code on one computer so far, and that computer was found to be defective which has led me to get a new computer. I was able to download SPSS 24 Gradpack onto the new computer, but every time I put in the activation code, the program gives me an error message saying that I am not allowed to generate any new licenses.

- labelled : `swg21592093.txt` — IBM SPSS Student Version and Graduate Pack Resources - United States
- first hit: `swg21985888.txt` — IBM How do I transfer my IBM SPSS product/software license from one machine to another? - United States
- the first hit's title is closer to the question by 0.1012; gold in the 100-deep pool: False

## What this does and does not say

the funnel ceiling is the highest Recall@k any reranker can reach on this label set, because RRF, the cross-encoder and the blend only reorder what the 100 candidates contain. Compare it against the §4.1 thresholds: a threshold above it cannot be met by ranking better. Then read the two title-overlap blocks together -- in the in-pool group the label and the first hit are topically equivalent, which is what a working retriever looks like; in the out-of-pool group the first hit is systematically closer to the question than the label. Where that holds, widening the funnel buys less than the raw miss rate suggests

The two candidate fixes have different ceilings. **Widening the funnel** raises the ceiling
above 0.8714 — but section 2 says the group it would recover is
mostly queries where the first hit is already closer to the question than the label, so it
buys less than the 0.1286 miss rate suggests.
**Improving the reranker** is bounded by the same ceiling and cannot pass it. Neither is a
change to make on this evidence, and neither is approved here.

## Limitations

- title overlap is a lexical Jaccard over content words, in the same family as the proxy's offline fallback. It is a heuristic for topical proximity, not a human judgment, and it is wrong on any document whose title is vaguer than its body
- n=36 in the out-of-pool group; the rate is a description of this set, not an estimate of a population
- the diagnostics dump is the *proxy's* arms. It says what that funnel returns, not what OpenSearch returns; the production run is the thing that measures production
- the label tier is external silver with no tenant signoff, so neither the labels nor this critique of them carry the §3.3 provenance
