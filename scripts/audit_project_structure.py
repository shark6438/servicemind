"""Audit ServiceMind's repository structure as an executable architecture contract."""

from __future__ import annotations

import argparse
import ast
import json
import tomllib
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
OUTPUT_JSON = ROOT / "evaluation/reports/project_structure_latest.json"
OUTPUT_MD = ROOT / "evaluation/reports/project_structure_latest.md"
INTERNAL_ROOTS = {
    "agents",
    "client",
    "core",
    "memory",
    "pages",
    "schema",
    "service",
    "servicemind",
    "voice",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def package_graph(root: Path = SRC / "servicemind") -> dict[str, set[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        source = relative.parts[0] if len(relative.parts) > 1 else "_root"
        graph.setdefault(source, set())
        for imported in _imports(path):
            if not imported.startswith("servicemind."):
                continue
            target = imported.split(".", 2)[1]
            if target != source:
                graph[source].add(target)
                graph.setdefault(target, set())
    return dict(graph)


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return deterministic strongly connected components containing a cycle."""
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(graph.get(node, set())):
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        has_self_loop = len(component) == 1 and component[0] in graph.get(component[0], set())
        if len(component) > 1 or has_self_loop:
            cycles.append(sorted(component))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return sorted(cycles)


def _domain_violations() -> list[str]:
    violations: list[str] = []
    for path in sorted((SRC / "servicemind/domain").rglob("*.py")):
        for imported in sorted(_imports(path)):
            if imported.startswith("servicemind.") and not imported.startswith(
                "servicemind.domain"
            ):
                violations.append(f"{path.relative_to(ROOT)} -> {imported}")
    return violations


def _source_boundary_violations() -> list[str]:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for imported in sorted(_imports(path)):
            root = imported.split(".", 1)[0]
            if root in {"tests", "scripts", "evaluation"}:
                violations.append(f"{path.relative_to(ROOT)} -> {imported}")
        if "sys.path.insert(" in text or "sys.path.append(" in text:
            violations.append(f"{path.relative_to(ROOT)} mutates sys.path")
    return violations


def _development_execution_violations() -> list[str]:
    """Reject developer entry points that bypass the installed distribution."""
    violations: list[str] = []
    for path in sorted((ROOT / "scripts").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if (
                node.func.attr in {"insert", "append"}
                and isinstance(owner, ast.Attribute)
                and owner.attr == "path"
                and isinstance(owner.value, ast.Name)
                and owner.value.id == "sys"
            ):
                violations.append(f"{path.relative_to(ROOT)} mutates sys.path")
    for path in sorted((ROOT / "scripts").glob("*.sh")):
        text = path.read_text(encoding="utf-8")
        if "PYTHONPATH=src" in text or "sys.path.insert" in text:
            violations.append(f"{path.relative_to(ROOT)} bypasses installed distribution")
    return violations


def _has_function(path: Path, name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(isinstance(node, ast.FunctionDef) and node.name == name for node in tree.body)


def build_report() -> dict[str, Any]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    graph = package_graph()
    cycles = find_cycles(graph)
    domain_violations = _domain_violations()
    source_violations = _source_boundary_violations()
    development_violations = _development_execution_violations()
    project_scripts = pyproject.get("project", {}).get("scripts", {})
    pytest_options = pyproject.get("tool", {}).get("pytest", {}).get("ini_options", {})
    package_find = (
        pyproject.get("tool", {}).get("setuptools", {}).get("packages", {}).get("find", {})
    )
    unit_paths = [
        ROOT / "deploy/systemd/servicemind-api.service",
        ROOT / "deploy/systemd/servicemind-streamlit.service",
        ROOT / "deploy/systemd/servicemind-outbox.service",
    ]
    units_present = all(path.is_file() for path in unit_paths)
    unit_text = "\n".join(path.read_text(encoding="utf-8") for path in unit_paths if path.exists())

    checks = {
        "src_layout": (SRC / "servicemind/__init__.py").is_file(),
        "build_backend_declared": pyproject.get("build-system", {}).get("build-backend")
        == "setuptools.build_meta",
        "explicit_package_discovery": package_find.get("where") == ["src"]
        and "servicemind*" in package_find.get("include", []),
        "streamlit_pages_packaged": "pages*" in package_find.get("include", [])
        and (SRC / "pages/__init__.py").is_file()
        and (SRC / "pages/1_Memory_Review.py").is_file(),
        "installed_process_entrypoints": project_scripts.get("servicemind-api")
        == "run_service:main"
        and project_scripts.get("servicemind-outbox") == "servicemind.reliability.worker:main",
        "tests_use_installed_distribution": "pythonpath" not in pytest_options
        and "--import-mode=importlib" in pytest_options.get("addopts", []),
        "fastapi_application_factory": _has_function(SRC / "service/service.py", "create_app"),
        "http_adapter_split": (SRC / "servicemind/interfaces/http/memory_review.py").is_file(),
        "domain_dependency_rule": not domain_violations,
        "acyclic_servicemind_packages": not cycles,
        "source_tree_boundary": not source_violations,
        "installed_execution_hygiene": not development_violations,
        "versioned_process_manifests": units_present
        and "PYTHONPATH=src" not in unit_text
        and "/servicemind-api" in unit_text
        and "/servicemind-outbox" in unit_text,
    }
    return {
        "schema_version": "servicemind-project-structure-v1",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "violations": {
            "package_cycles": cycles,
            "domain_dependencies": domain_violations,
            "source_boundaries": source_violations,
            "development_execution": development_violations,
        },
        "servicemind_package_graph": {key: sorted(values) for key, values in sorted(graph.items())},
        "repository_model": {
            "style": "packaged modular monolith with explicit process adapters",
            "composition_root": "src/service/service.py:create_app",
            "domain_root": "src/servicemind/domain",
            "inbound_adapters": ["src/servicemind/interfaces/http", "src/pages"],
            "outbound_adapters": [
                "src/servicemind/integrations",
                "src/servicemind/persistence",
                "src/servicemind/tool_platform",
            ],
            "independent_processes": ["api", "streamlit", "outbox"],
            "legacy_instance_agent_shell": [
                "src/agents",
                "src/client",
                "src/core",
                "src/memory",
                "src/schema",
                "src/service",
                "src/pages",
                "src/voice",
            ],
        },
        "evidence_scope": (
            "Structure, dependency direction, packaging and process manifests only; "
            "this does not certify tenant-domain RAG quality or instance-agent behavior."
        ),
    }


def render_markdown(report: dict[str, Any]) -> str:
    check_rows = "\n".join(
        f"| `{name}` | {'PASS' if passed else 'FAIL'} |"
        for name, passed in report["checks"].items()
    )
    graph_rows = "\n".join(
        f"| `{source}` | {', '.join(f'`{target}`' for target in targets) or '—'} |"
        for source, targets in report["servicemind_package_graph"].items()
    )
    return f"""# ServiceMind project structure audit

Status: **{report["status"]}**

This audit covers repository structure, import direction, packaging and the three deployed
process manifests. It does not rate unrelated instance Agents and does not convert missing
RAG business evidence into a quality pass.

## Executable gates

| gate | result |
| --- | --- |
{check_rows}

## Frozen structure

- Packaged `src` modular monolith; tests import the installed editable distribution.
- `service.service:create_app` is the composition root.
- Domain contracts do not import runtime, persistence, HTTP or provider adapters.
- HTTP entry adapters live under `servicemind.interfaces.http`; repositories and providers
  remain outbound adapters.
- API, Streamlit and outbox are separate versioned process manifests.
- The inherited top-level instance-agent shell remains load-bearing but sits outside the
  ServiceMind platform dependency graph.

## Package dependency graph

| package | depends on |
| --- | --- |
{graph_rows}

Strongly connected package components: **0**.

## Reference basis

- [PyPA: src layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)
- [PyPA: pyproject and entry points](https://packaging.python.org/en/latest/specifications/pyproject-toml/)
- [FastAPI: larger applications and routers](https://fastapi.tiangolo.com/tutorial/bigger-applications/)
- [LangGraph: discrete nodes, raw state and durable execution](https://docs.langchain.com/oss/javascript/langgraph/thinking-in-langgraph)
- [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)
- [Twelve-Factor App](https://12factor.net/)
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    report = build_report()
    json_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    markdown = render_markdown(report)
    if args.check:
        artifacts_match = (
            OUTPUT_JSON.exists()
            and OUTPUT_MD.exists()
            and OUTPUT_JSON.read_text(encoding="utf-8") == json_text
            and OUTPUT_MD.read_text(encoding="utf-8") == markdown
        )
        passed = report["status"] == "PASS" and artifacts_match
        print("PASS" if passed else "FAIL", report["status"])
        return 0 if passed else 1
    OUTPUT_JSON.write_text(json_text, encoding="utf-8")
    OUTPUT_MD.write_text(markdown, encoding="utf-8")
    print(report["status"])
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
