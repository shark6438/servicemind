"""Which memories the candidate window admits, and whether both implementations agree.

``MemoryRetriever`` reranks, but it reranks a window: ``candidates(query,
ceiling=...)`` decides what the semantic ranker is even allowed to see. A memory that
shares part of the query and is older than the ceiling's worth of newer memories is
therefore either recalled or lost before any relevance model runs.

The two implementations used to answer that cut differently. The in-memory one scores
each record by token overlap with the query, so a partial match scores above a record
that shares nothing. The Postgres one ordered by ``ts_rank_cd(content,
plainto_tsquery(query))`` -- and ``plainto_tsquery`` joins its terms with AND, so every
record that does not contain *all* of the query's terms ranked 0. The window then
collapsed to a recency cut with an empty lexical signal in it, while the deployment
reported the semantic channel as enabled.

These tests run one scenario against both repositories: a partial match written first,
then more distractors than the window can hold, each sharing nothing with the query. The
partial match must be admitted. The in-memory reference already satisfied this, so the
scenario is what makes the Postgres divergence visible -- and it is why the fix belongs
in the SQL rather than in a test.

The tests at the end cover the other half: *which* window gets asked for in the first
place. The repository orders candidates by lexeme overlap; when the ranker is cosine
similarity that ordering cannot stand in for the ranking, so the semantic path must not
let it decide who gets scored.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from servicemind.memory.contracts import (
    MemoryCandidate,
    MemoryEvidenceRef,
    MemoryQuery,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    MemoryWriteAction,
    MemoryWriteDecision,
    SemanticSubtype,
)
from servicemind.memory.repository import InMemoryMemoryRepository, PostgresMemoryRepository
from servicemind.memory.service import MAX_CANDIDATE_CEILING, MemoryRetriever

pytestmark = pytest.mark.asyncio

LIVE_TENANT = UUID("11111111-1111-4111-8111-111111111111")

# More distractors than the window can hold, so a ranking that cannot tell "matches the
# query in part" from "matches the query nowhere" loses the match to recency alone.
DISTRACTORS = 6
CEILING = DISTRACTORS


def _activate() -> MemoryWriteDecision:
    return MemoryWriteDecision(
        action=MemoryWriteAction.ACTIVATE, reason_codes=("SEMANTIC_LEARNED_FACT",)
    )


def _candidate(tenant_id: UUID, subject_key: str, content: str) -> MemoryCandidate:
    return MemoryCandidate(
        tenant_id=tenant_id,
        scope=MemoryScope(),
        memory_type=MemoryType.SEMANTIC,
        semantic_subtype=SemanticSubtype.LEARNED_FACT,
        subject_key=subject_key,
        content=content,
        source_run_id=None,
        source_trace_id=f"trace-recall-window-{subject_key}",
        evidence_refs=(
            MemoryEvidenceRef(
                evidence_id=f"ev-recall-window-{subject_key}",
                source_ref=f"kb://recall-window/{subject_key}",
                content_hash="b" * 64,
                verified=True,
            ),
        ),
        confidence=0.9,
        importance=0.7,
        created_by="test-recall-window",
        valid_from=datetime.now(UTC) - timedelta(minutes=1),
    )


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param("memory", id="in-memory"),
        pytest.param("postgres", id="postgres", marks=[pytest.mark.docker, pytest.mark.postgres]),
    ],
)
async def test_a_partial_match_survives_the_candidate_ceiling(backend: str) -> None:
    """A memory sharing part of the query outranks distractors sharing none of it.

    The marker is a hex digest, so it survives the tokeniser as one lexeme and the query
    shares terms with the match and with nothing else. That isolates the assertion from
    whatever else the live tenant has stored: every pre-existing row scores below the
    match, or alongside the distractors, but never between them and the match.

    The distractors are written last on purpose. A ranking that scores the match at zero
    can only fall back on ``updated_at``, and under that fallback the newest rows -- the
    distractors -- are exactly what fills the window.
    """
    marker = uuid4().hex[:12]
    query = MemoryQuery(
        tenant_id=LIVE_TENANT,
        text=f"recall-window {marker} vpn",
        user_id="recall-window-probe",
    )
    # Shares "vpn" and the marker; omits "recall" and "window", which is the partial
    # match AND semantics cannot see.
    partial = f"vpn {marker} rebinding"

    if backend == "memory":
        repository: InMemoryMemoryRepository | PostgresMemoryRepository = InMemoryMemoryRepository()
    else:
        repository = PostgresMemoryRepository(LIVE_TENANT)

    stored = await repository.persist(
        _candidate(LIVE_TENANT, f"recall-window-{marker}-partial", partial), _activate()
    )
    assert stored is not None and stored.status is MemoryStatus.ACTIVE

    for index in range(DISTRACTORS):
        # Content chosen to share no lexeme with the query, including the marker.
        await repository.persist(
            _candidate(
                LIVE_TENANT,
                f"recall-window-{marker}-distractor-{index}",
                f"printer toner cartridge replacement number {index} {marker[:4]}",
            ),
            _activate(),
        )

    window = await repository.candidates(query, ceiling=CEILING)
    contents = [record.content for record in window]
    assert partial in contents, (
        f"the partial match was cut from a window of {CEILING} while {DISTRACTORS} "
        f"newer distractors shared nothing with the query; window held {contents}"
    )


@pytest.mark.docker
@pytest.mark.postgres
async def test_the_two_implementations_agree_on_the_top_of_the_window() -> None:
    """The same corpus, ranked by both repositories, puts the same memory first.

    The two implementations answer one contract; a ranking difference between them is a
    difference in what the product recalls depending on which one is deployed, which is
    not something a test can leave to whichever one a given suite happens to build.
    """
    marker = uuid4().hex[:12]
    query = MemoryQuery(
        tenant_id=LIVE_TENANT,
        text=f"recall-window {marker} vpn",
        user_id="recall-window-probe",
    )
    partial = f"vpn {marker} rebinding"

    in_memory = InMemoryMemoryRepository()
    postgres = PostgresMemoryRepository(LIVE_TENANT)
    for repository in (in_memory, postgres):
        await repository.persist(
            _candidate(LIVE_TENANT, f"recall-window-{marker}-partial", partial), _activate()
        )
        for index in range(DISTRACTORS):
            await repository.persist(
                _candidate(
                    LIVE_TENANT,
                    f"recall-window-{marker}-distractor-{index}",
                    f"printer toner cartridge replacement number {index} {marker[:4]}",
                ),
                _activate(),
            )

    memory_window = await in_memory.candidates(query, ceiling=CEILING)
    postgres_window = await postgres.candidates(query, ceiling=CEILING)
    assert memory_window[0].content == partial, "the reference implementation lost the match"
    assert postgres_window[0].content == partial, (
        "the deployed implementation ranked a memory that shares nothing with the query "
        f"above the one that shares {partial!r}"
    )


class _FixedVectors:
    """A provider whose similarity is decided by the test rather than by a model.

    What is under test is which memories the retriever *asks* to have embedded, so a
    vector chosen per content isolates that question from a model's opinion of it. A
    real provider would need the live TEI service and would answer a different one.
    """

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors

    async def embed_query(self, text: str) -> list[float]:
        return self.vectors[text]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.vectors[text] for text in texts]


LEXICAL_QUERY = "mfa device rebind"
#: Shares no lexeme with ``LEXICAL_QUERY``, so every token-overlap ranking puts it last.
ZERO_OVERLAP = "authenticator rotation runbook"


def _rebind_corpus() -> tuple[InMemoryMemoryRepository, list[str]]:
    """One memory that means the query, plus distractors that merely contain its words."""
    repository = InMemoryMemoryRepository()
    distractors = [f"mfa device rebind case {index}" for index in range(DISTRACTORS)]
    return repository, distractors


async def test_a_semantic_ranker_sees_what_a_lexical_window_would_cut() -> None:
    """The window may only cut on the signal the ranker uses.

    ``MemoryRetriever`` embeds candidates and ranks them by cosine similarity, but the
    repository hands them over ordered by lexeme overlap. Asking for a lexically-cut
    window therefore decides the answer before the ranker runs: a memory that shares no
    token with the query is dropped however close it is in meaning. The memory below is
    the single best match and shares nothing with the query, which is exactly the case
    a lexeme-ordered window loses.
    """
    repository, distractors = _rebind_corpus()
    await repository.persist(
        _candidate(LIVE_TENANT, "rebind-zero-overlap", ZERO_OVERLAP), _activate()
    )
    for index, content in enumerate(distractors):
        await repository.persist(
            _candidate(LIVE_TENANT, f"rebind-distractor-{index}", content), _activate()
        )
    query = MemoryQuery(tenant_id=LIVE_TENANT, text=LEXICAL_QUERY, user_id="window-probe")

    provider = _FixedVectors(
        {LEXICAL_QUERY: [1.0, 0.0], ZERO_OVERLAP: [1.0, 0.0]}
        | dict.fromkeys(distractors, [0.0, 1.0])
    )
    semantic = await MemoryRetriever(
        repository, embedding=provider, candidate_ceiling=CEILING
    ).retrieve(query)
    assert semantic, "the semantic ranker returned nothing at all"
    assert semantic[0].memory.content == ZERO_OVERLAP, (
        "the best match in meaning never reached the ranker: a window of "
        f"{CEILING} ordered by lexeme overlap cut it before any embedding was computed"
    )

    # The same ceiling still bounds the window when the repository's own order *is* the
    # ranking signal -- widening the window unconditionally would trade recall for cost
    # on the one path where the cut is sound.
    lexical = await MemoryRetriever(repository, candidate_ceiling=CEILING).retrieve(query)
    assert ZERO_OVERLAP not in [item.memory.content for item in lexical], (
        "the lexical ceiling stopped bounding the window it is supposed to bound"
    )


async def test_a_full_semantic_window_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    """Past the repository cap the window is a lexeme cut again, and that must be said.

    The silent version of this loss is what made it a defect rather than a tradeoff: the
    deployment reported the semantic channel as enabled while the ranking was decided by
    token overlap. The residual bound is acceptable; a residual bound nobody is told
    about is not.
    """
    repository = InMemoryMemoryRepository()
    marker = uuid4().hex[:12]
    contents = [f"{marker} unit {index} alpha{index}" for index in range(MAX_CANDIDATE_CEILING + 1)]
    for index, content in enumerate(contents):
        await repository.persist(_candidate(LIVE_TENANT, f"{marker}-{index}", content), _activate())
    query = MemoryQuery(tenant_id=LIVE_TENANT, text=f"{marker} unit", user_id="window-probe")
    provider = _FixedVectors(dict.fromkeys([query.text, *contents], [0.0, 1.0]))

    with caplog.at_level(logging.WARNING, logger="servicemind.memory.service"):
        await MemoryRetriever(repository, embedding=provider).retrieve(query)

    assert "candidate window is full" in caplog.text, (
        f"{len(contents)} memories were eligible for a window of {MAX_CANDIDATE_CEILING} "
        "and the retriever said nothing about the ones it could not rank"
    )
