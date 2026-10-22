"""Combine same-revision test and isolated mutation evidence without changing verdicts."""

import argparse
import json
import runpy
import sys
from datetime import UTC, datetime
from pathlib import Path

from servicemind.evaluation.pytest_evidence import grade_test_phases
from servicemind.evaluation.security import (
    EvidenceKind,
    load_security_scenarios,
    scenario_set_digest,
)
from servicemind.evaluation.security_grader import (
    EvidenceOutcome,
    ObservedEvidence,
    ScenarioObservation,
)
from servicemind.evaluation.source_revision import source_revision

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--tests", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
root = args.root.resolve()
sys.path.insert(0, str(root / "scripts"))
import mutation_harness  # noqa: E402
import verify_phase7_security as driver  # noqa: E402

revision = source_revision(root)
local = load_security_scenarios(root / "evaluation/security/scenarios.local.v1.json")
original = load_security_scenarios(root / "evaluation/security/scenarios.v1.json")
observations = [
    ScenarioObservation.model_validate_json(p.read_text())
    for p in sorted(args.tests.glob("*.json"))
]
assert len(observations) == len(local.scenarios)
assert {o.scenario_id for o in observations} == {s.id for s in local.scenarios}
assert {o.deployed_revision for o in observations} == {revision}
assert {o.scenarios_digest for o in observations} == {scenario_set_digest(local)}
collected = {}
for observation in observations:
    for item in observation.evidence:
        if item.kind != EvidenceKind.MUTATION:
            collected[f"{item.kind.value}\0{item.ref}\0{item.mutation or ''}"] = item

expected_scripts = {
    item.ref for s in local.scenarios for item in s.evidence if item.kind == EvidenceKind.MUTATION
}
seen = {script: set() for script in expected_scripts}
mutation_count = 0
for result_path in sorted((args.output / "mutation-evidence").glob("*.result.json")):
    result = json.loads(result_path.read_text())
    script = result["script"]
    assert script in expected_scripts
    assert result["exit_code"] == 0, result
    assert result["before_revision"] == result["after_revision"] == revision
    evidence_path = result_path.with_name(
        result_path.name.replace(".result.json", ".evidence.json")
    )
    rows = json.loads(evidence_path.read_text())
    assert rows[0]["name"] == "baseline" and rows[-1]["name"] == "restored baseline"
    for baseline in (rows[0], rows[-1]):
        assert baseline["exit_code"] == 0
        assert grade_test_phases(baseline["reports"], process_ok=True) == EvidenceOutcome.PASSED
    for row in rows[1:-1]:
        assert row["name"] not in seen[script], row["name"]
        status = mutation_harness.mutation_status(row["exit_code"], row["reports"])
        assert row["status"] == status == "RED (good)", row["name"]
        seen[script].add(row["name"])
        item = ObservedEvidence(
            kind=EvidenceKind.MUTATION,
            ref=script,
            mutation=row["name"],
            outcome=EvidenceOutcome.PASSED,
            detail=json.dumps(
                {"status": status, "raw_evidence": evidence_path.name, "source_revision": revision}
            ),
        )
        collected[f"mutation\0{script}\0{row['name']}"] = item
        mutation_count += 1
for script in expected_scripts:
    namespace = runpy.run_path(str(root / script))
    expected = {m.name if hasattr(m, "name") else m[0] for m in namespace["MUTATIONS"]}
    assert seen[script] == expected, {"script": script, "missing": sorted(expected - seen[script])}

verdicts, note = driver.acceptance_verdicts()
assert not verdicts and note, "Local-only batch must not silently import live acceptance"
for scenario in original.scenarios:
    for reference in scenario.evidence:
        if reference.kind == EvidenceKind.ACCEPTANCE:
            item = ObservedEvidence(
                kind=EvidenceKind.ACCEPTANCE,
                ref=reference.ref,
                outcome=EvidenceOutcome.MISSING,
                detail=note + "; live acceptance excluded by user scope",
            )
            collected[f"acceptance\0{reference.ref}\0"] = item
for scenario_set, destination in [(local, "replays"), (original, "full-scope-replays")]:
    target = args.output / destination
    assert not target.exists(), f"refusing to overwrite {target}"
    target.mkdir(parents=True)
    built = driver.build_observations(
        scenario_set,
        collected,
        revision=revision,
        observed_at=min(o.observed_at for o in observations),
        only=set(),
    )
    for observation in built:
        (target / f"{observation.scenario_id}.json").write_text(
            observation.model_dump_json(indent=2) + "\n"
        )
print(
    json.dumps(
        {
            "source_revision": revision,
            "mutation_count": mutation_count,
            "assembled_at": datetime.now(UTC).isoformat(),
            "local_scenarios": len(local.scenarios),
            "full_scenarios": len(original.scenarios),
        },
        indent=2,
    )
)
