"""Contract tests for the label diagnostic's pure helpers.

The diagnostic itself reads two gitignored inputs (the corpus zip and the proxy's candidate
dump), so a test that exercised it end to end could only run on the machine that produced
them -- and a check that cannot run in CI is not a check. These tests therefore drive the
pure functions with synthetic rows and a synthetic zip, which is where the arithmetic that
becomes the published finding actually lives.
"""

from __future__ import annotations

import importlib.util
import io
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "diagnose_phase4_label_diagnostic", ROOT / "scripts/diagnose_phase4_label_diagnostic.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _row(
    query_id: str,
    question: str,
    relevant: list[str],
    *,
    dense: list[str] | None = None,
    production_blend: list[str] | None = None,
) -> dict[str, Any]:
    """One query's record, with only the arms these tests read.

    ``dense`` and ``production_blend`` are the two arms the diagnostic uses for different
    jobs -- the first hit's title comes from ``dense``, and whether the gold was in the
    deciding pool comes from ``production_blend`` -- so keeping them separable here is what
    lets a test show the two are not accidentally the same list.
    """
    row: dict[str, Any] = {"query_id": query_id, "question": question, "relevant": relevant}
    row["dense"] = list(dense if dense is not None else relevant)
    row["production_blend"] = list(
        production_blend if production_blend is not None else row["dense"]
    )
    return row


# --------------------------------------------------------------------------- _words


def test_words_drops_stopwords_digits_and_single_characters() -> None:
    words = MODULE._words("How do I fix the SSH 5620 IT?")
    # Four different reasons in one sentence: ``how``/``do`` are stopwords, ``I`` is one
    # character, ``5620`` is a digit run, and ``IT`` is both a stopword and -- once folded --
    # the pronoun. Only the two content words survive.
    assert words == frozenset({"fix", "ssh"})


def test_words_keeps_two_letter_acronyms() -> None:
    """The length filter is ``> 1``: a two-letter token is a word, a one-letter is not.

    The single letter has to be one the stop list does not already cover -- ``a`` and ``I``
    are stopwords, so asserting on them would leave the length filter with nothing to do
    and the test green even with the filter removed.
    """
    assert MODULE._words("db DB") == frozenset({"db"})
    assert MODULE._words("x") == frozenset()
    assert MODULE._words("x db z") == frozenset({"db"})


def test_words_splits_on_everything_that_is_not_alphanumeric() -> None:
    assert MODULE._words("nco-p-alcatel_5620.sam/v13") == frozenset(
        {"nco", "alcatel", "sam", "v13"}
    )


def test_words_is_empty_for_a_title_that_is_all_stopwords() -> None:
    assert MODULE._words("The Of What Why") == frozenset()


# ------------------------------------------------------------------------ _jaccard


def test_jaccard_is_zero_when_both_sides_are_empty() -> None:
    """The empty-vs-empty case is the one that would otherwise divide by zero.

    It arises for real: a title with only stopwords yields an empty word set, and the
    published table has to be able to contain that row rather than crash on it.
    """
    assert MODULE._jaccard(frozenset(), frozenset()) == 0.0


def test_jaccard_endpoints_and_midpoint() -> None:
    assert MODULE._jaccard(frozenset({"a"}), frozenset({"a"})) == 1.0
    assert MODULE._jaccard(frozenset({"a"}), frozenset({"b"})) == 0.0
    assert MODULE._jaccard(frozenset({"a", "b"}), frozenset({"b", "c"})) == 1 / 3


def test_jaccard_is_symmetric() -> None:
    left, right = frozenset({"a", "b", "c"}), frozenset({"c", "d"})
    assert MODULE._jaccard(left, right) == MODULE._jaccard(right, left)


# ------------------------------------------------------------------------- _rank_of


def test_rank_of_is_one_based_and_none_when_absent() -> None:
    row = _row("Q1", "q", ["gold"], dense=["x", "y", "gold", "z"])
    assert MODULE._rank_of(row, "dense") == 3
    assert MODULE._rank_of(_row("Q2", "q", ["gold"], dense=["x"]), "dense") is None


def test_rank_of_takes_the_earliest_match_when_several_are_relevant() -> None:
    row = _row("Q1", "q", ["second", "first"], dense=["first", "second"])
    assert MODULE._rank_of(row, "dense") == 1


def test_rank_of_reads_the_arm_it_is_asked_for() -> None:
    """Two arms can disagree about whether the gold is present at all.

    This is the property the whole diagnostic rests on -- the funnel ceiling is measured on
    ``production_blend`` while the first hit's title comes from ``dense`` -- so a helper
    that silently read one arm for both would make the finding unmeasurable.
    """
    row = _row("Q1", "q", ["gold"], dense=["gold"], production_blend=["x", "y"])
    assert MODULE._rank_of(row, "dense") == 1
    assert MODULE._rank_of(row, "production_blend") is None


# ---------------------------------------------------------------------- _arm_summary


def test_arm_summary_divides_by_all_queries_not_by_the_hits() -> None:
    rows = [
        _row("Q1", "q", ["g1"], dense=["g1"]),
        _row("Q2", "q", ["g2"], dense=["g2"]),
        _row("Q3", "q", ["g3"], dense=["x"]),
        _row("Q4", "q", ["g4"], dense=["y"]),
    ]
    summary = MODULE._arm_summary(rows, "dense")
    assert summary["contains_gold"] == 2
    assert summary["contains_gold_rate"] == 0.5
    assert summary["gold_in_top_5"] == 2
    assert summary["gold_in_top_5_rate"] == 0.5
    assert summary["median_rank_when_present"] == 1
    assert summary["list_lengths_observed"] == [1]


def test_arm_summary_top_5_excludes_rank_6_but_contains_gold_keeps_it() -> None:
    row = _row("Q1", "q", ["gold"], dense=["a", "b", "c", "d", "e", "gold"])
    summary = MODULE._arm_summary([row], "dense")
    assert summary["contains_gold"] == 1
    assert summary["gold_in_top_5"] == 0
    assert summary["gold_in_top_5_rate"] == 0.0


def test_arm_summary_reports_the_deepest_list_and_every_distinct_length() -> None:
    rows = [
        _row("Q1", "q", ["g1"], dense=["g1"] * 3),
        _row("Q2", "q", ["g2"], dense=["g2"] * 5),
        _row("Q3", "q", ["g3"], dense=[]),
    ]
    summary = MODULE._arm_summary(rows, "dense")
    # ``declared_depth`` is the maximum, not the first: bm25 returned short lists on some
    # queries in the real dump (84..100), and reporting the first would understate the pool.
    assert summary["declared_depth"] == 5
    assert summary["list_lengths_observed"] == [0, 3, 5]
    assert summary["contains_gold"] == 2


def test_arm_summary_on_no_rows_is_zero_rather_than_a_division_error() -> None:
    summary = MODULE._arm_summary([], "dense")
    assert summary["contains_gold_rate"] == 0.0
    assert summary["gold_in_top_5_rate"] == 0.0
    assert summary["median_rank_when_present"] is None
    assert summary["declared_depth"] == 0


# ---------------------------------------------------------- _title_overlap_breakdown


def test_title_overlap_counts_a_tie_as_the_label_winning() -> None:
    """``first > gold`` is strict, so an equal-overlap hit is not called "closer".

    The published split leans on this: in the in-pool group the two are near-equal, and a
    non-strict comparison would report most of that group as "the first hit is closer",
    which would invert the reading.
    """
    rows = [_row("Q1", "ssh rebind", ["gold"], dense=["hit"])]
    titles = {"gold": "ssh rebind", "hit": "ssh rebind"}
    block = MODULE._title_overlap_breakdown(rows, "dense", titles)
    assert block["first_hit_title_closer"] == 0
    assert block["first_hit_title_closer_rate"] == 0.0
    assert block["mean_gap_gold_minus_first"] == 0.0


def test_title_overlap_skips_queries_whose_arm_returned_nothing() -> None:
    """An arm that returned no candidates has no first hit to compare against.

    Counting it as "the label won" would improve the rate by exactly the share of queries
    the arm failed to answer -- the opposite of what the number is for.
    """
    rows = [
        _row("Q1", "ssh rebind", ["gold"], dense=["gold"]),
        _row("Q2", "ssh rebind", ["gold"], dense=[]),
    ]
    titles = {"gold": "ssh rebind"}
    block = MODULE._title_overlap_breakdown(rows, "dense", titles)
    assert block["queries"] == 1


def test_title_overlap_reports_the_sign_of_the_gap() -> None:
    rows = [_row("Q1", "ssh cipher error", ["vague"], dense=["ssh cipher error"])]
    titles = {"vague": "release notice", "ssh cipher error": "ssh cipher error"}
    block = MODULE._title_overlap_breakdown(rows, "dense", titles)
    assert block["first_hit_title_closer"] == 1
    assert block["mean_gap_gold_minus_first"] < 0
    assert block["mean_question_vs_first_title"] > block["mean_question_vs_gold_title"]


def test_title_overlap_handles_a_document_the_titles_map_does_not_know() -> None:
    """A missing title must not be silently treated as a perfect match."""
    rows = [_row("Q1", "ssh rebind", ["absent"], dense=["absent"])]
    block = MODULE._title_overlap_breakdown(rows, "dense", {})
    assert block["mean_question_vs_gold_title"] == 0.0
    assert block["mean_question_vs_first_title"] == 0.0


# ------------------------------------------------------------------------ _examples


def _example_fixture() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Two positives of *different* size, plus one case of each kind that must be excluded.

    Two positives rather than one because a single positive cannot tell "sorted by
    advantage" from "sorted the other way" -- both orders return it first.
    """
    rows = [
        # The first hit is far more on-topic than the label: advantage 0.8.
        _row("CLEAR", "alcatel sam probe", ["nokia"], dense=["alcatel sam probe"]),
        # Also positive, but smaller: advantage 0.25.
        _row("SLIGHT", "ssh cipher error", ["gold2"], dense=["hit2"]),
        # The label and the first hit are equally on-topic: not an example.
        _row("TIE", "ssh rebind", ["gold"], dense=["hit"]),
        # The label is closer: the first hit is worse, which is the opposite finding.
        _row("REVERSE", "ssh rebind", ["ssh rebind precise"], dense=["unrelated"]),
        # The query's arm returned nothing: nothing to compare.
        _row("EMPTY", "ssh rebind", ["gold"], dense=[]),
    ]
    titles = {
        "nokia": "nokia probe release notice",
        "alcatel sam probe": "alcatel sam probe",
        "gold2": "ssh cipher error guide",
        "hit2": "ssh cipher error",
        "gold": "ssh rebind",
        "hit": "ssh rebind",
        "ssh rebind precise": "ssh rebind precise",
        "unrelated": "unrelated",
    }
    return rows, titles


def test_examples_are_ordered_by_the_first_hits_advantage() -> None:
    rows, titles = _example_fixture()
    examples = MODULE._examples(rows, titles)
    assert [item["query_id"] for item in examples] == ["CLEAR", "SLIGHT"]
    assert [item["title_overlap_advantage_of_the_first_hit"] for item in examples] == sorted(
        [item["title_overlap_advantage_of_the_first_hit"] for item in examples], reverse=True
    )
    assert all(item["title_overlap_advantage_of_the_first_hit"] > 0 for item in examples)
    # The cut takes the largest, so reversing the sort changes which one survives it.
    assert [item["query_id"] for item in MODULE._examples(rows, titles, limit=1)] == ["CLEAR"]


def test_examples_exclude_queries_where_the_label_is_the_closer_one() -> None:
    """The list may only contain cases that support the claim it is used to support.

    Including a query where the label is closer would let the selection be read as "these
    are the worst 10" while half of them argue the other way.
    """
    rows, titles = _example_fixture()
    ids = {item["query_id"] for item in MODULE._examples(rows, titles, limit=10)}
    assert "REVERSE" not in ids
    assert "TIE" not in ids
    assert "EMPTY" not in ids


def test_examples_honour_the_limit() -> None:
    rows = [
        _row(f"Q{i}", "alcatel sam probe", ["nokia"], dense=["alcatel sam probe"])
        for i in range(25)
    ]
    _, titles = _example_fixture()
    assert len(MODULE._examples(rows, titles, limit=10)) == 10
    assert len(MODULE._examples(rows, titles, limit=3)) == 3


def test_examples_report_pool_membership_from_the_deciding_arm() -> None:
    """``gold_in_the_100_deep_pool`` must come from ``production_blend``, not from dense.

    This is the field that ties each hand-readable example back to the ceiling count; if it
    read the dense arm the examples would claim a gold was out of the pool when the pool
    being measured never saw it. The gold therefore sits *deeper* in ``dense`` than the
    first hit -- present, but not as the answer to the question -- so the two arms disagree
    and the flag has to say which one it read.
    """
    rows = [
        _row(
            "OUT",
            "alcatel sam probe",
            ["nokia"],
            dense=["alcatel sam probe", "nokia"],
            production_blend=["something else"],
        )
    ]
    _, titles = _example_fixture()
    examples = MODULE._examples(rows, titles)
    assert examples[0]["first_dense_hit"]["file"] == "alcatel sam probe"
    assert examples[0]["gold_in_the_100_deep_pool"] is False


def test_examples_collapse_whitespace_in_the_question() -> None:
    rows = [_row("Q1", "alcatel   sam\n probe", ["nokia"], dense=["alcatel sam probe"])]
    _, titles = _example_fixture()
    assert MODULE._examples(rows, titles)[0]["question"] == "alcatel sam probe"


def test_examples_name_both_documents_and_their_titles() -> None:
    rows, titles = _example_fixture()
    example = MODULE._examples(rows, titles)[0]
    assert example["labelled"] == {"file": "nokia", "title": titles["nokia"]}
    assert example["first_dense_hit"] == {
        "file": "alcatel sam probe",
        "title": titles["alcatel sam probe"],
    }


# ------------------------------------------------------------------- _corpus_titles


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return buffer.getvalue()


TECHTQA_DOC = """Title: IBM Netcool Probe for Alcatel-Lucent 5620 SAM
Document Type: Technote
Abstract: Probe support
Text: The body of the document mentions Title: inside it, which must not be read.
"""


def test_corpus_titles_reads_the_header_block_and_not_the_body(tmp_path, monkeypatch) -> None:
    """The split is on the first ``Text:``, so the body never reaches the comparison.

    The header keeps its other fields (``Document Type``, ``Abstract``): they are part of
    the document's self-description and the real corpus puts them there, so the helper
    returns the block rather than trying to pick one field out of it. What matters for the
    finding is only that the body -- which is long, house-styled and flattens the topical
    signal -- is excluded, and that a ``Title:`` repeated inside the body does not move the
    split point.
    """
    archive = tmp_path / "corpus.zip"
    archive.write_bytes(_zip_bytes({"docs/swg1.txt": TECHTQA_DOC}))
    monkeypatch.setattr(MODULE, "CORPUS", archive)
    header = MODULE._corpus_titles()["swg1.txt"]
    assert header.startswith("IBM Netcool Probe for Alcatel-Lucent 5620 SAM")
    assert "Abstract: Probe support" in header
    assert "body of the document" not in header


def test_corpus_titles_keys_by_basename_and_ignores_non_text_entries(tmp_path, monkeypatch) -> None:
    archive = tmp_path / "corpus.zip"
    archive.write_bytes(
        _zip_bytes({"a/b/swg1.txt": TECHTQA_DOC, "a/b/index.json": '{"Title:": "no"}'})
    )
    monkeypatch.setattr(MODULE, "CORPUS", archive)
    assert set(MODULE._corpus_titles()) == {"swg1.txt"}
