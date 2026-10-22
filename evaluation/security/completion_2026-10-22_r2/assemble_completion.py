"""Grade raw release phases, mutation experiments and fresh live acceptance together."""

import hashlib
import json
import runpy
import sys
from datetime import UTC, datetime
from pathlib import Path

from servicemind.evaluation.pytest_evidence import grade_test_phases
from servicemind.evaluation.security import EvidenceKind, load_security_scenarios
from servicemind.evaluation.security_grader import EvidenceOutcome, ObservedEvidence
from servicemind.evaluation.source_revision import source_revision

ROOT = Path(__file__).resolve().parents[3]
BATCH = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
import mutation_harness  # noqa: E402
import verify_phase7_security as driver  # noqa: E402


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    revision = source_revision(ROOT)
    frozen = json.loads((BATCH / "frozen-components.json").read_text())
    assert revision == frozen["source_revision"]
    assert digest(BATCH / "simulated_model_server.py") == frozen["simulator_sha256"]
    phase_path = BATCH / "release-test-phases.json"
    phases = json.loads(phase_path.read_text())
    assert phases["exitstatus"] == 0
    assert grade_test_phases(phases["reports"], process_ok=True) == EvidenceOutcome.PASSED
    sets = [
        (
            load_security_scenarios(ROOT / "evaluation/security/scenarios.local.v1.json"),
            "local-replays",
        ),
        (
            load_security_scenarios(ROOT / "evaluation/security/scenarios.v1.json"),
            "full-scope-replays",
        ),
    ]
    collected = {}

    def collect(item):
        collected[f"{item.kind.value}\0{item.ref}\0{item.mutation or ''}"] = item

    selectors = {
        e.ref
        for cases, _ in sets
        for s in cases.scenarios
        for e in s.evidence
        if e.kind == EvidenceKind.TEST
    }
    for selector in sorted(selectors):
        rows = [
            r
            for r in phases["reports"]
            if r["nodeid"] == selector or r["nodeid"].startswith(selector + "[")
        ]
        outcome = grade_test_phases(rows, process_ok=True)
        assert outcome == EvidenceOutcome.PASSED, (selector, outcome)
        collect(
            ObservedEvidence(
                kind=EvidenceKind.TEST,
                ref=selector,
                outcome=outcome,
                detail=json.dumps(
                    {"raw_phases": phase_path.name, "sha256": digest(phase_path), "phases": rows}
                ),
            )
        )
    scripts = {
        e.ref
        for cases, _ in sets
        for s in cases.scenarios
        for e in s.evidence
        if e.kind == EvidenceKind.MUTATION
    }
    seen = {script: set() for script in scripts}
    for result_path in sorted((BATCH / "mutation-evidence").glob("*.result.json")):
        result = json.loads(result_path.read_text())
        script = result["script"]
        assert script in scripts and result["exit_code"] == 0
        assert result["before_revision"] == result["after_revision"] == revision
        raw_path = result_path.with_name(result_path.name.replace(".result.json", ".evidence.json"))
        rows = json.loads(raw_path.read_text())
        assert rows[0]["name"] == "baseline" and rows[-1]["name"] == "restored baseline"
        for baseline in (rows[0], rows[-1]):
            assert baseline["exit_code"] == 0
            assert grade_test_phases(baseline["reports"], process_ok=True) == EvidenceOutcome.PASSED
        for row in rows[1:-1]:
            assert row["name"] not in seen[script]
            assert (
                mutation_harness.mutation_status(row["exit_code"], row["reports"])
                == row["status"]
                == "RED (good)"
            )
            seen[script].add(row["name"])
            collect(
                ObservedEvidence(
                    kind=EvidenceKind.MUTATION,
                    ref=script,
                    mutation=row["name"],
                    outcome=EvidenceOutcome.PASSED,
                    detail=json.dumps(
                        {
                            "raw_evidence": str(raw_path.relative_to(BATCH)),
                            "sha256": digest(raw_path),
                            "source_revision": revision,
                        }
                    ),
                )
            )
    for script in scripts:
        namespace = runpy.run_path(str(ROOT / script))
        expected = {m.name if hasattr(m, "name") else m[0] for m in namespace["MUTATIONS"]}
        assert seen[script] == expected, (script, sorted(expected - seen[script]))
    driver.ACCEPTANCE_REPORT = BATCH / "acceptance-report.json"
    driver.ACCEPTANCE_REPLAYS = BATCH / "acceptance-final-replays"
    verdicts, note = driver.acceptance_verdicts()
    assert not note and len(verdicts) == 28, note
    required_live = {
        e.ref
        for cases, _ in sets
        for s in cases.scenarios
        for e in s.evidence
        if e.kind == EvidenceKind.ACCEPTANCE
    }
    for case in sorted(required_live):
        replay = driver.ACCEPTANCE_REPLAYS / f"{case}.json"
        assert verdicts[case] == "PASS", (case, verdicts[case])
        collect(
            ObservedEvidence(
                kind=EvidenceKind.ACCEPTANCE,
                ref=case,
                outcome=EvidenceOutcome.PASSED,
                detail=json.dumps(
                    {
                        "source_revision": revision,
                        "case": case,
                        "verdict": verdicts[case],
                        "report_sha256": digest(driver.ACCEPTANCE_REPORT),
                        "replay_sha256": digest(replay),
                    }
                ),
            )
        )
    observed = datetime.fromtimestamp(phase_path.stat().st_mtime, UTC)
    for cases, destination in sets:
        target = BATCH / destination
        assert not target.exists(), f"Refusing to overwrite {target}"
        target.mkdir()
        for observation in driver.build_observations(
            cases, collected, revision=revision, observed_at=observed, only=set()
        ):
            (target / f"{observation.scenario_id}.json").write_text(
                observation.model_dump_json(indent=2) + "\n"
            )
    summary = {
        "verdict": "PASS",
        "source_revision": revision,
        "assembled_at": datetime.now(UTC).isoformat(),
        "detected": sum(map(len, seen.values())),
        "expected": sum(map(len, seen.values())),
        "experiments": {s: len(n) for s, n in sorted(seen.items())},
        "test_selectors": len(selectors),
        "live_security_cases": sorted(required_live),
        "method": "Complete setup/call/teardown; passing original/restored baselines; call-phase mutant failures; independently regraded current 28-case acceptance.",
    }
    (BATCH / "mutation-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
