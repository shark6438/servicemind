#!/usr/bin/env python
"""Run the acceptance pipeline's own steps and record what they actually did.

The per-case replays say what happened to a run. They say nothing about the steps that
had to succeed *before* a case could mean anything -- seeding four identities, planting
the knowledge fixtures, proving the index aliases switch -- or about the regression that
has to hold afterwards. Those steps live in separate scripts with separate exit codes,
and the report is not allowed to describe them from memory.

So this runs each stage for real, captures the command, its exit code and its output,
and writes ``evaluation/reports/phase7_pipeline_evidence.json`` for the gate to render.

Two things it is careful about:

* **No secret reaches the record.** The acceptance configuration lives in
  ``deploy/glpi/.env``, and a script that echoes its environment would put a password in
  a committed file. Every captured string is scrubbed against the *values* of the
  sensitive keys in that file before it is written, so a leak has to defeat the scrubber
  rather than merely be overlooked.
* **A failing stage is recorded, not swallowed.** The stage list is data; the exit code
  is the finding. A pipeline that only recorded its successes would be a pipeline whose
  report cannot show a broken precondition.

Usage::

    uv run python scripts/collect_phase7_pipeline_evidence.py            # every stage
    uv run python scripts/collect_phase7_pipeline_evidence.py --skip regression
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS = REPO_ROOT / "evaluation" / "reports"
OUTPUT = REPORTS / "phase7_pipeline_evidence.json"
ENV_FILE = REPO_ROOT / "deploy" / "glpi" / ".env"

#: Captured output is truncated to this many characters per stream. The report is a
#: record, not a log dump; the full text is reproducible by re-running the stage.
OUTPUT_LIMIT = 6000

#: Keys whose *values* must never appear in captured output or in the record.
SECRET_KEY = re.compile(r"(PASSWORD|SECRET|TOKEN|API_KEY|CREDENTIAL)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Stage:
    """One pipeline step: what it is for, and the exact command that performs it."""

    id: str
    purpose: str
    command: list[str]
    #: True when the stage must be run by the *same* interpreter environment, i.e. it is
    #: a project script rather than a shell command.
    is_pytest: bool = False


STAGES: tuple[Stage, ...] = (
    Stage(
        id="environment",
        purpose="冻结被测环境：服务清单、systemd unit、健康检查、源码版本与未提交改动",
        command=["bash", "-lc", "true"],
    ),
    Stage(
        id="identity",
        purpose="身份播种结果核对：四个验收主体存在、凭据可用、无声明漂移（只读）",
        command=["scripts/reconcile_phase7_acceptance_identity.py", "--check"],
    ),
    Stage(
        id="identity-drift",
        purpose="既有记忆审核主体的双向漂移核对（只读，不创建身份）",
        command=["scripts/reconcile_phase5_memory_review_identity.py", "--check"],
    ),
    Stage(
        id="fixtures",
        purpose="知识夹具与工单夹具的幂等核对（只读；未播种时应非零退出）",
        command=["scripts/seed_phase7_acceptance_fixtures.py", "--check"],
    ),
    Stage(
        id="index-lifecycle",
        purpose="真 OpenSearch 上的索引蓝绿生命周期探针",
        command=[
            "tests/servicemind/test_phase4_index_lifecycle_live.py",
            "--run-docker",
        ],
        is_pytest=True,
    ),
    Stage(
        id="regression",
        purpose="全仓回归：tests/servicemind + tests/service",
        command=["tests/servicemind", "tests/service"],
        is_pytest=True,
    ),
)


def _sensitive_values() -> dict[str, str]:
    """Read ``deploy/glpi/.env`` and return the *values* of keys that name a secret.

    Only assignments whose key looks sensitive are collected, and the values never leave
    this function except as scrub patterns. The file itself is not printed, and the
    returned mapping is never serialised.
    """
    if not ENV_FILE.exists():
        return {}
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if SECRET_KEY.search(key) and len(value) >= 6:
            values[key] = value
    return values


def scrub(text: str, secrets: dict[str, str]) -> str:
    """Replace every occurrence of a sensitive value with its variable name.

    The name is kept so the record still says *which* setting was in play -- a report
    that silently deleted the text could not distinguish "the password was empty" from
    "the password was here".
    """
    for key, value in secrets.items():
        if value in text:
            text = text.replace(value, f"<redacted:{key}>")
    return text


def _shell(stage: Stage) -> list[str]:
    if stage.id == "environment":
        return [
            "bash",
            "-lc",
            "echo '--- servicemind containers ---'; "
            "docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -i servicemind | sort; "
            "echo '--- api unit ---'; "
            "systemctl --user show servicemind-api -p ActiveEnterTimestamp -p ActiveState "
            "-p ExecStart -p Restart; "
            "echo '--- health ---'; curl -s -m 5 http://127.0.0.1:18080/health; echo; "
            "echo '--- revision ---'; git rev-parse HEAD; "
            "echo '--- uncommitted files ---'; git status --porcelain | wc -l",
        ]
    if stage.is_pytest:
        return ["uv", "run", "pytest", *stage.command, "-q"]
    return ["uv", "run", "python", *stage.command]


def run_stage(stage: Stage, secrets: dict[str, str]) -> dict[str, object]:
    argv = _shell(stage)
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - the argv is fixed in this file
            argv,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        code: int | None = completed.returncode
        stdout, stderr = completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        code = None
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr if isinstance(exc.stderr, str) else "") + "\n[timed out after 1800s]"
    except FileNotFoundError as exc:
        code = None
        stdout, stderr = "", f"command not found: {exc}"

    return {
        "id": stage.id,
        "purpose": stage.purpose,
        "command": " ".join(argv),
        "exit_code": code,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "stdout": scrub(stdout, secrets)[-OUTPUT_LIMIT:],
        "stderr": scrub(stderr, secrets)[-OUTPUT_LIMIT:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip", default="", help="comma-separated stage ids to skip")
    parser.add_argument("--only", default="", help="comma-separated stage ids to run")
    args = parser.parse_args()

    skipped = {item.strip() for item in args.skip.split(",") if item.strip()}
    only = {item.strip() for item in args.only.split(",") if item.strip()}
    selected = [
        stage for stage in STAGES if stage.id not in skipped and (not only or stage.id in only)
    ]
    if not selected:
        print(json.dumps({"error": "no stages selected"}, ensure_ascii=False))
        return 3

    secrets = _sensitive_values()
    results = []
    for stage in selected:
        print(f"[{stage.id}] {stage.purpose}", file=sys.stderr, flush=True)
        result = run_stage(stage, secrets)
        print(f"[{stage.id}] exit {result['exit_code']}", file=sys.stderr, flush=True)
        results.append(result)

    REPORTS.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(tz=UTC).isoformat(),
                "repo_revision": subprocess.run(
                    ["git", "rev-parse", "HEAD"],  # noqa: S607 - fixed argv
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip(),
                # Recorded so a reader can tell a scrubbed record from an unscanned one.
                "secret_keys_scrubbed": sorted(secrets),
                "stages": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"written": str(OUTPUT), "stages": [r["id"] for r in results]}, ensure_ascii=False
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
