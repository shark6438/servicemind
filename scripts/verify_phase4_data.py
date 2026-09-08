"""Verify pinned Phase 4 data artifacts without executing any downloaded code."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fetch_phase4_eval import restore_agentdojo, restore_enterpriseops, restore_techqa

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "phase4" / "manifests" / "sources.v1.2.json"


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
    external = {
        "techqa": restore_techqa(verify_only=True),
        "enterpriseops": restore_enterpriseops(verify_only=True),
        "agentdojo": restore_agentdojo(verify_only=True),
    }

    print(
        json.dumps(
            {
                "status": "passed",
                "manifest_version": manifest["manifest_version"],
                "verified_mendeley_files": len(mendeley["files"]),
                "mendeley_production_allowed": mendeley["production_allowed"],
                "external_evaluation": external,
                "optional_missing_sources": sorted(
                    item["id"]
                    for group in ("reference_sources", "evaluation_sources")
                    for item in manifest[group]
                    if item.get("download_status", "").startswith("files_missing")
                ),
                "safety_bench": by_id["servicenow-itsm-safety-bench"]["download_status"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
