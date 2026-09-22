#!/usr/bin/env python3
"""Verify the deployed ServiceMind operator console and its browser security boundary."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]


def _command(*parts: str) -> str:
    return subprocess.run(parts, check=True, capture_output=True, text=True).stdout.strip()


def build_report() -> dict[str, Any]:
    unit_status = _command("systemctl", "--user", "is-active", "servicemind-frontend.service")
    unit = _command(
        "systemctl",
        "--user",
        "show",
        "servicemind-frontend.service",
        "--property=FragmentPath,ExecMainPID,ActiveState",
    )
    fragment = _command(
        "systemctl",
        "--user",
        "show",
        "servicemind-frontend.service",
        "--property=FragmentPath",
        "--value",
    )
    container = json.loads(_command("docker", "inspect", "servicemind-frontend"))[0]
    image_scan = json.loads(
        (ROOT / "evaluation/reports/frontend_image_vulnerability_latest.json").read_text(
            encoding="utf-8"
        )
    )
    image_vulnerabilities = [
        vulnerability
        for result in image_scan.get("Results", [])
        for vulnerability in result.get("Vulnerabilities") or []
    ]
    with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
        frontend = client.get("http://127.0.0.1:3000/")
        release = client.get("http://127.0.0.1:3000/release-status.json")
        allowed = client.options(
            "http://127.0.0.1:18080/v1/servicemind/runs",
            headers={
                "Origin": "http://127.0.0.1:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        denied = client.options(
            "http://127.0.0.1:18080/v1/servicemind/runs",
            headers={
                "Origin": "https://attacker.invalid",
                "Access-Control-Request-Method": "GET",
            },
        )
        openapi = client.get("http://127.0.0.1:18080/openapi.json")
    paths = openapi.json().get("paths", {}) if openapi.status_code == 200 else {}
    headers = {key.lower(): value for key, value in frontend.headers.items()}
    csp = headers.get("content-security-policy", "")
    nonce_match = re.search(r"script-src[^;]*'nonce-([^']+)'", csp)
    script_tags = re.findall(r"<script\b[^>]*>", frontend.text, flags=re.IGNORECASE)
    nonce = nonce_match.group(1) if nonce_match else ""
    binding = container["NetworkSettings"]["Ports"].get("3000/tcp", [])
    checks = {
        "user_unit_active": unit_status == "active" and "ActiveState=active" in unit,
        "versioned_unit_loaded": Path(fragment).read_bytes()
        == (ROOT / "deploy/systemd/servicemind-frontend.service").read_bytes(),
        "frontend_http_200": frontend.status_code == 200,
        "security_headers": all(
            name in headers
            for name in (
                "content-security-policy",
                "x-content-type-options",
                "x-frame-options",
                "referrer-policy",
                "permissions-policy",
            )
        ),
        "strict_nonce_csp": bool(nonce)
        and "'strict-dynamic'" in csp
        and "script-src 'self' 'unsafe-inline'" not in csp
        and bool(script_tags)
        and all(f'nonce="{nonce}"' in tag for tag in script_tags),
        "container_nonroot": str(container["Config"].get("User", "")) not in {"", "0", "root"},
        "container_read_only": container["HostConfig"].get("ReadonlyRootfs") is True,
        "deployed_image_scan_clean": image_scan.get("Metadata", {}).get("ImageID")
        == container["Image"]
        and not image_vulnerabilities,
        "loopback_only_binding": binding == [{"HostIp": "127.0.0.1", "HostPort": "3000"}],
        "release_snapshot_available": release.status_code == 200
        and release.json().get("release_decision") == "QUALITY_EXCEPTION_ACCEPTED",
        "allowed_origin_cors": allowed.status_code == 200
        and allowed.headers.get("access-control-allow-origin") == "http://127.0.0.1:3000",
        "untrusted_origin_denied": denied.headers.get("access-control-allow-origin") is None,
        "operational_api_surface": all(
            path in paths
            for path in (
                "/v1/servicemind/runs",
                "/v1/servicemind/runs/{run_id}/timeline",
                "/v1/servicemind/audit-events",
            )
        ),
    }
    return {
        "schema_version": "servicemind-frontend-runtime-v3",
        "verified_at": datetime.now(UTC).isoformat(),
        "status": "PASS" if all(checks.values()) else "FAIL",
        "base_url": "http://127.0.0.1:3000",
        "unit": "servicemind-frontend.service",
        "image": container["Config"]["Image"],
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("evaluation/reports/frontend_runtime_latest.json"),
    )
    args = parser.parse_args()
    report = build_report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": report["status"], "checks": report["checks"]}))
    return 1 if args.check and report["status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
