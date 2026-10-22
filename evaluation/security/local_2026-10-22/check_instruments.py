"""Calibrate evidence failure paths; no model or service calls."""

import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import gate_phase7_security as gate  # noqa: E402
import verify_phase7_security as driver  # noqa: E402

from servicemind.evaluation.security import (  # noqa: E402
    SecurityScenario,
    SecurityScenarioSet,
    load_security_scenarios,
    scenario_set_digest,
)
from servicemind.evaluation.security_grader import (  # noqa: E402
    EvidenceOutcome,
    ScenarioObservation,
)

checks = {}
scenario = SecurityScenario(
    id="CALIBRATION-BLOCKED",
    category="verifier-availability",
    title="A blocked acceptance observation stays unexercised",
    defends_against="Evidence missing is misrepresented as passed",
    teeth="Direct check of the acceptance importer branch",
    evidence=[{"kind": "acceptance", "ref": "ACC-01"}],
)
case_set = SecurityScenarioSet.model_construct(
    schema_version="phase7-security-v1",
    description="Single-branch calibration, not an attack suite",
    scenarios=(scenario,),
    not_covered=("Live acceptance is deliberately not executed",),
)
with (
    patch.object(driver, "acceptance_verdicts", return_value=({"ACC-01": "BLOCKED"}, "")),
    patch.object(driver, "run_tests", return_value={}),
):
    evidence = driver.collect_evidence(case_set, only=set(), with_mutations=False)
assert len(evidence) == 1
assert next(iter(evidence.values())).outcome == EvidenceOutcome.UNEXERCISED
checks["blocked_acceptance_is_unexercised_without_enum_crash"] = "PASS"

local_set = load_security_scenarios(ROOT / "evaluation/security/scenarios.local.v1.json")
observation = ScenarioObservation(
    scenario_id=local_set.scenarios[0].id,
    observed_at=datetime.now(UTC),
    deployed_revision=driver.source_revision(),
    scenarios_digest=scenario_set_digest(local_set),
    evidence=(),
)
with tempfile.TemporaryDirectory() as directory:
    for name in ("first.json", "duplicate.json"):
        (Path(directory) / name).write_text(observation.model_dump_json())
    try:
        gate.load_replays(local_set, Path(directory))
    except gate.ConfigurationError as error:
        assert "Duplicate" in str(error)
    else:
        raise AssertionError("duplicate replay was accepted")
checks["duplicate_scenario_replay_rejected"] = "PASS"

verdicts, note = driver.acceptance_verdicts()
assert not verdicts and "different source revision" in note
checks["historical_acceptance_rejected"] = "PASS"
print(json.dumps({"scope": "instrument calibration only", "checks": checks}, indent=2))
