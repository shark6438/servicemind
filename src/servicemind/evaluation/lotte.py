"""LoTTE as a frozen external benchmark for the candidate-recall stage.

Why an external benchmark at all
--------------------------------
TechQA's 910 queries have been consumed by every earlier phase, so a number
measured on them can no longer be reported as held out. LoTTE's ``technology``
topic is unseen by this project, ships official qrels, and separates an
``dev`` split (for choosing) from a ``test`` split (for one-shot confirmation).
The phase-4 plan therefore admits or rejects the late-interaction arm on
LoTTE ``technology`` ``dev``, and freezes ``test`` until the protocol is
registered.

Provenance, and one deliberate deviation
----------------------------------------
The plan named Stanford's 3,576,167,599-byte ``lotte.tar.gz``. Measured from
this host that endpoint serves ~45 KiB/s -- ~21 h for the archive, and eight
parallel range requests only reached ~75 KiB/s, so the limit is upstream. The
same data is published by the ColBERT authors themselves as two Hugging Face
datasets, both reachable at ~8.5 MB/s, and can be pinned to a *revision* with a
per-file digest rather than to one opaque tarball hash -- strictly more
checkable, not less. This module reads that mirror and records the Stanford
archive's size and Last-Modified in the manifest as the cross-check target.

Splits are not interchangeable
------------------------------
:func:`load_split` refuses ``test`` unless ``SERVICEMIND_LOTTE_TEST_SEAL``
matches the value frozen in the source manifest. The seal is not a secret --
the manifest is meant to be read -- it is a guard against a tuning script
reaching the confirmation set by accident, and it is paired with a static scan
that fails if any non-confirmation script names the test split.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

#: Both repos are uploads by the ColBERT authors (``colbertv2``), so the mirror
#: is the same publisher as the tarball rather than a third-party re-upload.
PASSAGES_REPO = "colbertv2/lotte_passages"
PASSAGES_REVISION = "3be7f857585299f1268d29d3591202d731ea84a1"
QUERIES_REPO = "colbertv2/lotte"
QUERIES_REVISION = "1a87807da631e4197d77f7e720c38941abcf26d1"
LICENSE = "apache-2.0"

#: The unchanged upstream archive, kept as the cross-check target. Recorded so a
#: future reader can tell the mirror was a decision, not an accident.
STANFORD_ARCHIVE_URI = "https://downloads.cs.stanford.edu/nlp/data/colbert/colbertv2/lotte.tar.gz"
STANFORD_ARCHIVE_BYTES = 3576167599
STANFORD_ARCHIVE_LAST_MODIFIED = "Tue, 01 Feb 2022 21:33:35 GMT"

DEFAULT_ROOT = Path("data/phase4/raw/eval/lotte-hf")
ROOT_ENV = "SERVICEMIND_LOTTE_ROOT"
MANIFEST_PATH = Path("data/phase4/manifests/sources.v1.3.json")
SEAL_ENV = "SERVICEMIND_LOTTE_TEST_SEAL"

TOPICS = ("lifestyle", "recreation", "science", "technology", "writing")
SPLITS = ("dev", "test")
SOURCES = ("search", "forum")


class LotteError(RuntimeError):
    """The dataset is absent, incomplete, or being read at the wrong split."""


@dataclass(frozen=True, slots=True)
class LottePassage:
    """One collection row. ``pid`` is LoTTE's own document id, as a string."""

    pid: str
    text: str
    author: str | None = None


@dataclass(frozen=True, slots=True)
class LotteQuery:
    """One judged question. ``answer_pids`` is the gold set, possibly empty."""

    qid: str
    query: str
    answer_pids: tuple[str, ...]
    source: str


@dataclass(frozen=True, slots=True)
class LotteSplit:
    """One (topic, split, source) slice: a shared collection plus its questions."""

    topic: str
    split: str
    source: str
    passages: tuple[LottePassage, ...]
    queries: tuple[LotteQuery, ...]

    @property
    def passage_count(self) -> int:
        return len(self.passages)

    @property
    def query_count(self) -> int:
        return len(self.queries)

    @property
    def answerable_count(self) -> int:
        return sum(1 for query in self.queries if query.answer_pids)


def data_root(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root)
    return Path(os.environ.get(ROOT_ENV, "").strip() or DEFAULT_ROOT)


def passages_file(topic: str, split: str, *, root: str | Path | None = None) -> Path:
    """Locate a split's collection.

    The upstream upload is not uniform: some splits carry ``collection.tsv`` and
    others only the ``*_collection.jsonl`` rendering of it. Both are the same
    rows, so the reader accepts either rather than pinning one layout.
    """
    if topic not in TOPICS:
        raise LotteError(f"unknown LoTTE topic {topic!r}")
    if split not in SPLITS:
        raise LotteError(f"unknown LoTTE split {split!r}")
    base = data_root(root) / topic
    for candidate in (base / split / "collection.tsv", base / f"{split}_collection.jsonl"):
        if candidate.is_file():
            return candidate
    raise LotteError(f"no collection for {topic}/{split} under {base}")


def queries_file(topic: str, split: str, source: str, *, root: str | Path | None = None) -> Path:
    if source not in SOURCES:
        raise LotteError(f"unknown LoTTE question source {source!r}")
    path = data_root(root) / topic / split / f"qas.{source}.jsonl"
    if not path.is_file():
        raise LotteError(f"no questions for {topic}/{split}/{source} at {path}")
    return path


def _seal() -> str:
    return os.environ.get(SEAL_ENV, "").strip()


def sealed_test_token(*, manifest: Path | None = None) -> str:
    """Read the frozen test-split seal, or raise if the manifest is not frozen."""
    path = Path(manifest) if manifest is not None else MANIFEST_PATH
    if not path.is_file():
        raise LotteError(f"{path} is missing; the confirmation split is not frozen yet")
    payload = json.loads(path.read_text(encoding="utf-8"))
    token = str(payload.get("lotte_test_seal", "")).strip()
    if not token:
        raise LotteError(f"{path} records no lotte_test_seal")
    return token


def assert_split_readable(split: str) -> None:
    """Refuse the confirmation split unless the caller presents the frozen seal.

    Called by every reader, so it cannot be bypassed by reaching for the file
    path directly.
    """
    if split != "test":
        return
    expected = sealed_test_token()
    if _seal() != expected:
        raise PermissionError(
            "the LoTTE test split is frozen for one-shot confirmation and cannot be read "
            f"by a tuning script; set {SEAL_ENV} to the seal recorded in {MANIFEST_PATH} "
            "only when running the registered confirmation"
        )


def iter_passages(
    topic: str, split: str, *, root: str | Path | None = None, limit: int | None = None
) -> Iterator[LottePassage]:
    """Stream a collection so a 10^6-row split never has to fit in memory twice."""
    assert_split_readable(split)
    path = passages_file(topic, split, root=root)
    with path.open("r", encoding="utf-8") as handle:
        if path.suffix == ".tsv":
            for emitted, line in enumerate(handle):
                if limit is not None and emitted >= limit:
                    return
                line = line.rstrip("\n")
                if not line:
                    continue
                pid, _, text = line.partition("\t")
                yield LottePassage(pid=pid, text=text)
            return
        for emitted, line in enumerate(handle):
            if limit is not None and emitted >= limit:
                return
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            yield LottePassage(
                pid=str(row["doc_id"]),
                text=str(row["text"]),
                author=row.get("author"),
            )


def load_queries(
    topic: str,
    split: str,
    source: str,
    *,
    root: str | Path | None = None,
    limit: int | None = None,
) -> tuple[LotteQuery, ...]:
    assert_split_readable(split)
    path = queries_file(topic, split, source, root=root)
    queries: list[LotteQuery] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            if limit is not None and len(queries) >= limit:
                break
            row = json.loads(line)
            queries.append(
                LotteQuery(
                    qid=str(row["qid"]),
                    query=str(row["query"]),
                    answer_pids=tuple(str(pid) for pid in row.get("answer_pids") or ()),
                    source=source,
                )
            )
    return tuple(queries)


def load_split(
    topic: str,
    split: str,
    source: str,
    *,
    root: str | Path | None = None,
    limit: int | None = None,
) -> LotteSplit:
    passages = tuple(iter_passages(topic, split, root=root))
    queries = load_queries(topic, split, source, root=root, limit=limit)
    return LotteSplit(topic=topic, split=split, source=source, passages=passages, queries=queries)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_digests(topic: str, *, root: str | Path | None = None) -> dict[str, str]:
    """Digest every technology file, for the freeze manifest and for checking it."""
    base = data_root(root) / topic
    if not base.is_dir():
        raise LotteError(f"{base} is missing; fetch the dataset before freezing it")
    digests: dict[str, str] = {}
    for path in sorted(p for p in base.rglob("*") if p.is_file()):
        digests[str(path.relative_to(base))] = _sha256(path)
    return digests


def verify_digests(topic: str, *, root: str | Path | None = None) -> list[str]:
    """Return the relative paths whose content no longer matches the manifest."""
    path = MANIFEST_PATH
    if not path.is_file():
        raise LotteError(f"{path} is missing; freeze the dataset before verifying it")
    entry = _manifest_entry(json.loads(path.read_text(encoding="utf-8")))
    files = entry.get("files")
    # The manifest is on disk and could have been edited; narrow rather than assume, so a
    # malformed "files" section is reported as every path being untracked instead of
    # raising AttributeError out of a verification helper.
    recorded: dict[str, str] = files if isinstance(files, dict) else {}
    problems = [
        name
        for name, expected in sorted(recorded.items())
        if not (data_root(root) / topic / name).is_file()
        or _sha256(data_root(root) / topic / name) != expected
    ]
    present = set(file_digests(topic, root=root))
    problems.extend(f"{name} (untracked)" for name in sorted(present - set(recorded)))
    return problems


def _manifest_entry(payload: dict[str, object]) -> dict[str, object]:
    for section in ("evaluation_sources", "reference_sources", "production_sources"):
        for item in payload.get(section) or ():  # type: ignore[union-attr]
            if item.get("id") == "lotte-technology":  # type: ignore[union-attr]
                return item  # type: ignore[return-value]
    raise LotteError("the manifest records no lotte-technology entry")
