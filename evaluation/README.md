# Evaluation evidence

`evaluation/` contains versioned inputs and outputs for the project's quality claims. It is not
a runtime scratch directory.

| Directory | Role |
| --- | --- |
| `acceptance/`, `quality/`, `security/`, `reliability/`, `load/`, `memory/`, `routing/` | Cases, fixtures and replayable evidence for a named evaluation area. |
| `gold/` | Gold labels and the available corpus inputs. RAG evaluation remains explicitly incomplete until these are finished. |
| `reports/` | Machine-readable and human-readable gate results referenced by the delivery documents. |
| `experiments/` | Frozen, dated experimental snapshots retained to make past decisions reproducible; they are not production runtime data. |
| `artifacts/`, `li-index/` | Small fixtures and local-index inputs used by evaluation harnesses. |

Retention rule: keep a committed artifact when a report, gate, fixture or reproducibility path
references it. Remove only untracked, regenerable local outputs (caches, test output, build
directories and service logs); those are excluded by `.gitignore` and do not belong in commits.

For the current delivery conclusion, read
[the final deliverable report](../docs/SERVICEMIND_FINAL_DELIVERABLE_REPORT_2026-10-03.md) and
[the 18-area coverage audit](../docs/EVALUATION_18_COVERAGE_AUDIT_2026-10-02.md).
