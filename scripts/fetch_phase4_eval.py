"""Restore and verify the pinned Phase 4 external evaluation datasets.

The raw datasets are intentionally gitignored. This script is the reproducible
contract: every downloaded file is pinned by repository revision and SHA-256, a
checksum mismatch fails closed, and the AgentDojo archive is extracted only after
path traversal and symlink checks.

Usage:
    python scripts/fetch_phase4_eval.py
    python scripts/fetch_phase4_eval.py --sources techqa enterpriseops
    python scripts/fetch_phase4_eval.py --verify-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import ssl
import urllib.request
import zipfile
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / "data" / "phase4" / "raw" / "eval"
ARCHIVES = EVAL_ROOT / ".archives"

TECHQA_REVISION: Final = "0b5bbc84b7f07d6d09d063130e90b716d8d4a32a"
TECHQA_FILES: Final = {
    "README.md": "8d85191f81a748f2a66bb32cecb972727e69fcc0541c1f936c4debfa4823190b",
    "corpus.zip": "c06aa287dcc1abf8db6b49b8495df095db73342d729f6451ac330785245d10be",
    "train.json": "69d97231509482ed6bd5ec1c4bc0607acb82a88d11169eb8383592d0ca8b93c7",
}

ENTERPRISEOPS_REVISION: Final = "c8e538eae8a6205294f0a86675fefdc1fac408f6"
ENTERPRISEOPS_FILES: Final = {
    "README.md": "cf559d619ad720c652c038d1b5fa9c541c45f83bac93a5d535c7fb3e7a091820",
    "oracle/itsm-00000-of-00001.parquet": (
        "63240f60ee8810e50267034137b811f4c06c5f2cd84fcd8b1122448e871f9f4b"
    ),
    "plus_5_tools/itsm-00000-of-00001.parquet": (
        "b2428d1a7fd6a374d432d3d4a262ec82f3ffa4a2611c6a0c5027f460e3cd6e1a"
    ),
    "plus_10_tools/itsm-00000-of-00001.parquet": (
        "235b1a5cf8fa0f17e01c07f0e8b2107d6d56bd0fecf1e46c4354beed10d3a1dc"
    ),
    "plus_15_tools/itsm-00000-of-00001.parquet": (
        "34c521f338e1fb9957400eda58e0ef8be32c9b021190a4f2dbafd0f7cfbebbd5"
    ),
}

AGENTDOJO_REVISION: Final = "089ed468cf3ed0322acc66b0211f26d9d90dbf60"
AGENTDOJO_ARCHIVE_SHA256: Final = "b1cbd20962cca3dafb9317a4117db65cd61a56c7f480ff0df2357d3f6c77849c"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ssl_context() -> ssl.SSLContext:
    candidates = [
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/ssl/ca-bundle.pem",
        "/etc/pki/tls/certs/ca-bundle.crt",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return ssl.create_default_context(cafile=candidate)
    return ssl.create_default_context()


def download_verified(url: str, target: Path, expected: str, *, verify_only: bool) -> None:
    if target.is_file() and sha256_of(target) == expected:
        print(f"  {target.relative_to(ROOT)}: sha256 ok")
        return
    if verify_only:
        raise RuntimeError(f"missing or invalid pinned file: {target.relative_to(ROOT)}")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "servicemind-eval-fetch/1.2"})
    try:
        with urllib.request.urlopen(request, timeout=300, context=_ssl_context()) as response:
            with partial.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 256)
        actual = sha256_of(partial)
        if actual != expected:
            raise RuntimeError(
                f"sha256 mismatch for {target.name}: expected {expected}, got {actual}"
            )
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    print(f"  {target.relative_to(ROOT)}: downloaded, sha256 ok")


def restore_techqa(*, verify_only: bool) -> dict[str, object]:
    target = EVAL_ROOT / "techqa-rag-eval"
    base = f"https://huggingface.co/datasets/nvidia/TechQA-RAG-Eval/resolve/{TECHQA_REVISION}"
    for relative, digest in TECHQA_FILES.items():
        download_verified(
            f"{base}/{relative}?download=true", target / relative, digest, verify_only=verify_only
        )
    rows = json.loads((target / "train.json").read_text(encoding="utf-8"))
    with zipfile.ZipFile(target / "corpus.zip") as archive:
        corpus_files = sum(not item.is_dir() for item in archive.infolist())
    if len(rows) != 910 or corpus_files != 28_481:
        raise RuntimeError(
            f"unexpected TechQA shape: rows={len(rows)}, corpus_files={corpus_files}"
        )
    return {
        "revision": TECHQA_REVISION,
        "rows": len(rows),
        "answerable": sum(not row.get("is_impossible", False) for row in rows),
        "impossible": sum(bool(row.get("is_impossible", False)) for row in rows),
        "corpus_files": corpus_files,
    }


def restore_enterpriseops(*, verify_only: bool) -> dict[str, object]:
    target = EVAL_ROOT / "enterpriseops-gym-itsm"
    base = (
        "https://huggingface.co/datasets/ServiceNow-AI/EnterpriseOps-Gym/resolve/"
        f"{ENTERPRISEOPS_REVISION}"
    )
    for relative, digest in ENTERPRISEOPS_FILES.items():
        download_verified(
            f"{base}/{relative}?download=true", target / relative, digest, verify_only=verify_only
        )

    import pandas as pd

    counts = {
        path.parent.name: len(pd.read_parquet(path)) for path in sorted(target.rglob("*.parquet"))
    }
    if set(counts.values()) != {103} or len(counts) != 4:
        raise RuntimeError(f"unexpected EnterpriseOps ITSM shape: {counts}")
    return {"revision": ENTERPRISEOPS_REVISION, "mode_records": counts}


def _safe_extract_single_root(archive_path: Path, target: Path) -> int:
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if not infos:
            raise RuntimeError("empty AgentDojo archive")
        root = Path(infos[0].filename).parts[0]
        for item in infos:
            parts = Path(item.filename).parts
            if (
                not parts
                or parts[0] != root
                or item.filename.startswith(("/", "\\"))
                or ".." in parts
                or (item.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise RuntimeError(f"unsafe archive member: {item.filename}")
        temporary = target.parent / f".{target.name}.extracting"
        if temporary.exists():
            shutil.rmtree(temporary)
        archive.extractall(temporary)
        if target.exists():
            shutil.rmtree(target)
        (temporary / root).replace(target)
        shutil.rmtree(temporary, ignore_errors=True)
    return sum(path.is_file() for path in target.rglob("*"))


def restore_agentdojo(*, verify_only: bool) -> dict[str, object]:
    archive = ARCHIVES / f"agentdojo-{AGENTDOJO_REVISION[:7]}.zip"
    url = f"https://codeload.github.com/ethz-spylab/agentdojo/zip/{AGENTDOJO_REVISION}"
    download_verified(url, archive, AGENTDOJO_ARCHIVE_SHA256, verify_only=verify_only)
    target = EVAL_ROOT / "agentdojo-main"
    if verify_only:
        if not (target / "LICENSE").is_file():
            raise RuntimeError("AgentDojo extraction is missing or incomplete")
        file_count = sum(path.is_file() for path in target.rglob("*"))
    else:
        file_count = _safe_extract_single_root(archive, target)
    return {
        "revision": AGENTDOJO_REVISION,
        "archive_sha256": AGENTDOJO_ARCHIVE_SHA256,
        "extracted_files": file_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=["techqa", "enterpriseops", "agentdojo"],
        default=["techqa", "enterpriseops", "agentdojo"],
    )
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    restorers = {
        "techqa": restore_techqa,
        "enterpriseops": restore_enterpriseops,
        "agentdojo": restore_agentdojo,
    }
    result = {name: restorers[name](verify_only=args.verify_only) for name in args.sources}
    print(json.dumps({"status": "passed", "sources": result}, indent=2))


if __name__ == "__main__":
    main()
