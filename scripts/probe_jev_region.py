"""One-shot region probe for the Jev decision endpoint.

Sends exactly one request through the frozen wire format, from whichever
network path you point it at, and prints the routing verdict. Never prints the
API key. Costs USD 0.00 when the request is rejected (no inference happens)
and at most USD 0.0000314 when it succeeds.

Usage:
    uv run python scripts/probe_jev_region.py                  # direct (TUN/global VPN)
    uv run python scripts/probe_jev_region.py --proxy http://127.0.0.1:2080
    uv run python scripts/probe_jev_region.py --dry-run        # build payload, send nothing
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]

import compare_jev_routing as wire  # noqa: E402

OUT = ROOT / "evaluation/experiments/jev_region_probe"
PROBE_TEXT = "What is the status of ticket INC-12345?"
GEO_MESSAGE = "not available in your region"


def key_from_environment() -> str:
    value = os.environ.get("OPENROUTER_API_KEY")
    if not value:
        value = dotenv_values(ROOT / ".env").get("OPENROUTER_API_KEY")
    if not value:
        raise SystemExit("OPENROUTER_API_KEY not found in the environment or .env")
    return value


def egress(client: httpx.Client) -> str:
    try:
        data = client.get("https://ipinfo.io/json", timeout=15).json()
        return f"{data.get('ip')} {data.get('country')} {data.get('city')} ({data.get('org')})"
    except (httpx.HTTPError, ValueError):
        return "unavailable"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy", default=None, help="explicit proxy URL; omit for direct")
    parser.add_argument("--text", default=PROBE_TEXT)
    parser.add_argument("--request-write", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    payload = wire.payload_for(
        {"text": args.text, "request_write": args.request_write}, "openrouter"
    )
    if args.dry_run:
        print(f"DRY_RUN payload_bytes={len(wire.encoded(payload))} model={payload['model']}")
        return 0

    key = key_from_environment()
    started = datetime.now(UTC)
    transport = {"proxy": args.proxy} if args.proxy else {}
    with httpx.Client(trust_env=False, timeout=30, follow_redirects=False, **transport) as client:
        where = egress(client)
        print(f"path     : {'proxy ' + args.proxy if args.proxy else 'direct (trust_env=False)'}")
        print(f"egress   : {where}")
        try:
            response = client.post(
                wire.PROVIDERS["openrouter"]["endpoint"],
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            )
        except httpx.HTTPError as exc:
            print(f"transport: {type(exc).__name__}: {exc}")
            return 5

    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:400]}

    receipt = {
        "at": started.isoformat(),
        "proxy": args.proxy,
        "egress": where,
        "status": response.status_code,
        "response": {
            field: body[field]
            for field in ("id", "model", "provider", "usage", "answers", "error")
            if field in body
        },
    }
    receipt = json.loads(json.dumps(receipt).replace(key, "[REDACTED]"))
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    (OUT / f"probe_{stamp}_{response.status_code}.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"status   : {response.status_code}")
    print(json.dumps(receipt["response"], ensure_ascii=False, indent=2)[:1800])
    if response.status_code == 200:
        print("verdict  : ACCEPTED — this network path can serve the model")
        return 0
    message = json.dumps(receipt["response"].get("error", ""), ensure_ascii=False)
    if response.status_code == 403 and GEO_MESSAGE in message:
        print("verdict  : GEO_BLOCKED — region gate still filtering the only endpoint")
        return 3
    print("verdict  : OTHER — inspect the response above")
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
