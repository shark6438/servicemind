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
        "scaffold_import_budget": [],
    }


def test_scaffold_import_budget_matches_the_debt_it_freezes() -> None:
    """The numbers are current debt, not headroom.

    A budget above the measured count would gate nothing, and a root that lost its last
    import site has to leave the table rather than linger as an allowance.
    """
    assert MODULE.scaffold_import_sites() == MODULE.SCAFFOLD_IMPORT_BUDGET


def test_scaffold_budget_rejects_one_more_import_site() -> None:
    """A single added `from core import ...` is the regression this gate exists to catch."""
    budget = MODULE.SCAFFOLD_IMPORT_BUDGET
    assert MODULE._scaffold_budget_violations(budget) == []
    assert MODULE._scaffold_budget_violations({**budget, "core": budget["core"] + 1}) == [
        f"src/servicemind -> core: {budget['core'] + 1} import sites, budget {budget['core']}"
    ]
    # Shrinking the debt is always allowed, including ahead of the code change.
    assert MODULE._scaffold_budget_violations({**budget, "core": budget["core"] - 1}) == []


def test_cycle_detector_rejects_mutual_package_dependencies() -> None:
    assert MODULE.find_cycles({"agents": {"orchestration"}, "orchestration": {"agents"}}) == [
        ["agents", "orchestration"]
    ]
