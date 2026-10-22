"""Seal reproducible, sanitized evidence; --check verifies the seal without rewriting it."""

import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

from servicemind.evaluation.acceptance import CaseExecution, load_acceptance_cases
from servicemind.evaluation.acceptance_grader import grade as grade_acceptance
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
assert (
    source_revision(ROOT)
    == json.loads((BATCH / "frozen-components.json").read_text())["source_revision"]
)
assert actual["security_verdict"] == "PASS" and actual["api_fee"] == 0
current = json.loads((BATCH / "final-local-model-control-summary.json").read_text())
assert (
    current["utility_verdict"] in {"PASS", "FAIL", "BLOCKED"}
    and current["remote_identity_unchanged"]
    and current["api_fee"] == 0
)
execution = CaseExecution.model_validate_json(
    (BATCH / "final-local-model-control/ACC-01.json").read_text()
)
case_set = load_acceptance_cases(ROOT / "evaluation/acceptance/cases.v1.json")
independent = next(
    row
    for row in grade_acceptance(case_set, [execution], generated_at=datetime.now(UTC)).cases
    if row.case_id == "ACC-01"
)
assert independent.verdict.value == current["utility_verdict"]
configuration = json.loads((BATCH / "final-runtime-state.json").read_text())[
    "runtime_build_identity"
]["configuration"]
assert configuration["default_model"] == "openai-compatible"
assert configuration["model_allowed_providers"] == "openai"
assert configuration["model_allowed_models"] == "security-local-qwen-instruct"
assert not configuration["generic_api_enabled"]
private_values = {}
for path in [
    ROOT / ".env",
    ROOT / "deploy/glpi/.env",
    Path("/home/shihongye/.config/servicemind/local-model.env"),
]:
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
    "source_revision": source_revision(ROOT),
    "private_value_checks": len(private_values),
    "private_value_hits": 0,
    "files": [
        {"path": str(p.relative_to(ROOT)), "sha256": digest(p), "bytes": p.stat().st_size}
        for p in paths
    ],
}
MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
print(json.dumps({"verdict": "PASS", "sealed_files": len(paths), "private_value_hits": 0}))
