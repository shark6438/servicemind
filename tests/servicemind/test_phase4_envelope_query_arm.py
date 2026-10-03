"""Contract tests for the envelope-input probe.

The probe makes one claim about the *platform* and one about its own arithmetic. Both can
be wrong without anything raising:

* the platform's rewrite input is a JSON envelope with the question inside it, not the
  question. If that stops being true, every arm in this probe is measuring the
  bare-question input under a new name -- so the shape is asserted against the platform's
  own builder rather than described in a comment;
* the envelope comparison is only evidence if it beats the noise floor of re-running the
  same input. The control is therefore an input the report refuses to render without,
  which is asserted here rather than trusted.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from core import settings

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "probe_phase4_envelope_query_arm",
    ROOT / "scripts/probe_phase4_envelope_query_arm.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

QUESTION = "Why does my IBM MQ .NET client throw a random invalid handle exception?"


# ------------------------------------------------------- what the platform hands the model


@pytest.mark.asyncio
async def test_the_rewrite_input_is_an_envelope_with_the_question_inside_it(monkeypatch) -> None:
    """The gap is that the model is asked to rewrite a JSON array, not the question.

    Asserted on the platform's own builder, so a change to the envelope's shape shows up
    here rather than silently redefining what every arm of the probe measured.
    """
    monkeypatch.setattr(settings, "SERVICEMIND_CONTEXT_ENABLED", True)
    model_query = await MODULE.envelope_for(QUESTION)
    items = json.loads(model_query)
    assert isinstance(items, list) and items
    assert model_query != QUESTION

    by_id = {item["item_id"]: item for item in items}
    assert "task" in by_id
    task = json.loads(by_id["task"]["content"])
    assert task["goal"] == QUESTION


@pytest.mark.asyncio
async def test_the_fast_path_envelope_is_a_task_item_and_a_policy_item(monkeypatch) -> None:
    monkeypatch.setattr(settings, "SERVICEMIND_CONTEXT_ENABLED", True)
    items = json.loads(await MODULE.envelope_for(QUESTION))
    assert sorted(item["source"] for item in items) == ["policy", "task"]


def test_the_knowledge_state_item_carries_the_goal_and_not_the_composed_query() -> None:
    """``_knowledge_query`` composes goal + objective + the reviewer's feedback + the
    evidence already held, and the round that needs the last two is exactly the one that
    re-retrieves. The envelope's state item is built from ``state["goal"]``, so the
    composed parts are absent from the only text the model is asked to rewrite."""
    from uuid import UUID

    from servicemind.context.contracts import ContextAgent
    from servicemind.orchestration.phase5_governance import Phase5Governance

    composed = "goal plus reviewer feedback plus the evidence already held"
    items = Phase5Governance._state_items(
        {
            "goal": QUESTION,
            "allowed_glpi_entity_ids": [1],
            "group_ids": [],
            "profile_ids": [],
            "review_result": {"feedback": composed},
        },
        ContextAgent.KNOWLEDGE,
        frozenset({ContextAgent.KNOWLEDGE}),
        UUID("11111111-1111-4111-8111-1111111111ff"),
    )
    assert json.loads(items[0].content)["query"] == QUESTION
    assert composed not in items[0].content


@pytest.mark.asyncio
async def test_the_task_item_carries_the_objective_but_not_the_reviewers_feedback(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "SERVICEMIND_CONTEXT_ENABLED", True)
    items = json.loads(await MODULE.envelope_for(QUESTION))
    assert "feedback" not in json.dumps(items)
    assert "ledger" not in json.dumps(items)


@pytest.mark.asyncio
async def test_the_probe_refuses_to_run_with_the_context_branch_off(monkeypatch) -> None:
    """With context assembly off there is no envelope, and the arms beside this one would
    be the bare-question arms again -- which already have a report."""
    monkeypatch.setattr(settings, "SERVICEMIND_CONTEXT_ENABLED", False)
    with pytest.raises(RuntimeError, match="no envelope"):
        await MODULE.envelope_for(QUESTION)


# ------------------------------------------------------------- what the comparison counts


def _capture(entries: dict[str, tuple[str | None, list[str]]]) -> dict:
    """A capture shaped like the real one: the anchor, plus the model's own output."""

    def entry(key: str, model_normalization: str | None, rewrites: list[str]) -> dict:
        return {
            "query": key,
            # The anchor is the user's question, so it is identical in every capture --
            # which is exactly why the comparison must not read it.
            "normalized_query": key,
            "model_normalized_query": model_normalization,
            "rewritten_queries": list(rewrites),
        }

    return {"queries": {key: entry(key, norm, rw) for key, (norm, rw) in entries.items()}}


def test_a_changed_model_normalisation_is_counted_as_changed() -> None:
    plain = _capture({"a": ("one", ["x"]), "b": ("two", ["y"])})
    other = _capture({"a": ("different", ["x"]), "b": ("two", ["y"])})
    comparison = MODULE._comparison(plain, other)
    assert comparison["model_output_changed"] == 1
    assert comparison["model_normalisation_changed"] == 1
    assert comparison["unchanged"] == 1
    assert comparison["model_output_changed_share"] == 0.5
    assert [item["query"] for item in comparison["examples"]] == ["a"]


def test_the_anchor_cannot_carry_a_change_even_when_it_differs_between_captures() -> None:
    """The regression this comparison was rewritten for.

    The probe used to compare ``normalized_query``. While the processor overwrote it with
    the model's normalization that was a fair proxy; now that the user's question is the
    anchor, the field is the same string in every capture and a comparison over it reports
    a sensitivity of zero for any input whatsoever. The model's own fields are what move.
    """
    plain = _capture({"a": ("one", ["x"])})
    other = _capture({"a": ("one", ["x"])})
    other["queries"]["a"]["normalized_query"] = "a totally different anchor"
    comparison = MODULE._comparison(plain, other)
    assert comparison["model_output_changed"] == 0
    assert comparison["unchanged"] == 1


def test_the_same_model_normalisation_with_different_rewrites_is_counted_separately() -> None:
    plain = _capture({"a": ("one", ["x", "y"])})
    other = _capture({"a": ("one", ["x", "z"])})
    comparison = MODULE._comparison(plain, other)
    assert comparison["model_output_changed"] == 0
    assert comparison["only_the_rewrites_changed"] == 1
    assert comparison["unchanged"] == 0


def test_an_entry_the_model_never_answered_is_excluded_rather_than_compared() -> None:
    """A fallback is not an observation of the model on either input."""
    plain = _capture({"a": (None, []), "b": ("two", [])})
    other = _capture({"a": ("one", []), "b": ("two", [])})
    comparison = MODULE._comparison(plain, other)
    assert comparison["queries_without_a_model_answer"] == 1
    assert comparison["queries_compared"] == 1
    assert comparison["model_output_changed"] == 0
    assert comparison["unchanged"] == 1


def test_whitespace_and_case_do_not_count_as_a_changed_query() -> None:
    """The stage's punctuation varies run to run; counting that as a change would make
    the noise floor look like a finding."""
    plain = _capture({"a": ("One  two", ["x"])})
    other = _capture({"a": ("one two", ["X"])})
    comparison = MODULE._comparison(plain, other)
    assert comparison["model_output_changed"] == 0
    assert comparison["only_the_rewrites_changed"] == 0
    assert comparison["unchanged"] == 1


def test_the_examples_stop_at_three() -> None:
    plain = _capture({str(index): ("one", []) for index in range(5)})
    other = _capture({str(index): ("other", []) for index in range(5)})
    comparison = MODULE._comparison(plain, other)
    assert comparison["model_output_changed"] == 5
    assert len(comparison["examples"]) == 3


def test_only_the_queries_both_captures_hold_are_compared() -> None:
    plain = _capture({"a": ("one", []), "b": ("two", [])})
    other = _capture({"b": ("two", [])})
    comparison = MODULE._comparison(plain, other)
    assert comparison["queries_compared"] == 1
    assert comparison["unchanged"] == 1


def test_the_control_is_required_rather_than_omitted(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(MODULE, "REPEAT_CAPTURE", tmp_path / "absent.json")
    with pytest.raises(FileNotFoundError, match="noise floor"):
        MODULE._report({"queries": {}})


# ------------------------------------------------------------------------------ the gate


def _payload() -> dict:
    arm = {
        "use_rewrites": True,
        "rewrite_capture": "envelope",
        "recall_at_5": 0.7,
        "recall_at_10": 0.75,
        "recall_at_20": 0.8,
        "packed_documents": {"max": 16},
    }
    sensitivity = {
        "queries_compared": 400,
        "queries_without_a_model_answer": 0,
        "model_output_changed": 280,
        "model_output_changed_share": 0.7,
        "model_normalisation_changed": 280,
        "only_the_rewrites_changed": 110,
        "unchanged": 10,
        "examples": [],
        "reading": "r",
        "noise_floor": {
            "queries_compared": 400,
            "queries_without_a_model_answer": 0,
            "model_output_changed": 113,
            "model_output_changed_share": 0.2825,
            "model_normalisation_changed": 113,
            "only_the_rewrites_changed": 155,
            "unchanged": 132,
        },
    }
    return {
        "status": "ENVELOPE_QUERY_ARM_MEASUREMENT",
        "generated_at": "2026-10-02T00:00:00+00:00",
        "inputs": {
            "index_prefix": "sm-techqa-arms-v1",
            "funnel_report": {"path": "evaluation/reports/phase4_query_arm_funnel_latest.json"},
            "envelope_capture": {"sha256": ""},
            "plain_capture": {"sha256": ""},
            "repeat_capture": {"sha256": ""},
        },
        "corpus": {
            "active_generation": "a5daf639d749",
            "children_on_cluster": 199409,
            "parents_on_cluster": 46691,
            "rebuilt_here": False,
        },
        "envelope_input": {
            "call_site": "supervisor_workflow.py:1000",
            "builder": "build_fast_knowledge_context",
            "deployed_value": "true",
        },
        "rewrite_sensitivity": sensitivity,
        "arms": {"c0_env": arm, "c0_mq_env": arm, "c0_mq": arm},
        "published_funnel_arms": {"c0_off": {"recall_at_10": 0.7214, "packed_max": 16}},
        "limitations": ["silver labels"],
    }


def _gate(monkeypatch, tmp_path: Path, *, stale: str | None = None, edit_markdown: bool = False):
    import hashlib

    files = {}
    for key, name in (
        ("producer", "producer.py"),
        ("envelope_capture", "envelope.json"),
        ("plain_capture", "plain.json"),
        ("repeat_capture", "repeat.json"),
        ("funnel_report", "funnel.json"),
    ):
        path = tmp_path / name
        path.write_text(f"{key}", encoding="utf-8")
        files[key] = path

    payload = _payload()
    for key, path in files.items():
        payload["inputs"][key] = {
            **payload["inputs"].get(key, {}),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    if stale:
        payload["inputs"][stale]["sha256"] = "f" * 64

    out_json = tmp_path / "out.json"
    out_md = tmp_path / "out.md"
    out_json.write_text(json.dumps(payload), encoding="utf-8")
    out_md.write_text(
        MODULE.render_markdown(payload) + ("tampered" if edit_markdown else ""), encoding="utf-8"
    )

    monkeypatch.setattr(MODULE, "OUT_JSON", out_json)
    monkeypatch.setattr(MODULE, "OUT_MD", out_md)
    monkeypatch.setattr(MODULE, "PRODUCER", files["producer"])
    monkeypatch.setattr(MODULE, "ENVELOPE_CAPTURE", files["envelope_capture"])
    monkeypatch.setattr(MODULE, "PLAIN_CAPTURE", files["plain_capture"])
    monkeypatch.setattr(MODULE, "REPEAT_CAPTURE", files["repeat_capture"])
    monkeypatch.setattr(MODULE, "FUNNEL_REPORT", files["funnel_report"])
    monkeypatch.setattr(sys, "argv", ["probe", "--check"])
    return payload


def test_the_markdown_shows_the_noise_floor_beside_the_envelope_column() -> None:
    """Reading the envelope number without its control is how sampling gets reported as
    a finding."""
    rendered = MODULE.render_markdown(_payload())
    assert "噪声对照" in rendered
    assert "113（28.25%）" in rendered
    assert "280（70.00%）" in rendered


def test_a_missing_report_fails_the_gate(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(MODULE, "OUT_JSON", tmp_path / "absent.json")
    monkeypatch.setattr(MODULE, "OUT_MD", tmp_path / "absent.md")
    monkeypatch.setattr(sys, "argv", ["probe", "--check"])
    assert MODULE.main() == 1


def test_a_consistent_report_passes_the_gate(monkeypatch, tmp_path, capsys) -> None:
    _gate(monkeypatch, tmp_path)
    assert MODULE.main() == 0
    assert "PASS" in capsys.readouterr().out


def test_a_control_that_moved_since_the_run_fails_the_gate(monkeypatch, tmp_path, capsys) -> None:
    _gate(monkeypatch, tmp_path, stale="repeat_capture")
    assert MODULE.main() == 1
    assert "stale_inputs=['repeat_capture']" in capsys.readouterr().out


def test_a_report_whose_producer_moved_fails_the_gate(monkeypatch, tmp_path, capsys) -> None:
    """Same binding as the funnel's: the report is an output of this file."""
    _gate(monkeypatch, tmp_path, stale="producer")
    assert MODULE.main() == 1
    assert "stale_inputs=['producer']" in capsys.readouterr().out


def test_a_markdown_that_no_longer_matches_the_json_fails_the_gate(
    monkeypatch, tmp_path, capsys
) -> None:
    _gate(monkeypatch, tmp_path, edit_markdown=True)
    assert MODULE.main() == 1
    assert "markdown_matches=False" in capsys.readouterr().out


# ------------------------------------------------- the corpus the arms were measured on


class _FakeIndex:
    def __init__(self, active: str | None) -> None:
        self._active = active

    async def active_generation(self, tenant_id) -> str | None:
        return self._active

    def child_alias(self, tenant_id) -> str:
        return "children-alias"

    def parent_alias(self, tenant_id) -> str:
        return "parents-alias"


class _FakeClient:
    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts
        self.asked: list[str] = []

    async def count(self, *, index: str) -> dict:
        self.asked.append(index)
        return {"count": self._counts.get(index, 0)}


@pytest.mark.asyncio
async def test_the_probe_refuses_an_index_it_cannot_reach() -> None:
    """The probe never ingests, so no active generation means it would measure nothing.

    A zero written into the payload reads as a retriever that found nothing, which is
    the same "plausible number" failure the funnel's reuse path already refuses.
    """
    with pytest.raises(RuntimeError, match="no active generation"):
        await MODULE._measurable_corpus(_FakeClient({}), _FakeIndex(None))


@pytest.mark.asyncio
async def test_the_probe_refuses_an_empty_parent_generation() -> None:
    """Every hit is dropped at parent expansion, so every arm would read zero."""
    client = _FakeClient({"children-alias": 199409})
    with pytest.raises(RuntimeError, match="parent generation"):
        await MODULE._measurable_corpus(client, _FakeIndex("a5daf639d749"))


@pytest.mark.asyncio
async def test_the_probe_reads_the_alias_counts_under_the_tenants_own_aliases() -> None:
    """The guard has to count what the readers use -- the aliases -- not a glob."""
    client = _FakeClient({"children-alias": 199409, "parents-alias": 46691})
    active, children, parents = await MODULE._measurable_corpus(client, _FakeIndex("a5daf639d749"))
    assert (active, children, parents) == ("a5daf639d749", 199409, 46691)
    assert client.asked == ["children-alias", "parents-alias"]


def test_the_report_carries_the_generation_it_measured_on() -> None:
    """A count with no generation beside it cannot be told from a later index's."""
    payload = _payload()
    assert payload["corpus"]["active_generation"] == "a5daf639d749"
    assert payload["corpus"]["parents_on_cluster"] == 46691
    assert "a5daf639d749" in MODULE.render_markdown(payload)


def test_a_capture_that_cannot_say_who_wrote_it_is_not_reused() -> None:
    """The probe's columns are a claim about the model, so the sample must say so.

    ``QueryProcessor`` reports a swallowed model failure as an ordinary deterministic
    query. An unflagged capture may therefore be the fallback wearing the model's name,
    and comparing it against the envelope would be comparing two things neither of which
    is what the caption says.
    """
    flagged = {
        "queries": {
            "TRAIN_Q001": {"provenance": "model", "model_normalized_query": "x"},
        }
    }
    unflagged = {"queries": {"TRAIN_Q001": {"normalized_query": "x"}}}
    # Recorded before the model's output got its own field: the normalization it holds
    # under ``normalized_query`` is now the anchor slot, so replaying it would compare
    # the user's question against itself and publish the zero as a sensitivity.
    pre_anchor = {"queries": {"TRAIN_Q001": {"provenance": "model", "normalized_query": "x"}}}
    assert MODULE._capture_is_reusable(flagged, {"TRAIN_Q001"}) is True
    assert MODULE._capture_is_reusable(unflagged, {"TRAIN_Q001"}) is False
    assert MODULE._capture_is_reusable(pre_anchor, {"TRAIN_Q001"}) is False
    # A capture of another query set is stale for the same reason.
    assert MODULE._capture_is_reusable(flagged, {"TRAIN_Q002"}) is False
