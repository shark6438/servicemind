"""The corpus shape the §4.1 gate is about, and the loader that refuses to invent it.

The gate's clauses are about strata -- a second tenant, group-restricted documents, a
withdrawn procedure, both ends of an effective window, hard negatives, three kinds of
unanswerable query. Every corpus in this repository before now had none of them, so the
clauses could not be satisfied by any run, correct or not. These tests pin the fixture that
has them and the three ways the loader fails closed on a manifest that does not say what it
means.

The last test is the one that matters: it drives the §4.1 audit over *this* corpus's
coordinates and shows the count both staying at zero for the asker who holds the group and
leaving it for the asker who does not. A corpus with strata is only worth having if the
check it exists to serve can actually come out either way on it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from servicemind.evaluation.gold import LabelTier, QueryCategory, RefusalReason, Split
from servicemind.evaluation.leakage import LeakKind, visible_evidence_violations
from servicemind.evaluation.tenant_domain import (
    TENANTS,
    TenantDomainError,
    TenantDomainFixture,
    load_fixture,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "evaluation" / "gold" / "tenant-domain-release"
ACCEPTANCE_GLOBEX = ROOT / "evaluation" / "acceptance" / "fixtures" / "globex"
PROBE_SCRIPT = ROOT / "scripts" / "evaluate_tenant_domain_release.py"
ACME = TENANTS["acme"]
GLOBEX = TENANTS["globex"]


def _probe_script():
    spec = importlib.util.spec_from_file_location("tenant_domain_probe", PROBE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass/model resolution goes through sys.modules during exec.
    sys.modules["tenant_domain_probe"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def release_fixture() -> TenantDomainFixture:
    return TenantDomainFixture(FIXTURE)


def _write_fixture(tmp_path: Path, documents: dict[str, str], manifest: dict, qrels: dict):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name, body in documents.items():
        (corpus / name).write_text(body, encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "qrels.json").write_text(json.dumps(qrels), encoding="utf-8")
    return tmp_path


def _declaration(file: str, record: str, **extra: object) -> dict:
    return {"file": file, "source_record_id": record, **extra}


def _minimal_qrels(record: str) -> dict:
    return {
        "name": "tmp",
        "queries": [
            {
                "id": "q",
                "query": "what is the procedure",
                "relevant": [record],
                "graded": [
                    {
                        "parent_chunk_id": f"{record}#document",
                        "source_record_id": record,
                        "grade": 4,
                    }
                ],
            }
        ],
    }


# --- the fixture is the shape the gate is about ---------------------------------------


def test_the_fixture_spans_every_stratum_the_gate_names(release_fixture) -> None:
    """Each clause of the gate has something in this corpus to be about.

    Reported as one assertion over the whole shape because that is what makes the set
    usable: any one of these at zero and the corresponding clause is satisfied vacuously,
    which is how the committed corpora satisfied all of them.
    """
    strata = release_fixture.strata()
    assert strata["tenants"] == 2, "a wrong-tenant count needs a second tenant to be wrong about"
    assert strata["group_restricted"] >= 2, "the wrong-group count needs a restricted document"
    assert strata["inactive"] >= 1, "the expired-version count needs a withdrawn document"
    assert strata["with_an_effective_end"] >= 1, "one end of the clock check"
    assert strata["not_yet_in_force"] >= 1, "the other end of the clock check"


def test_the_fixture_carries_all_four_query_strata(release_fixture) -> None:
    gold = release_fixture.gold_set()
    for category in QueryCategory:
        assert gold.by_category(category), f"no query in the {category.value} stratum"
    assert gold.by_split(Split.DEVELOPMENT), "nothing could have been tuned without the test half"


def test_the_fixture_carries_all_three_refusal_reasons(release_fixture) -> None:
    """Three kinds of impossible query, because they fail apart and score apart.

    One abstention rate over a single kind cannot tell a version conflict from a disclosure
    refusal, and the two have nothing in common to fix.
    """
    gold = release_fixture.gold_set()
    reasons = {query.refusal_reason for query in gold.unanswerable}
    assert reasons == set(RefusalReason)


def test_human_signoff_is_the_only_thing_standing_between_this_and_the_gate(
    release_fixture,
) -> None:
    """The fixture's shape is complete, so its blockers name exactly one unmet clause.

    This is the discriminating property of a data-derived applicability check. A hardcoded
    ``applicable: false`` gives the same answer for this set and for the TechQA silver set;
    here the answer moves to one line that names the one thing a fixture cannot supply.
    Anything else appearing in this list means the fixture stopped covering a stratum the
    gate asks about -- which is why the assertion is on the whole list and not on whether
    it is empty.
    """
    blockers = release_fixture.gold_set().release_gate_blockers
    assert len(blockers) == 1, blockers
    assert "kappa" in blockers[0]
    assert "two domain annotators" in blockers[0]


def test_the_fixture_records_itself_as_synthetic(release_fixture) -> None:
    """A fixture must not be able to claim the annotation protocol.

    If it could, the release gate would unlock on labels nobody read -- and it would unlock
    silently, because the thing that would notice is the same field that was forged.
    """
    provenance = release_fixture.gold_set().provenance
    assert provenance is not None
    assert provenance.label_tier is LabelTier.SILVER
    assert not provenance.meets_tenant_protocol


def test_the_loader_reads_the_existing_acceptance_fixture_unchanged() -> None:
    """The manifest format is extended, not replaced.

    ``evaluation/acceptance/fixtures/globex/manifest.json`` already declares groups and the
    retirement flag, and already documents why each restriction is what it is. A second
    format for the same statement would let the two disagree, and the disagreement would be
    invisible because each loader would be self-consistent.
    """
    fixture = TenantDomainFixture(ACCEPTANCE_GLOBEX)
    documents = fixture.documents()
    assert {d.provenance.source_record_id for d in documents} == {
        "KB-GLOBEX-VPN-MFA-REBIND",
        "KB-GLOBEX-VPN-MFA-G3",
        "KB-GLOBEX-VPN-MFA-G4",
        "KB-GLOBEX-VPN-MFA-LEGACY",
        "KB-GLOBEX-VPN-APP-REG",
        "KB-GLOBEX-VPN-MFA-AUDIT",
    }
    strata = fixture.strata()
    assert strata["tenants"] == 1
    assert strata["group_restricted"] == 3
    assert strata["inactive"] == 1


# --- the loader fails closed ----------------------------------------------------------


def test_a_document_with_no_declaration_is_an_error(tmp_path: Path) -> None:
    """The convenient default here is the dangerous one.

    Treating an undeclared file as tenant-public indexes it, makes it retrievable, and
    reports it as correctly restricted -- because nothing ever said it was restricted. The
    run then reads as evidence that the ACL filter works on a corpus where the file it
    failed to restrict was never subject to it.
    """
    root = _write_fixture(
        tmp_path,
        {"declared.md": "# Declared\n\nbody\n", "undeclared.md": "# Undeclared\n\nbody\n"},
        {
            "tenant_id": str(ACME),
            "documents": [_declaration("declared.md", "KB-DECLARED")],
        },
        _minimal_qrels("KB-DECLARED"),
    )
    with pytest.raises(TenantDomainError, match="no manifest entry"):
        TenantDomainFixture(root).documents()


def test_a_declaration_with_no_document_is_an_error(tmp_path: Path) -> None:
    root = _write_fixture(
        tmp_path,
        {"present.md": "# Present\n\nbody\n"},
        {
            "tenant_id": str(ACME),
            "documents": [
                _declaration("present.md", "KB-PRESENT"),
                _declaration("renamed-away.md", "KB-GONE"),
            ],
        },
        _minimal_qrels("KB-PRESENT"),
    )
    with pytest.raises(TenantDomainError, match="declares documents that do not exist"):
        TenantDomainFixture(root).documents()


def test_a_judgment_about_a_document_the_corpus_lacks_is_an_error(tmp_path: Path) -> None:
    """Recall over a key that was never indexed measures a join, not a retrieval."""
    root = _write_fixture(
        tmp_path,
        {"present.md": "# Present\n\nbody\n"},
        {"tenant_id": str(ACME), "documents": [_declaration("present.md", "KB-PRESENT")]},
        _minimal_qrels("KB-ABSENT"),
    )
    with pytest.raises(TenantDomainError, match="does not contain"):
        TenantDomainFixture(root).gold_set()


def test_a_chunk_level_judgment_is_an_error(tmp_path: Path) -> None:
    """A fixture cannot know a chunk id, so it must not be able to pretend it does.

    ``parent_chunk_id`` is minted by the chunker at index time and changes with the
    chunking configuration. A manifest that pinned one would silently stop matching the
    next time the chunker changed, and the judgments would go on being reported as if they
    were being applied.
    """
    record = "KB-PRESENT"
    qrels = _minimal_qrels(record)
    qrels["queries"][0]["graded"][0]["parent_chunk_id"] = "3f2b1a4c-0000-4000-8000-000000000000"
    root = _write_fixture(
        tmp_path,
        {"present.md": "# Present\n\nbody\n"},
        {"tenant_id": str(ACME), "documents": [_declaration("present.md", record)]},
        qrels,
    )
    with pytest.raises(TenantDomainError, match="chunk id this fixture can know"):
        TenantDomainFixture(root).gold_set()


def test_an_unknown_authority_level_is_an_error(tmp_path: Path) -> None:
    root = _write_fixture(
        tmp_path,
        {"present.md": "# Present\n\nbody\n"},
        {
            "tenant_id": str(ACME),
            "documents": [_declaration("present.md", "KB-PRESENT", authority="authoritative")],
        },
        _minimal_qrels("KB-PRESENT"),
    )
    with pytest.raises(TenantDomainError, match="unknown authority level"):
        TenantDomainFixture(root).documents()


def test_a_tenant_that_is_neither_an_alias_nor_a_uuid_is_an_error(tmp_path: Path) -> None:
    """A typo must not create a tenant that exists only inside the fixture.

    If it did, the wrong-tenant count would be comparing a real tenant against a
    misspelling, and every document under it would read as correctly isolated from
    everyone.
    """
    root = _write_fixture(
        tmp_path,
        {"present.md": "# Present\n\nbody\n"},
        {
            "tenant_id": str(ACME),
            "documents": [_declaration("present.md", "KB-PRESENT", tenant="globx")],
        },
        _minimal_qrels("KB-PRESENT"),
    )
    with pytest.raises(TenantDomainError, match="neither a known alias"):
        TenantDomainFixture(root).documents()


def test_a_window_that_never_opens_is_an_error(tmp_path: Path) -> None:
    """A closed window is not a clock stratum, it is a document that is never in force."""
    root = _write_fixture(
        tmp_path,
        {"present.md": "# Present\n\nbody\n"},
        {
            "tenant_id": str(ACME),
            "documents": [
                _declaration(
                    "present.md",
                    "KB-PRESENT",
                    effective_from="2026-03-01T00:00:00+00:00",
                    effective_to="2026-03-01T00:00:00+00:00",
                )
            ],
        },
        _minimal_qrels("KB-PRESENT"),
    )
    with pytest.raises(TenantDomainError, match="is never in force"):
        TenantDomainFixture(root).documents()


# --- the coordinates are the corpus's, and the audit bites on them ---------------------


def test_the_coordinates_are_read_off_the_documents_not_re_parsed(release_fixture) -> None:
    """One parse, so the index and the audit cannot disagree by construction."""
    coordinates = release_fixture.coordinates()
    for document in release_fixture.documents():
        item = coordinates[document.provenance.source_record_id]
        assert item.tenant_id == document.acl.tenant_id
        assert item.group_ids == document.acl.group_ids
        assert item.is_active == document.acl.is_active
        assert item.effective_from == document.acl.effective_from
        assert item.effective_to == document.acl.effective_to
    assert len(coordinates) == len(release_fixture.documents())


def test_load_fixture_returns_a_matched_triple(release_fixture) -> None:
    documents, gold, coordinates = load_fixture(FIXTURE)
    assert len(documents) == len(coordinates)
    assert {d.provenance.source_record_id for d in documents} == set(coordinates)
    assert {q.id for q in gold.queries} == {q.id for q in release_fixture.gold_set().queries}


def test_the_audit_comes_out_both_ways_on_this_corpus(release_fixture) -> None:
    """The §4.1 count is not structurally zero here, which is the whole point of the fixture.

    The same document, retrieved for two askers: one holds the group it is restricted to and
    the retrieval is clean, the other holds nothing and the same retrieval is a disclosure.
    A corpus without a group-restricted document cannot distinguish those two runs, and
    would report zero for both.
    """
    coordinates = release_fixture.coordinates()
    restricted = "KB-ACME-VPN-MFA-G3"
    assert coordinates[restricted].group_ids == frozenset({3})

    def audit(group_ids: frozenset[int]):
        return visible_evidence_violations(
            query_id="td-breakglass-group3",
            retrieved=[restricted],
            coordinates=coordinates,
            asker_tenant_id=ACME,
            asker_group_ids=group_ids,
            query_time=release_fixture.query_time,
        )

    assert audit(frozenset({3})) == []
    assert [v.kind for v in audit(frozenset())] == [LeakKind.UNAUTHORIZED_GROUP]
    assert [v.kind for v in audit(frozenset({4}))] == [LeakKind.UNAUTHORIZED_GROUP]


def test_the_fixture_clock_is_load_bearing(release_fixture) -> None:
    """The same corpus, audited at two instants, must give two different answers.

    ``RetrievalPrincipal`` and ``EvidenceCoordinates`` both take a query time, and both
    default it to the moment they are constructed. A probe that lets them default is asking
    at *today's* date while the fixture was authored at its own, and on this corpus that
    silently collapses the clock strata: the not-yet-in-force document becomes an ordinary
    in-force one, the audit reports no violation, and the run reads as evidence that the
    window filter works. The first version of the probe script did exactly that and reported
    ``surfaced: true, violations: 0`` -- both true, about two different instants.

    Pinned here rather than in the script because the mistake is in how the *fixture* is
    read, and the next reader is as likely to make it.
    """
    coordinates = release_fixture.coordinates()
    query_time = release_fixture.query_time
    not_yet_in_force = "KB-ACME-VPN-MFA-Q3"

    def audit(at: datetime):
        return visible_evidence_violations(
            query_id="q",
            retrieved=[not_yet_in_force],
            coordinates=coordinates,
            asker_tenant_id=ACME,
            asker_group_ids=frozenset(),
            query_time=at,
        )

    assert [v.kind for v in audit(query_time)] == [LeakKind.EXPIRED_VERSION]
    assert audit(datetime.now(UTC)) == [], (
        "the document is in force today, which is why the fixture has to be asked at its "
        "own clock for the stratum to mean anything"
    )


def test_every_probe_caller_is_actually_a_different_caller(release_fixture) -> None:
    """A control that is not a different principal controls for nothing.

    Each probe's denied and allowed sides must differ on the axis the probe is about: the
    group probes on groups, the tenant probe on tenant. A pair that differed on both, or on
    neither, would let a filter that ignores the axis entirely pass the probe.
    """
    for entry in _probe_script().PROBES:
        denied, allowed = entry["denied"], entry["allowed"]
        assert denied is not None
        if allowed is None:
            continue
        same_tenant = denied["tenant"] == allowed["tenant"]
        same_groups = denied["groups"] == allowed["groups"]
        assert same_tenant != same_groups, (
            f"{entry['document']}: the two sides differ on "
            f"{'both axes (so neither is isolated)' if (not same_tenant and not same_groups) else 'neither axis (so the probe is not a probe)'}"
        )


def test_the_audit_catches_the_other_three_strata_on_this_corpus(release_fixture) -> None:
    """Tenant, retirement flag and both clock ends, each coming out as its own kind."""
    coordinates = release_fixture.coordinates()

    def audit(key: str):
        return visible_evidence_violations(
            query_id="q",
            retrieved=[key],
            coordinates=coordinates,
            asker_tenant_id=GLOBEX,
            asker_group_ids=frozenset(),
            query_time=release_fixture.query_time,
        )

    assert [v.kind for v in audit("KB-ACME-VPN-MFA-REBIND")] == [LeakKind.WRONG_TENANT]

    def audit_as_acme(key: str):
        return visible_evidence_violations(
            query_id="q",
            retrieved=[key],
            coordinates=coordinates,
            asker_tenant_id=ACME,
            asker_group_ids=frozenset(),
            query_time=release_fixture.query_time,
        )

    assert [v.kind for v in audit_as_acme("KB-ACME-VPN-MFA-LEGACY")] == [LeakKind.EXPIRED_VERSION]
    assert [v.kind for v in audit_as_acme("KB-ACME-VPN-MFA-V1")] == [LeakKind.EXPIRED_VERSION]
    assert [v.kind for v in audit_as_acme("KB-ACME-VPN-MFA-Q3")] == [LeakKind.EXPIRED_VERSION]
    # And the in-force counterpart is clean, so the two clock checks are not just firing on
    # every document in the corpus.
    assert audit_as_acme("KB-ACME-VPN-MFA-V2") == []
    assert audit_as_acme("KB-ACME-VPN-MFA-REBIND") == []
