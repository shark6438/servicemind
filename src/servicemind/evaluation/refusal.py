"""Mark an on-disk gate report as no longer stating what the gate will reproduce.

Every gate in this phase writes ``evaluation/reports/phase7_<gate>_latest.json`` and ``.md``,
and those files are read by people -- the baseline document cites them, and a reader who
opens one sees a verdict. The gate can also end in ``EXIT_CONFIGURATION``: the case list
moved under the report, the replays span several revisions, a batch header no longer
matches. That path returns before anything is written, which leaves the previous report in
place, and the previous report usually says ``PASS``.

Measured on 2026-10-01: ``phase7_quality_latest.json`` said ``"verdict": "PASS"`` while
``scripts/gate_phase7_quality.py --replay-only --check`` refused to judge the same replays
with exit 3. Nothing in the repository reads those files as an input -- the one cross-gate
consumer, :func:`scripts.verify_phase7_security.acceptance_verdicts`, re-derives the case
digest and refuses a report bound to another case list -- so the harm is a human reading a
stale headline and concluding the opposite of what the gate just said.

So a refusal is recorded rather than implied. The previous document is kept verbatim under
``superseded_report``, the headline verdict becomes ``REFUSED``, and the reason the gate
gave is stored beside it. The file then states what happened: this gate ran, on these
inputs, at this time, and declined to judge. It is not a verdict document any more, and
nothing that wants a verdict should read it as one -- which is why ``cases_digest`` and
``cases`` are deliberately *not* carried to the top level.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Written to the ``verdict`` field of a refused report. Deliberately not a member of any
#: grader's ``Verdict`` enum: a reader that validates the field must fail loudly rather
#: than accept a value that means "no verdict was reached".
REFUSED = "REFUSED"


def stamp_refusal(
    report_json: Path,
    report_md: Path | None,
    *,
    gate: str,
    reason: str,
    argv: Sequence[str] = (),
    at: datetime | None = None,
) -> dict[str, Any]:
    """Record on disk that ``gate`` refused to judge, superseding whatever it said before.

    Never raises for an unwritable or unreadable report: the caller is on its way out with
    a configuration exit code, and a failure to annotate the old file must not replace that
    with a traceback. Returns the document that was written (or the one that was kept, if
    nothing could be written).
    """
    moment = at or datetime.now(tz=UTC)
    previous: Any = None
    previous_note = "no report was on disk"
    if report_json.exists():
        try:
            previous = json.loads(report_json.read_text(encoding="utf-8"))
            previous_note = "the report on disk is preserved under superseded_report"
        except (OSError, json.JSONDecodeError) as exc:
            previous_note = f"the report on disk could not be read back: {exc}"

    document: dict[str, Any] = {
        "gate": gate,
        "verdict": REFUSED,
        "configuration_refusal": {
            "at": moment.isoformat(),
            "exit_code": 3,
            "argv": list(argv),
            "reason": reason,
            "note": previous_note,
        },
    }
    if previous is not None:
        document["superseded_report"] = previous
        document["superseded_verdict"] = (
            previous.get("verdict") if isinstance(previous, dict) else None
        )

    try:
        report_json.parent.mkdir(parents=True, exist_ok=True)
        report_json.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(json.dumps({"refusal_not_recorded": str(exc)}, ensure_ascii=False))
        return document

    if report_md is not None:
        try:
            report_md.parent.mkdir(parents=True, exist_ok=True)
            report_md.write_text(
                render_refusal_markdown(
                    document,
                    prior=report_md.read_text(encoding="utf-8") if report_md.exists() else None,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            print(json.dumps({"refusal_not_recorded": str(exc)}, ensure_ascii=False))
    return document


def render_refusal_markdown(document: dict[str, Any], *, prior: str | None) -> str:
    """The human-readable half: a banner, then whatever the file used to say."""
    refusal = document["configuration_refusal"]
    banner = [
        "# 拒绝判定（configuration refusal）",
        "",
        f"> 生成时间：`{refusal['at']}`",
        f"> gate：`{document['gate']}`",
        f"> 退出码：`{refusal['exit_code']}`（配置/覆盖错误，非判定）",
        "",
        "**本文件此前记录的结论已被取代。** 上述 gate 在本时点对当前输入拒绝判定，理由：",
        "",
        "```",
        str(refusal["reason"]),
        "```",
        "",
    ]
    if "superseded_verdict" in document:
        banner += [
            f"> 被取代的判定：`{document['superseded_verdict']}`"
            f"（生成于 `{(document.get('superseded_report') or {}).get('generated_at', 'unknown')}`）。",
            "> 它是对**更早的一组输入**作出的结论，不是对当前输入作出的结论。",
            "",
        ]
    if prior is None:
        banner += ["磁盘上原本没有报告。", ""]
    else:
        banner += ["---", "", "## 被取代的历史报告（逐字保留）", "", prior]
    return "\n".join(banner)


__all__ = ["REFUSED", "render_refusal_markdown", "stamp_refusal"]
