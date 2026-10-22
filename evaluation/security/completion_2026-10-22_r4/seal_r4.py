"""Seal the r4 throughput annex; --check verifies it without rewriting."""

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
RUNS = {
    "r4-a-8192-cap1200": "FAIL",
    "r4-b-8192-cap1200-t180": "BLOCKED",
    "r4-c-32t-8192-cap1200-t180": "BLOCKED",
}
DOCS = [
    "docs/SECURITY_ASSESSMENT_EXECUTION_PLAN_2026-10-22.md",
    "docs/SECURITY_ASSESSMENT_RESULTS_2026-10-22.md",
    "docs/SECURITY_LOCAL_MODEL_PHASE1_2026-10-22.md",
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(relative):
    return json.loads((ROOT / relative).read_text())


if "--check" in sys.argv:
    manifest = json.loads(MANIFEST.read_text())
    for row in manifest["files"]:
        assert digest(ROOT / row["path"]) == row["sha256"], row["path"]
    print(json.dumps({"verdict": "PASS", "verified_files": len(manifest["files"])}))
    raise SystemExit(0)

observations = load("evaluation/security/completion_2026-10-22_r4/observations.json")
cleanup = load("evaluation/security/completion_2026-10-22_r4/cleanup.json")

# The annex must still say what it said: three runs, the verdicts they earned, and
# the six things that could not be done.
for label, verdict in RUNS.items():
    grade = load(f"evaluation/security/completion_2026-10-22_r4/{label}/grade.json")
    assert grade["case_id"] == "ACC-01" and grade["verdict"] == verdict, label
assert len(observations["not_completed"]) == 6, observations["not_completed"]
assert observations["budget_arithmetic"]["acceptance_budget_seconds"] == 300
assert observations["budget_arithmetic"]["best_case_full_run_seconds"] > 300
assert not cleanup["production_services_modified"]
assert cleanup["private_environment_removed"]

# r4-a is a harness-configuration error (timeout above the 180s ceiling): it ended
# cancelled, unlike b/c which were still running. It must never be a model result.
r4a = load("evaluation/security/completion_2026-10-22_r4/r4-a-8192-cap1200/ACC-01.json")
assert r4a["terminal_status"] == "cancelled"

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
paths += [ROOT / relative for relative in DOCS]
hits = []
for path in paths:
    raw = path.read_bytes()
    for key, value in private_values.items():
        if value in raw:
            hits.append({"path": str(path.relative_to(ROOT)), "secret_variable": key})
assert not hits, json.dumps(hits)
manifest = {
    "sealed_at": datetime.now(UTC).isoformat(),
    "source_revision": observations["source_revision"],
    "sealed_working_tree_revision": source_revision(ROOT),
    "private_value_checks": len(private_values),
    "private_value_hits": 0,
    "files": [
        {"path": str(p.relative_to(ROOT)), "sha256": digest(p), "bytes": p.stat().st_size}
        for p in paths
    ],
}
MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
print(json.dumps({"verdict": "PASS", "sealed_files": len(paths), "private_value_hits": 0}))
