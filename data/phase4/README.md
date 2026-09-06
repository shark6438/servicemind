# ServiceMind Phase 4 data workspace

This directory separates source material by authority and runtime purpose.

- `raw/production/`: sources eligible for the Knowledge Agent production corpus.
- `raw/reference/`: development-only schemas, generators, and offline process data.
- `raw/eval/`: datasets and harnesses that must never be queried by production RAG.
- `manifests/`: tracked provenance, license, version, commit, checksum, and usage records.
- `indexes/`: local derived OpenSearch/Neo4j artifacts; never committed.
- `models/`: local embedding/reranker weights; never committed.

Downloaded files are intentionally ignored by Git. Reproducibility is provided by the
tracked manifest and download/verification scripts. A source may enter a production index
only when its manifest says `production_allowed: true` and its license is verified.
