"""Record pytest phases without interpreting terminal output as evidence."""

import json
import os
from pathlib import Path

from servicemind.evaluation.security_grader import EvidenceOutcome

_reports: list[dict] = []


def pytest_runtest_logreport(report):
    _reports.append(
        {
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "wasxfail": getattr(report, "wasxfail", None),
        }
    )


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("SERVICEMIND_PYTEST_EVIDENCE")
    if path:
        Path(path).write_text(
            json.dumps({"exitstatus": int(exitstatus), "reports": _reports}), encoding="utf-8"
        )


def grade_test_phases(rows: list[dict], *, process_ok: bool) -> EvidenceOutcome:
    if any(row["outcome"] == "failed" for row in rows):
        return EvidenceOutcome.FAILED
    if not process_ok or not rows:
        return EvidenceOutcome.MISSING
    if any(row["outcome"] != "passed" or row.get("wasxfail") for row in rows):
        return EvidenceOutcome.UNEXERCISED
    for nodeid in {row["nodeid"] for row in rows}:
        phases = [row["when"] for row in rows if row["nodeid"] == nodeid]
        if sorted(phases) != ["call", "setup", "teardown"]:
            return EvidenceOutcome.MISSING
    return EvidenceOutcome.PASSED
