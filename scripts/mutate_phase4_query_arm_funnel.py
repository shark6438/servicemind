"""Revert each decision the query-arm measurement rests on, and require the tests to notice.

The report answers one question -- which query arm the deployment runs, and how far the
published numbers are from it -- and it can answer it wrongly in two ways that both look
like a normal run:

* the replay falls back instead of failing, so the "production" row is a mixture of the
  production query arm and the deterministic one and the mixture is not reported;
* the cutoff identity is *stated* rather than recomputed, so the report reproduces the bug
  it exists to describe.

Neither raises. Both make every downstream number wrong in the same direction, which is
why they are the mutations here rather than the arithmetic.

Two of these are not hypothetical: the replay was keyed by the capture's query id, and
the parent store was the ingest-filled in-memory one. Neither could fail on the first arm
-- the deterministic arm runs first and never consults the replay, and the in-memory store
was filled by the ingest the first run happened to do -- so both survived a full run and
were caught only by running the reuse path against a kept index.

The gate's two halves are reverted separately: a ``--check`` that only compared input
digests would pass a report whose markdown no longer matches its JSON, and a ``--check``
that only compared markdown would pass one measured against a corpus that has since moved.
"""

import pathlib
from pathlib import Path

from mutation_harness import Mutation, run_mutations

ROOT = pathlib.Path(__file__).resolve().parent.parent
#: Stable across runs: an interrupted run journals under this name, and a rename would
#: orphan the journal at a path the next run never looks at.
EXPERIMENT = "phase4_query_arm_funnel"
SCRIPT = ROOT / "scripts/measure_phase4_production_query_arms.py"
TEST = "tests/servicemind/test_phase4_query_arm_funnel.py"
#: M19's ordering decision lives on the query model, not on this script: ``lexical_variants``
#: is what places the model's normalization in the fan-out, so a mutation that moves it has to
#: be written against that file.
KNOWLEDGE = ROOT / "src/servicemind/domain/knowledge.py"

IDENTITY_SATURATED = f"{TEST}::test_a_pack_shorter_than_every_cutoff_reports_the_identity"
IDENTITY_ABSENT = f"{TEST}::test_a_pack_with_room_reports_no_identity"
IDENTITY_PARTIAL = f"{TEST}::test_only_the_cutoffs_that_agree_are_reported"
STATS = f"{TEST}::test_the_pack_length_and_the_funnel_depth_are_reported_separately"
DISTRIBUTION = f"{TEST}::test_the_pack_statistics_describe_the_distribution_not_one_end_of_it"
REPLAY_IDENTITY = f"{TEST}::test_the_replay_hands_back_the_captured_object_itself"
REPLAY_KEYWORDS = f"{TEST}::test_the_replay_accepts_the_keywords_production_calls_it_with"
REPLAY_REFUSES = f"{TEST}::test_a_query_the_capture_missed_raises_instead_of_falling_back"
STRIPPED = f"{TEST}::test_stripping_the_rewrites_leaves_everything_else_in_place"
MARKDOWN_COLLAPSE = f"{TEST}::test_the_markdown_names_the_arms_whose_cutoffs_collapse"
MARKDOWN_NONE = f"{TEST}::test_the_markdown_says_so_when_no_cutoff_collapsed"
GATE_MISSING = f"{TEST}::test_a_missing_report_fails_the_gate"
GATE_STALE = f"{TEST}::test_an_input_that_moved_since_the_run_fails_the_gate"
GATE_MARKDOWN = f"{TEST}::test_a_markdown_that_no_longer_matches_the_json_fails_the_gate"
REPLAY_KEY = f"{TEST}::test_the_replay_is_keyed_by_the_question_and_not_by_the_capture_id"
REPLAY_ANSWER = f"{TEST}::test_the_replay_answers_the_question_that_was_asked"
PARENT_TENANT = f"{TEST}::test_the_parent_expansion_reads_the_index_under_the_asking_tenants_alias"
PARENT_ABSENT = f"{TEST}::test_a_parent_the_index_does_not_hold_is_absent_rather_than_empty"
PARENT_FRESH = f"{TEST}::test_the_substitute_does_not_claim_a_document_is_already_indexed"
PARENT_SOURCE = f"{TEST}::test_neither_script_fills_the_parent_store_by_ingesting"
GATE_PRODUCER = f"{TEST}::test_a_report_whose_producer_moved_fails_the_gate"
PROVENANCE = f"{TEST}::test_the_replay_carries_the_provenance_the_capture_recorded"
UNFLAGGED = f"{TEST}::test_a_capture_that_cannot_say_who_wrote_it_is_not_reused"
ANCHORED = f"{TEST}::test_the_anchor_arm_searches_the_text_the_user_typed"
ANCHOR_FRONT = (
    f"{TEST}::test_the_anchored_replay_puts_the_models_rewording_at_the_front_of_the_fan_out"
)
SUBSET = f"{TEST}::test_a_subset_of_arms_without_the_anchor_arm_is_refused"

MUTATIONS = [
    # ---- the cutoff identity is derived, not assumed -----------------------------------
    (
        "M01 the identity is never reported, which is the reading the report exists to "
        "replace: every committed TechQA row was taken on a pack that could not tell the "
        "cutoffs apart",
        SCRIPT,
        "            if distinct_recalls[left] == distinct_recalls[right]:",
        "            if False:",
        f"{IDENTITY_SATURATED} {MARKDOWN_COLLAPSE}",
    ),
    (
        "M02 every cutoff pair is reported as collapsed, so the flag stops distinguishing "
        "a pack that is short from a retriever that is flat",
        SCRIPT,
        "            if distinct_recalls[left] == distinct_recalls[right]:",
        "            if distinct_recalls[left] != distinct_recalls[right]:",
        f"{IDENTITY_ABSENT} {IDENTITY_PARTIAL} {MARKDOWN_NONE}",
    ),
    # ---- the replay replays ------------------------------------------------------------
    (
        "M03 the stripped replay keeps the rewrites, so the fan-out flag is no longer the "
        "only thing separating c0_sq from c0_mq",
        SCRIPT,
        '    rewrites = list(entry["rewritten_queries"]) if keep_rewrites else []',
        '    rewrites = list(entry["rewritten_queries"])',
        STRIPPED,
    ),
    (
        "M04 the replay rebuilds an equal-looking query instead of handing back the capture",
        SCRIPT,
        "            return self.mapping[query]",
        "            captured = self.mapping[query]\n"
        "            return KnowledgeQuery(\n"
        "                raw_query=query,\n"
        "                normalized_query=query,\n"
        "                intent=captured.intent,\n"
        "                language=captured.language,\n"
        "            )",
        f"{REPLAY_IDENTITY} {REPLAY_KEYWORDS} {REPLAY_REFUSES}",
    ),
    # ---- the funnel is reported as the funnel ------------------------------------------
    (
        "M05 the candidate depth is read off the packed list, so the report says 'how many "
        "documents were packed' twice and never how deep the funnel ran",
        SCRIPT,
        "    candidates = [outcome.candidate_count for outcome in answerable]",
        "    candidates = [len(outcome.ranked_keys) for outcome in answerable]",
        STATS,
    ),
    (
        "M06 the packed maximum is reported as the minimum, hiding every query that packed "
        "past the ceiling",
        SCRIPT,
        '            "max": max(packed) if packed else 0,',
        '            "max": min(packed) if packed else 0,',
        DISTRIBUTION,
    ),
    # ---- the gate's two halves ---------------------------------------------------------
    (
        "M07 the gate stops comparing the report's input digests, so it passes a report "
        "measured against a corpus that has since moved",
        SCRIPT,
        '            if payload["inputs"][key]["sha256"] != _digest(path):',
        "            if False:",
        GATE_STALE,
    ),
    (
        "M08 the gate stops comparing the markdown against the JSON it was rendered from",
        SCRIPT,
        '    markdown_matches = OUT_MD.read_text(encoding="utf-8") == render_markdown(payload)',
        "    markdown_matches = True",
        GATE_MARKDOWN,
    ),
    (
        "M09 a missing report is reported as a pass",
        SCRIPT,
        '            print("FAIL report missing")\n            return 1',
        '            print("FAIL report missing")\n            return 0',
        GATE_MISSING,
    ),
    # ---- the two faults a first run of this script actually had -------------------------
    (
        "M10 the replay is keyed by the capture's query id, which is what the harness never "
        "passes: every model-using arm raises KeyError, and only after the deterministic "
        "arm has already run",
        SCRIPT,
        "    return {\n"
        '        entry["query"]: _knowledge_query(\n'
        "            entry,\n"
        '            entry["query"],\n'
        "            keep_rewrites=keep_rewrites,\n"
        "            anchor_the_question=anchor_the_question,\n"
        "        )\n"
        '        for entry in capture["queries"].values()\n'
        "    }",
        "    return {\n"
        "        query_id: _knowledge_query(\n"
        "            entry,\n"
        '            entry["query"],\n'
        "            keep_rewrites=keep_rewrites,\n"
        "            anchor_the_question=anchor_the_question,\n"
        "        )\n"
        '        for query_id, entry in capture["queries"].items()\n'
        "    }",
        f"{REPLAY_KEY} {REPLAY_ANSWER}",
    ),
    (
        "M11 the parent store goes back to the ingest-filled in-memory stand-in, so a run "
        "that skips ingest drops every hit at parent expansion and reports recall 0 and "
        "packed 0 as if it had measured something",
        SCRIPT,
        "repository=KeptIndexRepository(index)",
        "repository=release.MemoryRepository()",
        PARENT_SOURCE,
    ),
    (
        "M12 the parent read is scoped to the corpus tenant instead of the asking tenant, "
        "which is the shared-alias read the production RLS clause exists to prevent",
        SCRIPT,
        "        found = await self.index.parents(principal.tenant_id, sorted(wanted))",
        "        found = await self.index.parents(TENANT, sorted(wanted))",
        PARENT_TENANT,
    ),
    (
        "M13 a parent the index does not hold comes back as an empty string, so a hit whose "
        "content nobody can read is packed as if it were readable",
        SCRIPT,
        "        return {key: value for key, value in found.items() if key in wanted}",
        '        return {key: found.get(key, "") for key in wanted}',
        PARENT_ABSENT,
    ),
    (
        "M14 the substitute claims every document is already indexed, so an ingest run "
        "skips the corpus and then measures whatever the cluster already held",
        SCRIPT,
        "    async def is_current(self, tenant_id: UUID, document: object) -> bool:\n        return False",
        "    async def is_current(self, tenant_id: UUID, document: object) -> bool:\n        return True",
        PARENT_FRESH,
    ),
    (
        "M15 the report stops binding its producer, so the committed numbers survive any "
        "edit to how they were measured",
        SCRIPT,
        '            ("producer", PRODUCER),\n            ("release_loader", RELEASE_SCRIPT),',
        '            ("release_loader", RELEASE_SCRIPT),\n            ("release_loader", RELEASE_SCRIPT),',
        GATE_PRODUCER,
    ),
    (
        "M16 the replay stops carrying the provenance the capture recorded, so the arm "
        "that exists to measure the model is relabelled as the fallback",
        SCRIPT,
        '        provenance=QueryProvenance(entry["provenance"]),',
        "        provenance=QueryProvenance.DETERMINISTIC,",
        PROVENANCE,
    ),
    (
        "M17 an unflagged capture is reused again, so a swallowed model failure is "
        "republished as the model's own normalisation",
        SCRIPT,
        '        isinstance(entry, dict) and "provenance" in entry and "model_normalized_query" in entry\n',
        '        isinstance(entry, dict) and "model_normalized_query" in entry\n',
        UNFLAGGED,
    ),
    (
        "M18 the anchor arm stops anchoring, so it re-runs the deployment arm and the "
        "table reports the same row twice",
        SCRIPT,
        "            normalized_query=deterministic,\n",
        "            normalized_query=model_normalization or deterministic,\n",
        ANCHORED,
    ),
    (
        "M19 the model's rewording is appended instead of prepended, so the "
        "normalisation is the variant the fan-out cap drops first",
        KNOWLEDGE,
        "        variants: list[str] = []\n"
        "        if self.model_normalized_query is not None and (\n"
        "            self.model_normalized_query.casefold() != self.normalized_query.casefold()\n"
        "        ):\n"
        "            variants.append(self.model_normalized_query)\n"
        "        variants.extend(self.rewritten_queries)\n",
        "        variants: list[str] = []\n"
        "        variants.extend(self.rewritten_queries)\n"
        "        if self.model_normalized_query is not None and (\n"
        "            self.model_normalized_query.casefold() != self.normalized_query.casefold()\n"
        "        ):\n"
        "            variants.append(self.model_normalized_query)\n",
        ANCHOR_FRONT,
    ),
    (
        "M20 a subset that omits the fidelity anchor is accepted, so the table has a row "
        "read against nothing",
        SCRIPT,
        '    if "c0_off" not in names:',
        "    if False:",
        SUBSET,
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
