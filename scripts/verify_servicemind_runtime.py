"""Verify the deployed ServiceMind API process, unit, configuration, and health URL."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

from core import settings


def _systemd_properties(unit: str, scope: str) -> dict[str, str]:
    command = ["systemctl"]
    if scope == "user":
        command.append("--user")
    command.extend(
        [
            "show",
            unit,
            "--property=ActiveState,SubState,MainPID,WorkingDirectory,ExecStart,FragmentPath",
            "--no-pager",
        ]
    )
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        return {"error": (result.stderr or result.stdout).strip()}
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def _health(base_url: str) -> tuple[int | None, object]:
    try:
        # A deployment probe must reach the bound socket directly. Inheriting an
        # operator's HTTP(S)_PROXY can send even a loopback URL to a remote proxy and
        # produce a convincing but unrelated 502.
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"{base_url.rstrip('/')}/health", timeout=5) as response:  # noqa: S310
            body = response.read(4096).decode("utf-8", errors="replace")
            try:
                return response.status, json.loads(body)
            except json.JSONDecodeError:
                return response.status, body
    except HTTPError as exc:
        return exc.code, exc.read(4096).decode("utf-8", errors="replace")
    except URLError as exc:
        return None, f"{type(exc.reason).__name__}: {exc.reason}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", default="servicemind-api.service")
    parser.add_argument("--scope", choices=("user", "system"), default="user")
    parser.add_argument("--base-url")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()

    host = settings.HOST
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    base_url = args.base_url or f"http://{probe_host}:{settings.PORT}"
    properties = _systemd_properties(args.unit, args.scope)
    http_status, health = _health(base_url)
    repository = Path(__file__).resolve().parents[1]
    working_directory = properties.get("WorkingDirectory")
    exec_start = properties.get("ExecStart", "")
    executable_match = re.search(r"\bpath=([^ ;]+)", exec_start)
    executable = Path(executable_match.group(1)) if executable_match else None
    executable_matches = (
        bool(executable)
        and executable.name.startswith("python")
        and (executable.parent.parent.resolve() == (repository / ".venv").resolve())
    )
    checks = {
        "unit_active": properties.get("ActiveState") == "active",
        "unit_running": properties.get("SubState") == "running",
        "main_pid_present": int(properties.get("MainPID", "0") or 0) > 0,
        "working_directory_matches_repository": bool(working_directory)
        and Path(working_directory).resolve() == repository,
        "exec_start_matches_repository": executable_matches and "src/run_service.py" in exec_start,
        "health_http_200": http_status == 200,
        "health_body_ok": isinstance(health, dict) and health.get("status") == "ok",
    }
    report = {
        "schema_version": "servicemind-runtime-verification-v1",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "unit": args.unit,
        "scope": args.scope,
        "base_url": base_url,
        "configured_host": settings.HOST,
        "configured_port": settings.PORT,
        "unit_properties": properties,
        "health_http_status": http_status,
        "health_body": health,
        "checks": checks,
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if args.check and report["status"] != "PASS":
        sys.exit(1)


if __name__ == "__main__":
    main()
