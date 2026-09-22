import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "export_frontend_release_status",
    Path(__file__).parents[1] / "scripts" / "export_frontend_release_status.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
build_snapshot = MODULE.build_snapshot


def test_frontend_release_status_preserves_quality_exception_and_caveats() -> None:
    snapshot = build_snapshot()

    assert snapshot["release_decision"] == "QUALITY_EXCEPTION_ACCEPTED"
    assert snapshot["rag"]["status"] == "DOMAIN_QUALITY_NOT_CERTIFIED"
    assert snapshot["rag"]["recall_at_10"] < 0.9
    assert snapshot["caveats"]
    serialized = str(snapshot).lower()
    assert "secret" not in serialized
    assert "token" not in serialized
