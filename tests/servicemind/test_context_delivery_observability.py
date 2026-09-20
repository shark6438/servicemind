from datetime import UTC, datetime

from servicemind.observability.context_delivery import (
    DeliveryStatus,
    build_context_delivery_report,
)


def selection(source: str, decision: str, reason: str, tokens: int) -> dict[str, object]:
    return {
        "item_id": f"{source}:1",
        "source": source,
        "content_hash": "a" * 64,
        "tokens": tokens,
        "decision": decision,
        "reason": reason,
    }


NOW = datetime(2026, 9, 16, tzinfo=UTC)


def test_no_production_artifacts_is_not_a_pass() -> None:
    report = build_context_delivery_report(
        [],
        excluded_artifacts=7,
        excluded_user_ids=("phase5-live-verifier",),
        generated_at=NOW,
    )
    assert report.status is DeliveryStatus.NO_DATA
    assert report.memory_starvation_rate is None
    assert report.excluded_artifacts == 7
    assert report.excluded_user_ids == ("phase5-live-verifier",)


def test_no_memory_candidates_is_inconclusive_instead_of_green() -> None:
    artifacts = [
        {"selection_manifest": [selection("evidence", "selected", "ranked_within_budget", 200)]}
    ]
    report = build_context_delivery_report(
        artifacts,
        min_analysis_envelopes=1,
        min_memory_candidate_envelopes=1,
        generated_at=NOW,
    )
    assert report.status is DeliveryStatus.INSUFFICIENT_MEMORY_DATA


def test_observed_cap_and_memory_delivery_pass() -> None:
    artifacts = [
        {
            "selection_manifest": [
                selection("evidence", "selected", "ranked_within_budget", 4800),
                selection("evidence", "pruned", "source_token_cap_exceeded", 400),
                selection("memory", "selected", "ranked_within_budget", 401),
            ]
        }
    ]
    report = build_context_delivery_report(
        artifacts,
        min_analysis_envelopes=1,
        min_memory_candidate_envelopes=1,
        generated_at=NOW,
    )
    assert report.status is DeliveryStatus.PASS
    assert report.evidence_cap_pruned_envelopes == 1
    assert report.memory_starvation_rate == 0
    assert report.memory_selected_tokens is not None
    assert report.memory_selected_tokens.maximum == 401


def test_retrieved_but_fully_pruned_memory_fails_the_gate() -> None:
    artifacts = [
        {
            "selection_manifest": [
                selection("evidence", "selected", "ranked_within_budget", 5000),
                selection("memory", "pruned", "token_budget_exceeded", 6000),
            ]
        }
    ]
    report = build_context_delivery_report(
        artifacts,
        min_analysis_envelopes=1,
        min_memory_candidate_envelopes=1,
        generated_at=NOW,
    )
    assert report.status is DeliveryStatus.FAIL
    assert report.memory_starved_envelopes == 1
    assert report.gate.violations == ("memory_starvation_rate_exceeded:1.000000>0.000000",)


def _all_keys(node: object) -> set[str]:
    if isinstance(node, dict):
        keys: set[str] = set(node)
        for value in node.values():
            keys |= _all_keys(value)
        return keys
    if isinstance(node, list):
        keys = set()
        for element in node:
            keys |= _all_keys(element)
        return keys
    return set()


#: Field names that would turn an aggregate report back into a pointer at the
#: customer's data. Matched as substrings so a decorated name still trips it.
SENSITIVE_KEY_FRAGMENTS = (
    "content",
    "hash",
    "provenance",
    "item_id",
    "memory_id",
    "prompt",
    "text",
    "ticket",
)


def test_the_report_carries_no_item_id_content_hash_or_provenance() -> None:
    """Being safe to store beside the manifests is the whole point of this report.

    A memory entry's ``item_id`` embeds its ``memory_id``, and ``content_hash`` is a
    stable fingerprint of the stored text: exporting either degrades an aggregate into
    a link back to tenant data, and nothing in the numbers would look wrong. So the
    property is asserted structurally -- a sentinel placed in the manifest must not
    survive into the serialized report, and no field may even be named like something
    that could carry one.
    """
    sentinel_id = "memory:SENTINEL-MEMORY-ID-7f3a"
    sentinel_hash = "deadbeef" * 8
    report = build_context_delivery_report(
        [
            {
                "selection_manifest": [
                    {
                        "item_id": sentinel_id,
                        "source": "memory",
                        "content_hash": "a" * 64,
                        "tokens": 401,
                        "decision": "selected",
                        "reason": "ranked_within_budget",
                    },
                    {
                        "item_id": "evidence:9",
                        "source": "evidence",
                        "content_hash": sentinel_hash,
                        "tokens": 4800,
                        "decision": "selected",
                        "reason": "ranked_within_budget",
                    },
                ]
            }
        ],
        min_analysis_envelopes=1,
        min_memory_candidate_envelopes=1,
        generated_at=NOW,
    )
    assert report.status is DeliveryStatus.PASS

    payload = report.model_dump_json()
    assert sentinel_id not in payload
    assert sentinel_hash not in payload
    for key in _all_keys(report.model_dump(mode="json")):
        offenders = [fragment for fragment in SENSITIVE_KEY_FRAGMENTS if fragment in key]
        assert not offenders, f"{key} names {offenders}"


def test_malformed_manifest_fails_closed() -> None:
    report = build_context_delivery_report(
        [{"selection_manifest": [{"source": "memory"}]}],
        min_analysis_envelopes=1,
        min_memory_candidate_envelopes=1,
        generated_at=NOW,
    )
    assert report.status is DeliveryStatus.FAIL
    assert report.gate.violations == ("malformed_context_artifacts:1",)
