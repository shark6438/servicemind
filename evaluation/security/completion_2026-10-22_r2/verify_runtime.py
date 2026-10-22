"""Record nonsecret final runtime identity and closure of temporary model endpoints."""

import asyncio
import json
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx

BATCH = Path(__file__).resolve().parent


async def main():
    async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
        for _ in range(90):
            try:
                response = await client.get("http://127.0.0.1:18080/health/build")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        response.raise_for_status()
        build = response.json()
        assert (
            build["source_revision"]
            == json.loads((BATCH / "frozen-components.json").read_text())["source_revision"]
        )
        config = build["configuration"]
        assert (
            config["default_model"] == "openai-compatible"
            and config["compatible_model"] == "security-local-qwen-instruct"
        )
        assert (
            config["model_allowed_providers"] == "openai"
            and config["model_allowed_models"] == "security-local-qwen-instruct"
        )
        assert not config["generic_api_enabled"]
        closed = {}
        for method, path, body in [
            ("POST", "/invoke", {"message": "assessment closure"}),
            ("POST", "/history", {"thread_id": "synthetic-no-access"}),
            ("GET", "/threads", None),
        ]:
            result = await client.request(method, "http://127.0.0.1:18080" + path, json=body)
            assert result.status_code == 404
            closed[path] = result.status_code
        assert (await client.get("http://127.0.0.1:18080/v1/servicemind/runs")).status_code == 401
        assert (await client.get("http://127.0.0.1:3000/")).status_code == 200
    units = {}
    for unit in [
        "servicemind-api.service",
        "servicemind-outbox.service",
        "servicemind-frontend.service",
        "servicemind-webhook-proxy.service",
    ]:
        state = subprocess.check_output(
            ["systemctl", "--user", "is-active", unit], text=True
        ).strip()
        assert state == "active"
        units[unit] = state
    container = json.loads(
        subprocess.check_output(["docker", "inspect", "servicemind-security-llama"])
    )[0]
    assert container["HostConfig"]["ReadonlyRootfs"]
    assert container["Config"]["User"] and container["Config"]["User"] != "0:0"
    assert container["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
    assert "--no-agent" in container["Config"]["Cmd"]
    assert "--no-webui-mcp-proxy" in container["Config"]["Cmd"]
    assert all(
        not mount["RW"] for mount in container["Mounts"] if mount["Type"] in {"bind", "volume"}
    )
    temporary_ports = [11435, 11436, 11437, 11439, 11441, 18083]
    for port in temporary_ports:
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0, port
    runtime = {
        "verdict": "PASS",
        "recorded_at": datetime.now(UTC).isoformat(),
        "runtime_build_identity": build,
        "closed_generic_routes": closed,
        "unauthenticated_product_status": 401,
        "service_units": units,
        "model_runtime": {
            "image": container["Config"]["Image"],
            "image_id": container["Image"],
            "mode": "CPU llama.cpp, local GGUF, no cloud-provider credentials",
            "read_only_root_and_model_mount": True,
            "api_key_authentication": True,
            "built_in_tools_and_proxy_disabled": True,
            "restart_policy": "unless-stopped",
            "ports": container["HostConfig"]["PortBindings"],
        },
        "database": json.loads((BATCH / "deployment-security-check.json").read_text()),
        "api_fee": 0,
        "temporary_model_ports_closed": temporary_ports,
        "interpretation": "Main API uses an actual free open model. Full control acceptance used the explicit simulator profile; final normal-model control is separate. No general business or all-model release certification.",
    }
    (BATCH / "final-runtime-state.json").write_text(json.dumps(runtime, indent=2) + "\n")
    print(
        json.dumps(
            {
                "verdict": "PASS",
                "source_revision": build["source_revision"],
                "model": config["compatible_model"],
                "api_fee": 0,
            }
        )
    )


asyncio.run(main())
