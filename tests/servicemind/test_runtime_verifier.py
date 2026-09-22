from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "verify_servicemind_runtime", ROOT / "scripts/verify_servicemind_runtime.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_runtime_verifier_accepts_only_the_installed_project_entrypoint() -> None:
    canonical = (
        "{ path=/project/.venv/bin/servicemind-api ; argv[]=/project/.venv/bin/servicemind-api ; }"
    )
    source_tree = (
        "{ path=/project/.venv/bin/python ; argv[]=/project/.venv/bin/python src/run_service.py ; }"
    )
    assert MODULE._uses_installed_api_entrypoint(canonical, Path("/project"))
    assert not MODULE._uses_installed_api_entrypoint(source_tree, Path("/project"))
