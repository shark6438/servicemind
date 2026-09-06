import ast
from pathlib import Path


def test_business_write_call_exists_only_in_frozen_harness() -> None:
    root = Path("src/servicemind")
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.as_posix().endswith("integrations/glpi/client.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Attribute) and function.attr == "append_ticket_followup":
                if path.as_posix() != "src/servicemind/harness/executor.py":
                    offenders.append(f"{path}:{node.lineno}")
    assert offenders == []


def test_only_phase2_harness_imports_glpi_write_method() -> None:
    action_source = Path("src/servicemind/agents/action.py").read_text(encoding="utf-8")
    reviewer_source = Path("src/servicemind/agents/reviewer.py").read_text(encoding="utf-8")
    analysis_source = Path("src/servicemind/agents/analysis.py").read_text(encoding="utf-8")
    assert "GlpiClient" not in action_source
    assert "GlpiClient" not in reviewer_source
    assert "GlpiClient" not in analysis_source
    assert "CredentialCipher" not in action_source
