"""Seal reproducible, sanitized evidence; --check verifies the seal without rewriting it."""

import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

from servicemind.evaluation.source_revision import source_revision

BATCH = Path(__file__).resolve().parent
ROOT = BATCH.parents[2]
MANIFEST = BATCH / "SHA256SUMS.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


if "--check" in sys.argv:
    manifest = json.loads(MANIFEST.read_text())
    for row in manifest["files"]:
        assert digest(ROOT / row["path"]) == row["sha256"], row["path"]
    print(json.dumps({"verdict": "PASS", "verified_files": len(manifest["files"])}))
    raise SystemExit(0)

for name in [
    "full-scope-report.json",
    "local-report.json",
    "case-matrix.json",
    "retention-observation.json",
    "mutation-summary.json",
]:
    assert json.loads((BATCH / name).read_text())["verdict"] == "PASS", name
actual = json.loads((BATCH / "local-model-attacks.json").read_text())
assert source_revision(ROOT) == actual["source_revision"]
assert actual["security_verdict"] == "PASS" and actual["api_fee"] == 0
configuration = json.loads((BATCH / "final-runtime-state.json").read_text())[
    "runtime_build_identity"
]["configuration"]
assert configuration["default_model"] == "ollama"
assert configuration["model_allowed_providers"] == "ollama"
assert configuration["model_allowed_models"] == "qwen3:30b-a3b"
assert not configuration["generic_api_enabled"]
private_values = {}
for path in [ROOT / ".env", ROOT / "deploy/glpi/.env"]:
    for key, value in dotenv_values(path).items():
        if (
            value
            and len(value) >= 16
            and re.search(
                r"(?:PASSWORD|SECRET|API_KEY|ACCESS_TOKEN|APP_TOKEN|USER_TOKEN|CREDENTIAL_KEY)$",
                key,
            )
        ):
            private_values[key] = value.encode()
paths = sorted(
    p
    for p in BATCH.rglob("*")
    if p.is_file() and "__pycache__" not in p.parts and p != MANIFEST and p.suffix != ".pyc"
)
paths += [
    ROOT / "docs/SECURITY_ASSESSMENT_EXECUTION_PLAN_2026-10-22.md",
    ROOT / "docs/SECURITY_ASSESSMENT_RESULTS_2026-10-22.md",
]
hits = []
for path in paths:
    raw = path.read_bytes()
    for key, value in private_values.items():
        if value in raw:
            hits.append({"path": str(path.relative_to(ROOT)), "secret_variable": key})
assert not hits, json.dumps(hits)
manifest = {
    "sealed_at": datetime.now(UTC).isoformat(),
    "source_revision": actual["source_revision"],
    "private_value_checks": len(private_values),
    "private_value_hits": 0,
    "files": [
        {"path": str(p.relative_to(ROOT)), "sha256": digest(p), "bytes": p.stat().st_size}
        for p in paths
    ],
}
MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
print(json.dumps({"verdict": "PASS", "sealed_files": len(paths), "private_value_hits": 0}))
