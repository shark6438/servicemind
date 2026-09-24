"""The corpus-side group isolation, run as a controlled experiment instead of read off a run.

The requirement this serves is the one the user stated on 2026-09-22: after a requester's
group is taken away, the run must not be able to see that group's documents any more. The
obvious way to check it is to look at what a live case's run cited and assert the
restricted document is absent -- and that way does not work here. Measured over the runs
of this acceptance, a Globex analyst holding group 3 while nothing is revoked cites
``KB-GLOBEX-VPN-MFA-G3`` in about half of them: which documents a run ends up citing
depends on the query the knowledge agent writes, on how the reranker scored the batch that
came back, and on whether the analysis kept the row. An assertion that the document is
missing would then pass, roughly half the time, on a platform where the narrowing did
nothing at all -- the failure mode the whole exercise exists to avoid, dressed as a pass.

So the narrowing is measured where it is deterministic. Retrieval takes a principal and a
query and returns rows; the ACL is a pre-filter inside the search index, applied from the
principal alone and independent of rerank and packing. Holding the query fixed and varying
only ``group_ids`` turns the question into two readings of one corpus, and the difference
between them can only be the group axis -- which is the same shape as the graph probe next
door, for the same reason.

Two documents, not one:

- the restricted runbook, which the principal holding group 3 must reach;
- a public document, which *both* principals must reach.

The second is not decoration. "The restricted document was not returned" is also true of a
retrieval that returned nothing at all -- an index that was never populated, a tenant that
was filtered out, an embedding service that is down. Requiring the public document of both
readings is what separates a filtered result from an empty one, and without it the probe
would report a green narrowing over a corpus it never actually read.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from servicemind.domain.evidence import CITATION_KEY, Evidence
from servicemind.domain.knowledge import RetrievalPrincipal
from servicemind.rag.service import EnterpriseRAG

#: The Globex acceptance corpus' restricted documents and the group each one names.
#:
#: Mirrors ``evaluation/acceptance/fixtures/globex/manifest.json``, which is what the
#: seeding script applies and what the seed-time check reads. It is repeated here rather
#: than imported because the declaration the probe is tested against must not be the same
#: object as the one under test: a manifest that lost its group restriction would
#: otherwise move both sides of the comparison together and the probe would keep passing
#: over a corpus with no restricted documents in it.
#: ``test_the_knowledge_probe_agrees_with_the_fixture_manifest`` holds the two together.
CORPUS_RESTRICTED_BY_GROUP: dict[str, int] = {
    "KB-GLOBEX-VPN-MFA-G3": 3,
}

#: A document the acceptance corpus leaves unrestricted, read by both principals.
#:
#: The current remedy handbook, and not an arbitrary choice: it is the document this
#: question is *about*, so it is the one most likely to survive reranking whatever the
#: corpus looks like -- which is what a control row has to be for the comparison above it
#: to mean anything.
PUBLIC_CONTROL_DOCUMENT = "KB-GLOBEX-VPN-MFA-REBIND"

#: How many rows the probe asks for. Wider than the production ``final_k`` of 8, on
#: purpose: the probe is asking "can this principal reach this document at all", not "did
#: this document make the top eight". A narrower reading would fail for a group that holds
#: the document but ranks it ninth, and the isolation claim would be reported as broken
#: when the ranking had merely moved.
PROBE_FINAL_K = 20


def acceptance_ids_in_evidence(evidence: Iterable[Evidence]) -> list[str]:
    """Which acceptance fixture documents a set of evidence rows names.

    Read off the citation the row carries, by exact ``source_record_id`` and not by
    substring: a title or a URI containing ``KB-GLOBEX-VPN-MFA-G3`` is not the document,
    and treating it as one is how a citation check passes over the wrong row.
    """
    wanted = set(CORPUS_RESTRICTED_BY_GROUP) | {PUBLIC_CONTROL_DOCUMENT}
    found: list[str] = []
    for row in evidence:
        citation = row.metadata.get(CITATION_KEY)
        if not isinstance(citation, dict):
            continue
        source_record_id = citation.get("source_record_id")
        # ``in`` on a set of strings would raise on an unhashable value and match nothing
        # on a missing one, and a citation row is data from the index, not a typed object.
        if isinstance(source_record_id, str) and source_record_id in wanted:
            if source_record_id not in found:
                found.append(source_record_id)
    return sorted(found)


def principal_for(
    tenant_id: UUID,
    *,
    user_id: str,
    entity_ids: set[int],
    group_ids: set[int],
) -> RetrievalPrincipal:
    """A principal shaped like the ones the platform builds from a token."""
    return RetrievalPrincipal(
        tenant_id=tenant_id,
        user_id=user_id,
        entity_ids=frozenset(entity_ids),
        group_ids=frozenset(group_ids),
    )


async def corpus_scope_reading(
    rag: EnterpriseRAG,
    principal: RetrievalPrincipal,
    *,
    query: str,
) -> list[str]:
    """Read the acceptance corpus through the production RAG as this principal.

    ``use_query_model=False`` and no rewrites, and that is a deliberate limit on what this
    probe claims. The production path lets a model rewrite the question before the search,
    and a rewrite is not reproducible enough to hang an isolation assertion on -- two arms
    of the same query is not a controlled comparison. Pinning the text makes it one: same
    corpus, same retriever, same query, one principal per reading, one field changed
    between them. It does not claim to reproduce any particular run's query.
    """
    result = await rag.retrieve(
        principal=principal,
        query=query,
        use_query_model=False,
        final_k=PROBE_FINAL_K,
    )
    return acceptance_ids_in_evidence(rag.to_evidence(principal.tenant_id, result))


async def corpus_scope_problems(
    rag: EnterpriseRAG,
    tenant_id: UUID,
    *,
    entity_id: int,
    query: str,
) -> list[str]:
    """Both readings of the corpus scope, and every way they can fail to be informative.

    Run at seed time as well as at case time: a corpus that has drifted into a state where
    the two principals read alike -- or a deployment whose index is empty -- would
    otherwise be reported by the next acceptance run as a platform failure.
    """
    problems: list[str] = []
    for source_record_id, group_id in CORPUS_RESTRICTED_BY_GROUP.items():
        held = await corpus_scope_reading(
            rag,
            principal_for(
                tenant_id,
                user_id=f"acceptance-corpus-probe-g{group_id}",
                entity_ids={entity_id},
                group_ids={group_id},
            ),
            query=query,
        )
        withheld = await corpus_scope_reading(
            rag,
            principal_for(
                tenant_id,
                user_id="acceptance-corpus-probe-none",
                entity_ids={entity_id},
                group_ids=set(),
            ),
            query=query,
        )
        if PUBLIC_CONTROL_DOCUMENT not in held or PUBLIC_CONTROL_DOCUMENT not in withheld:
            problems.append(
                f"the corpus probe did not read {PUBLIC_CONTROL_DOCUMENT}, which is "
                f"unrestricted, as one of the two principals (holding group {group_id}: "
                f"{held}; holding none: {withheld}). The corpus was not read, so "
                "'the restricted document was not returned' says nothing about the ACL"
            )
            continue
        if source_record_id not in held:
            problems.append(
                f"the principal holding group {group_id} cannot reach {source_record_id}, "
                f"which names group {group_id}: the group axis of the corpus ACL is not "
                f"letting a permitted reader through (saw {held})"
            )
        if source_record_id in withheld:
            problems.append(
                f"the principal holding no group reached {source_record_id}, which names "
                f"group {group_id}: the group axis of the corpus ACL is not filtering. A "
                f"revoked grant would not take this document away (saw {withheld})"
            )
    return problems
