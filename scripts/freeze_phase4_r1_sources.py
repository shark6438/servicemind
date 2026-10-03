#!/usr/bin/env python
"""Freeze the LoTTE technology split as the R1 retrieval-evaluation source.

Why this source, and why not the tarball
----------------------------------------
R2 admits or rejects the late-interaction arm by comparing pool coverage on LoTTE
technology ``dev``. The canonical distribution is a 3.58 GB tarball on a Stanford
host, which measured 45 KiB/s from this machine -- eight parallel connections only
reached 75 KiB/s, i.e. about 21 hours, and ``Accept-Ranges`` did not survive the
first transient failure without truncating the partial file. The ColBERT authors
publish the same files on the Hugging Face Hub at a pinned commit, which measured
8-13 MB/s here. We freeze the Hub copy and record the tarball's identity as a
cross-reference: a source nobody can re-fetch is not a frozen source.

The Hub copy is the authors' own upload (``colbertv2/lotte_passages``, Apache-2.0),
laid out exactly like the tarball's ``lotte/technology/`` subtree. Nothing is
reformatted or downsampled.

Provenance discipline
---------------------
* The revision is a commit hash, never a branch name.
* A file is renamed out of ``*.part`` only after its byte count and digest match
  what the Hub reports for the pinned revision: LFS files against their ``sha256``,
  small files against their git blob id. Both are byte-exact.
* ``--check`` re-hashes what is on disk against the frozen manifest, so a later
  edit to a frozen file is a loud failure rather than a quiet drift.

Exit codes
----------
``0``
    Every required file is present and matches the manifest.
``1``
    A frozen file is missing, or its bytes no longer match what was frozen.
``2``
    Nothing to judge yet: the manifest has not been written, or a file was never
    fetched. Not the same as a mismatch -- nothing drifted, there is simply no
    observation.
``3``
    The pin itself is unusable: the Hub does not serve the pinned revision, or a
    file's size/digest at that revision disagrees with the frozen manifest. The
    upstream changed under a hash that was supposed to make that impossible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]

HUB = "https://hf-mirror.com"
REPO = "colbertv2/lotte_passages"
REVISION = "3be7f857585299f1268d29d3591202d731ea84a1"
TOPIC = "technology"

RAW_DIR = ROOT / "data" / "phase4" / "raw" / "eval" / "lotte-hf"
MANIFEST_DIR = ROOT / "data" / "phase4" / "manifests"
MANIFEST_PATH = MANIFEST_DIR / "sources.v1.3.json"
PREDECESSOR = MANIFEST_DIR / "sources.v1.2.json"

#: Recorded so the tarball remains identifiable even though we do not fetch it.
TARBALL_URI = "https://downloads.cs.stanford.edu/nlp/data/colbert/colbertv2/lotte.tar.gz"
TARBALL_BYTES = 3576167599
TARBALL_LAST_MODIFIED = "Tue, 01 Feb 2022 21:33:35 GMT"

#: ``(repo path, split)``. Passages plus the four question/qrel files per split.
FILES: tuple[tuple[str, str], ...] = (
    (f"{TOPIC}/dev_collection.jsonl", "dev"),
    (f"{TOPIC}/dev/questions.search.tsv", "dev"),
    (f"{TOPIC}/dev/qas.search.jsonl", "dev"),
    (f"{TOPIC}/dev/questions.forum.tsv", "dev"),
    (f"{TOPIC}/dev/qas.forum.jsonl", "dev"),
    (f"{TOPIC}/dev/metadata.jsonl", "dev"),
    (f"{TOPIC}/test/collection.tsv", "test"),
    (f"{TOPIC}/test/questions.search.tsv", "test"),
    (f"{TOPIC}/test/qas.search.jsonl", "test"),
    (f"{TOPIC}/test/questions.forum.tsv", "test"),
    (f"{TOPIC}/test/qas.forum.jsonl", "test"),
    (f"{TOPIC}/test/metadata.jsonl", "test"),
)

CHUNK = 1 << 20


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _git_blob_id(path: Path) -> str:
    """The id git itself would give this file, which is what the Hub reports for
    files small enough not to be stored as LFS objects."""
    size = path.stat().st_size
    digest = hashlib.sha1()
    digest.update(f"blob {size}\0".encode())
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _count_lines(path: Path) -> int:
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            total += chunk.count(b"\n")
    return total


def hub_entries(client: httpx.Client) -> dict[str, dict[str, Any]]:
    """Size and digest of every file we freeze, as the pinned revision reports them."""
    entries: dict[str, dict[str, Any]] = {}
    for directory in ("", "/dev", "/test"):
        response = client.get(f"{HUB}/api/datasets/{REPO}/tree/{REVISION}/{TOPIC}{directory}")
        response.raise_for_status()
        for item in response.json():
            if not isinstance(item, dict) or item.get("type") != "file":
                continue
            # The tree endpoint reports an LFS object's content digest as ``lfs.oid``;
            # ``?blobs=true`` calls the same value ``lfs.sha256``. For a file stored
            # inline, the top-level ``oid`` is the git blob id of the content itself.
            lfs = item.get("lfs") or {}
            entries[item["path"]] = {
                "size": item.get("size"),
                "lfs_sha256": lfs.get("sha256") or lfs.get("oid"),
                "git_blob_id": item.get("oid"),
            }
    return entries


def download(client: httpx.Client, name: str, expected: dict[str, Any]) -> str:
    """Fetch one file, verifying it before it is allowed to leave ``.part``.

    Returns ``"cached"``, ``"fetched"``, or ``"failed"``.
    """
    lfs = bool(expected.get("lfs_sha256"))
    target = RAW_DIR / name
    if target.is_file():
        entry = _describe(target, lfs=lfs)
        if entry["size"] == expected["size"] and entry["digest"] == _expected_digest(expected):
            return "cached"
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    url = f"{HUB}/datasets/{REPO}/resolve/{REVISION}/{name}"
    if part.exists():
        part.unlink()
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with part.open("wb") as handle:
                for chunk in response.iter_bytes(CHUNK):
                    handle.write(chunk)
    except httpx.HTTPError as error:
        print(f"  download error: {error}")
        part.unlink(missing_ok=True)
        return "failed"
    entry = _describe(part, lfs=lfs)
    if entry["size"] != expected["size"] or entry["digest"] != _expected_digest(expected):
        print(
            f"  rejected: size={entry['size']} want {expected['size']}, "
            f"digest={entry['digest'][:16]} want {_expected_digest(expected)[:16]}"
        )
        part.unlink(missing_ok=True)
        return "failed"
    part.replace(target)
    return "fetched"


def _expected_digest(expected: dict[str, Any]) -> str:
    return expected["lfs_sha256"] or expected["git_blob_id"]


def _describe(path: Path, *, lfs: bool) -> dict[str, Any]:
    """Size plus the digest the Hub reports for this file.

    Which digest applies is the Hub's storage decision, not the file's size: a
    322 MB ``metadata.jsonl`` is stored inline and is described by its git blob
    id, while a 557 MB ``collection.tsv`` is an LFS object described by sha256.
    Inferring from size would verify the wrong function of the same bytes.
    """
    size = path.stat().st_size
    return {"size": size, "digest": _sha256(path) if lfs else _git_blob_id(path)}


def _digest_kind(name: str, expected: dict[str, Any]) -> str:
    return "sha256" if expected.get("lfs_sha256") else "git_blob_id"


def _split_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    counts["dev_passages"] = _count_lines(RAW_DIR / f"{TOPIC}/dev_collection.jsonl")
    counts["test_passages"] = _count_lines(RAW_DIR / f"{TOPIC}/test/collection.tsv")
    for split in ("dev", "test"):
        for channel in ("search", "forum"):
            counts[f"{split}_{channel}_queries"] = _count_lines(
                RAW_DIR / f"{TOPIC}/{split}/questions.{channel}.tsv"
            )
    return counts


def write_manifest(client: httpx.Client, entries: dict[str, dict[str, Any]]) -> int:
    if not PREDECESSOR.is_file():
        print(f"missing predecessor manifest {PREDECESSOR}", file=sys.stderr)
        return 3
    manifest = json.loads(PREDECESSOR.read_text())
    files: dict[str, dict[str, Any]] = {}
    for name, split in FILES:
        path = RAW_DIR / name
        lfs = bool(entries[name].get("lfs_sha256"))
        described = _describe(path, lfs=lfs)
        if described["size"] != entries[name]["size"]:
            print(
                f"refusing to freeze {name}: on disk {described['size']} bytes, "
                f"pinned revision says {entries[name]['size']}",
                file=sys.stderr,
            )
            return 3
        files[name] = {
            "split": split,
            "bytes": described["size"],
            _digest_kind(name, entries[name]): described["digest"],
        }
    entry = {
        "id": "lotte-technology",
        "source_uri": f"https://huggingface.co/datasets/{REPO}",
        "mirror_uri": f"{HUB}/datasets/{REPO}",
        "revision": REVISION,
        "license": "Apache-2.0",
        "authority": "public_benchmark",
        "production_allowed": False,
        "download_status": "frozen_verified",
        "path": str(RAW_DIR.relative_to(ROOT)),
        "ingestion_scope": (
            f"LoTTE {TOPIC} topic only. dev selects and diagnoses; test is sealed for a "
            "single R5 confirmation and is guarded by the R1.2 loader."
        ),
        "required_for_release_gate": False,
        "required_for_r2_admission": "dev",
        "canonical_distribution": {
            "uri": TARBALL_URI,
            "bytes": TARBALL_BYTES,
            "last_modified": TARBALL_LAST_MODIFIED,
            "fetched": False,
            "note": (
                "3.58 GB at 45 KiB/s measured from this host (~21 h); the pinned Hub "
                "copy is the authors' own upload of the same files. Kept as a "
                "cross-reference to be checked if the tarball is ever fetched."
            ),
        },
        "files": files,
        **_split_counts(),
    }
    manifest["evaluation_sources"] = [
        source
        for source in manifest.get("evaluation_sources", [])
        if source.get("id") != entry["id"]
    ] + [entry]
    manifest["manifest_version"] = "1.3"
    manifest["frozen_at"] = "2026-10-02"
    manifest["revised_at"] = "2026-10-02"
    manifest["revision_notes"] = (
        (manifest.get("revision_notes") or "")
        + " v1.3 freezes LoTTE technology dev+test from the pinned ColBERT Hub upload "
        "for R2 candidate-recall admission; the Stanford tarball is recorded as an "
        "unfetched cross-reference."
    ).strip()
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {MANIFEST_PATH.relative_to(ROOT)} ({len(files)} files)")
    return 0


def run_check() -> int:
    if not MANIFEST_PATH.is_file():
        print(f"no manifest at {MANIFEST_PATH.relative_to(ROOT)}; run --download first")
        return 2
    manifest = json.loads(MANIFEST_PATH.read_text())
    entry = next(
        (s for s in manifest.get("evaluation_sources", []) if s.get("id") == "lotte-technology"),
        None,
    )
    if entry is None:
        print("manifest has no lotte-technology entry", file=sys.stderr)
        return 3
    absent, drifted = [], []
    for name, frozen in sorted(entry["files"].items()):
        path = RAW_DIR / name
        if not path.is_file():
            absent.append(name)
            continue
        digest_field = "sha256" if "sha256" in frozen else "git_blob_id"
        actual = _sha256(path) if digest_field == "sha256" else _git_blob_id(path)
        if path.stat().st_size != frozen["bytes"] or actual != frozen[digest_field]:
            drifted.append(f"{name}: {actual[:16]} != frozen {frozen[digest_field][:16]}")
    for name in absent:
        print(f"MISSING {name}")
    for line in drifted:
        print(f"DRIFT   {line}")
    if drifted:
        print(f"FAIL {len(drifted)} frozen file(s) changed; the freeze is void")
        return 1
    if absent:
        print(f"INCOMPLETE {len(absent)} file(s) absent; nothing has drifted")
        return 2
    print(
        f"PASS {len(entry['files'])} files, revision {entry['revision'][:12]}, "
        f"dev_passages={entry['dev_passages']}, test_passages={entry['test_passages']}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--download", action="store_true", help="fetch and verify the split")
    parser.add_argument("--check", action="store_true", help="re-verify frozen files")
    args = parser.parse_args(argv)
    if not args.download and not args.check:
        parser.print_help()
        return 3

    if args.check:
        return run_check()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        entries = hub_entries(client)
        missing = [name for name, _ in FILES if name not in entries]
        if missing:
            print(f"pinned revision {REVISION} no longer lists: {missing}", file=sys.stderr)
            return 3
        failures = []
        for name, _ in FILES:
            outcome = download(client, name, entries[name])
            print(f"  {outcome:8s} {name}")
            if outcome == "failed":
                failures.append(name)
        if failures:
            print(f"could not freeze {len(failures)} file(s): {failures}", file=sys.stderr)
            return 1
        code = write_manifest(client, entries)
    return run_check() if code == 0 else code


if __name__ == "__main__":
    raise SystemExit(main())
