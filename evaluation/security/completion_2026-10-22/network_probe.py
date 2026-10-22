"""Bounded socket probes against the isolated assessment API only."""

import asyncio
import json
import time
from pathlib import Path

import httpx

PORT = 18081


async def slow():
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    headers = f"POST /v1/servicemind/webhooks/glpi HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\nX-GLPI-signature: {'0' * 64}\r\nX-GLPI-timestamp: {int(time.time())}\r\nConnection: close\r\n\r\n{{"
    started = time.monotonic()
    writer.write(headers.encode())
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(4096), 8)
    elapsed = time.monotonic() - started
    writer.close()
    await writer.wait_closed()
    assert b" 408 " in raw.split(b"\r\n")[0] and 4.5 < elapsed < 8
    return {"status": 408, "seconds": round(elapsed, 3)}


async def main():
    report = {"target": f"http://127.0.0.1:{PORT}", "slow_body": await slow()}
    async with httpx.AsyncClient(base_url=report["target"], trust_env=False, timeout=10) as c:
        headers = {"X-GLPI-signature": "0" * 64, "X-GLPI-timestamp": str(int(time.time()))}
        r = await c.post("/v1/servicemind/webhooks/glpi", headers=headers, content=b"x" * 65537)
        assert r.status_code == 413
        report["large_body_status"] = r.status_code

        async def probe():
            start = time.monotonic()
            r = await c.post("/v1/servicemind/webhooks/glpi", headers=headers, content=b"{")
            assert r.status_code in {422, 429}
            return r.status_code, time.monotonic() - start

        rows = await asyncio.gather(*(probe() for _ in range(160)))
        report["burst"] = {
            "requests": len(rows),
            "statuses": {str(s): sum(x[0] == s for x in rows) for s in {422, 429}},
            "max_seconds": round(max(x[1] for x in rows), 3),
        }
        assert any(s == 429 for s, _ in rows)
        start = time.monotonic()
        healthy = await c.get("/health")
        duration = time.monotonic() - start
        assert healthy.status_code == 200 and duration < 2
        report["health_after_burst"] = {
            "status": healthy.status_code,
            "seconds": round(duration, 3),
            "slo_seconds": 2,
        }
    report["verdict"] = "PASS"
    Path(__file__).with_name("network-probe.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


asyncio.run(main())
