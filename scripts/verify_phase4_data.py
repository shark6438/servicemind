"""Verify pinned Phase 4 data artifacts without executing any downloaded code."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "phase4" / "manifests" / "sources.v1.json"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def require_hash(path: Path, expected: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    actual = digest(path)
    if actual != expected:
        raise ValueError(f"Checksum mismatch for {path}: {actual}")


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    by_id = {
        item["id"]: item
        for group in ("production_sources", "reference_sources", "evaluation_sources")
        for item in manifest[group]
    }

    mendeley = by_id["mendeley-help-desk-tickets-v3"]
    base = ROOT / mendeley["path"]
    for filename, expected in mendeley["files"].items():
        require_hash(base / filename, expected)

    require_hash(
        ROOT / "data/phase4/raw/production/pagerduty-incident-response-docs-master.zip",
        by_id["pagerduty-incident-response-docs"]["archive_sha256"],
    )
    require_hash(
        ROOT / "data/phase4/raw/reference/ibm-enterprise-itsm-graph-rag-main.zip",
        by_id["ibm-enterprise-itsm-graph-rag"]["archive_sha256"],
    )
    require_hash(
        ROOT / "data/phase4/raw/reference/uci-incident-management-498/incident_event_log.csv",
        by_id["uci-incident-management-498"]["csv_sha256"],
    )
    require_hash(
        ROOT / "data/phase4/raw/eval/agentdojo-main.zip",
        by_id["agentdojo"]["archive_sha256"],
    )

    required_paths = [
        "data/phase4/raw/eval/techqa-rag-eval/train.json",
        "data/phase4/raw/eval/techqa-rag-eval/corpus.zip",
        "data/phase4/raw/eval/enterpriseops-gym-itsm/oracle/itsm-00000-of-00001.parquet",
        "data/phase4/raw/eval/classification-7648117/X_train.csv",
        "data/phase4/raw/eval/semantic-similarity-7426225/group_1.csv",
    ]
    missing = [path for path in required_paths if not (ROOT / path).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing Phase 4 artifacts: {missing}")

    print(
        json.dumps(
            {
                "status": "passed",
                "verified_mendeley_files": len(mendeley["files"]),
                "verified_pinned_archives": 3,
                "safety_bench": by_id["servicenow-itsm-safety-bench"]["download_status"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
