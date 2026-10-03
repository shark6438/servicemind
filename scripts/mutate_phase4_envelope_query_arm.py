"""Revert each decision the envelope probe rests on, and require the tests to notice.

The probe's finding is "the rewrite stage is handed a JSON envelope, and that moves the
text retrieval searches with for 70% of queries". Three ways to get that wrong all look
like a normal run:

* the comparison stops separating the envelope from the sampling noise, so the number it
  prints is mostly the stage's own variance;
* the control becomes optional, so the report renders a finding with nothing to compare
  it against;
* the absence of an envelope stops being fatal, so the "envelope" arms silently become
  the bare-question arms that already have a report.

None of the three raises. The last is the one the pattern is most prone to: a fallback
added for convenience turns every number below it into a claim about a different input.
"""

import pathlib
from pathlib import Path

from mutation_harness import Mutation, run_mutations

ROOT = pathlib.Path(__file__).resolve().parent.parent
#: Stable across runs: an interrupted run journals under this name, and a rename would
#: orphan the journal at a path the next run never looks at.
EXPERIMENT = "phase4_envelope_query_arm"
SCRIPT = ROOT / "scripts/probe_phase4_envelope_query_arm.py"
TEST = "tests/servicemind/test_phase4_envelope_query_arm.py"

CHANGED = f"{TEST}::test_a_changed_model_normalisation_is_counted_as_changed"
ANCHOR_NEUTRAL = (
    f"{TEST}::test_the_anchor_cannot_carry_a_change_even_when_it_differs_between_captures"
)
NO_ANSWER = f"{TEST}::test_an_entry_the_model_never_answered_is_excluded_rather_than_compared"
REWRITES_ONLY = (
    f"{TEST}::test_the_same_model_normalisation_with_different_rewrites_is_counted_separately"
)
CASE = f"{TEST}::test_whitespace_and_case_do_not_count_as_a_changed_query"
CAP = f"{TEST}::test_the_examples_stop_at_three"
CONTROL_REQUIRED = f"{TEST}::test_the_control_is_required_rather_than_omitted"
ENVELOPE_FATAL = f"{TEST}::test_the_probe_refuses_to_run_with_the_context_branch_off"
MARKDOWN_CONTROL = f"{TEST}::test_the_markdown_shows_the_noise_floor_beside_the_envelope_column"
GATE_CONTROL = f"{TEST}::test_a_control_that_moved_since_the_run_fails_the_gate"
GATE_MARKDOWN = f"{TEST}::test_a_markdown_that_no_longer_matches_the_json_fails_the_gate"
GATE_PRODUCER = f"{TEST}::test_a_report_whose_producer_moved_fails_the_gate"
UNREACHABLE = f"{TEST}::test_the_probe_refuses_an_index_it_cannot_reach"
EMPTY_PARENTS = f"{TEST}::test_the_probe_refuses_an_empty_parent_generation"
UNFLAGGED_CAPTURE = f"{TEST}::test_a_capture_that_cannot_say_who_wrote_it_is_not_reused"

SAME_NORM = (
    "        same_norm = (\n"
    '            " ".join(left_norm.split()).casefold() == " ".join(right_norm.split()).casefold()\n'
    "        )"
)

M13_ANCHOR = (
    "    if not all(\n"
    '        isinstance(entry, dict) and "provenance" in entry and "model_normalized_query" in entry\n'
    "        for entry in queries.values()\n"
    "    ):\n"
    "        return False\n"
)

MUTATIONS = [
    (
        "M01 the comparison stops normalising before comparing, so the stage's own "
        "punctuation and spacing are counted as a change the envelope caused",
        SCRIPT,
        SAME_NORM,
        "        same_norm = left_norm == right_norm",
        CASE,
    ),
    (
        "M02 the comparison walks no queries, so every arm's sensitivity reads as zero",
        SCRIPT,
        "    for query_id in shared:",
        "    for query_id in shared[:0]:",
        f"{CHANGED} {REWRITES_ONLY}",
    ),
    (
        "M03 a query whose rewrites changed but whose searched text did not is counted as "
        "unchanged, hiding the half of the effect that never reaches the dense arm",
        SCRIPT,
        "        elif left_rewrites != right_rewrites:\n            rewrites_only.append(query_id)",
        "        else:\n            pass",
        REWRITES_ONLY,
    ),
    (
        "M04 the worked examples are not capped, so one report carries all four hundred",
        SCRIPT,
        "            if len(examples) < 3:",
        "            if True:",
        CAP,
    ),
    (
        "M05 the envelope comparison no longer requires its noise control",
        SCRIPT,
        "    if not REPEAT_CAPTURE.exists():",
        "    if False:",
        CONTROL_REQUIRED,
    ),
    (
        "M06 the probe falls back to the bare question when the platform returns no "
        "envelope, which is the silent redefinition the guard exists to prevent",
        SCRIPT,
        "    if envelope is None:\n        raise RuntimeError(\n"
        '            "the platform returned no envelope: SERVICEMIND_CONTEXT_ENABLED is off, and "\n'
        '            "this probe would then be measuring the bare-question arm under a new name"\n'
        "        )",
        "    if envelope is None:\n        return query",
        ENVELOPE_FATAL,
    ),
    (
        "M07 the markdown prints the envelope column where the control column belongs",
        SCRIPT,
        '| 模型输出改变 | **{sensitivity["model_output_changed"]}'
        '（{sensitivity["model_output_changed_share"] * 100:.2f}%）** | '
        '{noise["model_output_changed"]}（{noise["model_output_changed_share"] * 100:.2f}%） |',
        '| 模型输出改变 | **{sensitivity["model_output_changed"]}'
        '（{sensitivity["model_output_changed_share"] * 100:.2f}%）** | '
        '{sensitivity["model_output_changed"]}'
        '（{sensitivity["model_output_changed_share"] * 100:.2f}%） |',
        MARKDOWN_CONTROL,
    ),
    (
        "M08 the gate stops treating the control capture as an input, so the report keeps "
        "claiming a noise floor measured against a file that has since been replaced",
        SCRIPT,
        '                ("repeat_capture", REPEAT_CAPTURE),',
        '                ("funnel_report", FUNNEL_REPORT),',
        GATE_CONTROL,
    ),
    (
        "M09 the gate stops comparing the markdown against the JSON it was rendered from",
        SCRIPT,
        '        markdown_matches = OUT_MD.read_text(encoding="utf-8") == render_markdown(payload)',
        "        markdown_matches = True",
        GATE_MARKDOWN,
    ),
    (
        "M11 the probe stops refusing an index with no active generation, so it measures "
        "nothing and writes the zero out as a measurement",
        SCRIPT,
        "    if active is None:",
        "    if active is None and False:",
        UNREACHABLE,
    ),
    (
        "M12 the probe stops refusing an empty parent generation, so every hit is dropped "
        "at parent expansion and every arm reads zero",
        SCRIPT,
        "    if parents == 0:",
        "    if parents == 0 and False:",
        EMPTY_PARENTS,
    ),
    (
        "M10 the report stops binding its producer, so these rows survive any edit to how "
        "they were measured",
        SCRIPT,
        '                ("producer", PRODUCER),\n                ("envelope_capture", ENVELOPE_CAPTURE),',
        '                ("envelope_capture", ENVELOPE_CAPTURE),\n                ("envelope_capture", ENVELOPE_CAPTURE),',
        GATE_PRODUCER,
    ),
    (
        "M13 the probe reuses an unflagged capture again, so a swallowed model failure is "
        "published as the stage's own output",
        SCRIPT,
        M13_ANCHOR,
        "",
        UNFLAGGED_CAPTURE,
    ),
    (
        "M14 the comparison reads the anchor again, so a sensitivity of zero is reported "
        "for any input -- which is the field the fix made constant",
        SCRIPT,
        '    normalization = entry.get("model_normalized_query")',
        '    normalization = entry.get("normalized_query")',
        ANCHOR_NEUTRAL,
    ),
    (
        "M15 a fallback is compared as though it were the model's answer, so a swallowed "
        "failure is published as the envelope's effect",
        SCRIPT,
        "        if left_norm is None or right_norm is None:",
        "        if False:",
        NO_ANSWER,
    ),
]

if __name__ == "__main__":
    raise SystemExit(
        run_mutations(
            root=ROOT,
            experiment=EXPERIMENT,
            mutations=[
                Mutation(name=name, path=Path(path), old=old, new=new, tests=tuple(tests.split()))
                for name, path, old, new, tests in MUTATIONS
            ],
        )
    )
