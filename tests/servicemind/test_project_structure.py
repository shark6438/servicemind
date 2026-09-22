from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "audit_project_structure", ROOT / "scripts/audit_project_structure.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_enterprise_structure_contract_is_closed() -> None:
    report = MODULE.build_report()
    assert report["status"] == "PASS", report["violations"]
    assert all(report["checks"].values())
    assert report["violations"] == {
        "package_cycles": [],
        "domain_dependencies": [],
        "source_boundaries": [],
        "development_execution": [],
    }


def test_cycle_detector_rejects_mutual_package_dependencies() -> None:
    assert MODULE.find_cycles({"agents": {"orchestration"}, "orchestration": {"agents"}}) == [
        ["agents", "orchestration"]
    ]
