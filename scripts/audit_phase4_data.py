"""Produce a reproducible, read-only content audit for downloaded Phase 4 sources."""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "phase4" / "raw"
REPORT = ROOT / "evaluation" / "reports" / "phase4_data_audit_latest.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_stats(path: Path) -> dict[str, int | bool]:
    files = [item for item in path.rglob("*") if item.is_file() and ".cache" not in item.parts]
    return {
        "present": path.exists(),
        "file_count": len(files),
        "bytes": sum(item.stat().st_size for item in files),
    }


def audit_mendeley() -> dict:
    path = DATA / "production" / "mendeley-helpdesk-v3"
    issues = pd.read_csv(path / "issues.csv", low_memory=False)
    utterances = pd.read_csv(path / "sample_utterances.csv", low_memory=False)
    history = pd.read_csv(path / "issues_change_history.csv", low_memory=False)
    issue_ids = set(issues["id"].dropna())
    utterance_ids = set(utterances["issueid"].dropna())
    return {
        **tree_stats(path),
        "doi": "10.17632/btm76zndnt.3",
        "issues": len(issues),
        "issue_columns": list(issues.columns),
        "history_events": len(history),
        "utterances": len(utterances),
        "utterance_cases": int(utterances["issueid"].nunique()),
        "matched_text_cases": len(issue_ids & utterance_ids),
        "resolutions": {
            str(key): int(value)
            for key, value in issues["issue_resolution"].value_counts(dropna=False).items()
        },
        "safe_ingestion_scope": "Only the 360 cases with matched utterances are text-case candidates.",
    }


def audit_uci() -> dict:
    path = DATA / "reference" / "uci-incident-management-498" / "incident_event_log.csv"
    frame = pd.read_csv(path, low_memory=False)
    return {
        "present": path.exists(),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "events": len(frame),
        "incidents": int(frame["number"].nunique()),
        "columns": list(frame.columns),
        "contains_ticket_text": False,
    }


def audit_techqa() -> dict:
    path = DATA / "eval" / "techqa-rag-eval"
    payload = json.loads((path / "train.json").read_text(encoding="utf-8"))
    archive = path / "corpus.zip"
    with zipfile.ZipFile(archive) as handle:
        corpus_files = [name for name in handle.namelist() if not name.endswith("/")]
    rows = payload if isinstance(payload, list) else payload.get("data", payload)
    return {
        **tree_stats(path),
        "revision": "0b5bbc84b7f07d6d09d063130e90b716d8d4a32a",
        "qa_records": len(rows),
        "corpus_files": len(corpus_files),
        "corpus_sha256": sha256(archive),
    }


def audit_classification() -> dict:
    path = DATA / "eval" / "classification-7648117"
    labels = pd.concat(
        [pd.read_csv(path / "y_train.csv"), pd.read_csv(path / "y_test.csv")],
        ignore_index=True,
    )
    return {
        **tree_stats(path),
        "doi": "10.5281/zenodo.7648117",
        "records": len(labels),
        "categories": {
            str(key): int(value) for key, value in labels["category_truth"].value_counts().items()
        },
    }


def audit_similarity() -> dict:
    path = DATA / "eval" / "semantic-similarity-7426225"
    records = pd.concat(
        [pd.read_csv(path / f"group_{group}.csv") for group in (1, 2, 3)],
        ignore_index=True,
    )
    label_columns = {
        name: list(pd.read_csv(path / name, nrows=1).columns)
        for name in ("group_1_labeled.csv", "group_2_labeled.csv", "group_3_labeled.csv")
    }
    return {
        **tree_stats(path),
        "doi": "10.5281/zenodo.7426225",
        "records": len(records),
        "languages": {
            str(key): int(value) for key, value in records["language"].value_counts().items()
        },
        "label_columns": label_columns,
        "standard_qrels": False,
    }


def audit_enterpriseops() -> dict:
    path = DATA / "eval" / "enterpriseops-gym-itsm"
    modes: dict[str, int] = {}
    tools_per_task: Counter[int] = Counter()
    for file in path.glob("*/itsm-*.parquet"):
        frame = pd.read_parquet(file)
        modes[file.parent.name] = len(frame)
        if "selected_tools" in frame.columns:
            tools_per_task.update(
                len(item) for item in frame["selected_tools"] if hasattr(item, "__len__")
            )
    return {
        **tree_stats(path),
        "revision": "c8e538eae8a6205294f0a86675fefdc1fac408f6",
        "mode_records": modes,
        "total_mode_rows": sum(modes.values()),
        "tool_count_distribution": dict(sorted(tools_per_task.items())),
    }


def main() -> None:
    report = {
        "audited_at": datetime.now(UTC).isoformat(),
        "production": {
            "pagerduty": tree_stats(DATA / "production" / "incident-response-docs-master" / "docs"),
            "mendeley_v3": audit_mendeley(),
        },
        "reference": {
            "uci_498": audit_uci(),
            "ibm_graph_rag": tree_stats(DATA / "reference" / "enterprise-itsm-graph-rag-main"),
        },
        "eval": {
            "techqa": audit_techqa(),
            "classification": audit_classification(),
            "semantic_similarity": audit_similarity(),
            "enterpriseops_itsm": audit_enterpriseops(),
            "agentdojo": tree_stats(DATA / "eval" / "agentdojo-main"),
            "itsm_safety_bench": {
                "upstream_confirmed": True,
                "downloaded": False,
                "reason": "Current GitHub/codeload edge returned 404; license not yet pinned.",
            },
        },
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
