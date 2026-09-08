"""Download and verify the pinned Phase 4 production/reference corpora.

Fetches the real externally hosted sources declared in the active Phase 4 v1.2
manifest. PagerDuty is production eligible. Mendeley is retained only as an offline
reference/distractor source and is never selected by default.

* ``pagerduty-incident-response-docs`` -- GitHub repo archive (codeload zip) frozen at
  the manifest ``revision`` commit; the whole zip is sha256-pinned by the manifest.
* ``mendeley-help-desk-tickets-v3`` -- 10 files of the public "Help Desk Tickets" dataset
  (DOI 10.17632/btm76zndnt.3); each file is sha256-pinned individually. Download URLs are
  resolved at runtime from the Mendeley public API so signed object URLs never go stale in
  this script.

Verification is authoritative: a file is written to ``raw/`` only after its sha256 matches
the manifest. Re-running is idempotent (present + valid files are skipped; corrupt or
missing files are re-fetched). Archives are extracted with zipfile member checks (no
absolute paths, ``..``, or symlink members), so no downloaded artifact is executed.

If a source is unreachable or a checksum mismatches, the script stops non-zero and prints
the exact commands a human can run to fetch the artifact by hand, so the failure can be
resolved without guessing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import ssl
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "phase4" / "manifests" / "sources.v1.2.json"
PRODUCTION_RAW = ROOT / "data" / "phase4" / "raw" / "production"
REFERENCE_RAW = ROOT / "data" / "phase4" / "raw" / "reference"

MENDELAY_PUBLIC_API = "https://data.mendeley.com/public-api/datasets/{dataset_id}"
USER_AGENT = "servicemind-phase4-fetch/1.0 (+https://servicemind.local)"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_items() -> dict[str, dict[str, Any]]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {
        item["id"]: item
        for group in ("production_sources", "reference_sources", "evaluation_sources")
        for item in manifest[group]
    }


# --------------------------------------------------------------------------- HTTP


def _ssl_context() -> ssl.SSLContext:
    """A context rooted at a real CA bundle.

    Conda/self-built interpreters sometimes ship without a default verify path, so the
    script prefers an explicit bundle: certifi (if importable), the interpreter prefix's
    ``ssl/cacert.pem``, then the system bundle. Falls back to the interpreter default.
    """
    candidates: list[str] = []
    try:
        import certifi

        candidates.append(certifi.where())
    except Exception:  # noqa: BLE001 - certifi is optional here
        pass
    candidates.extend(
        [
            str(Path(sys.prefix) / "ssl" / "cacert.pem"),
            "/etc/ssl/certs/ca-certificates.crt",
            "/etc/ssl/ca-bundle.pem",
            "/etc/pki/tls/certs/ca-bundle.crt",
        ]
    )
    for path in candidates:
        if Path(path).is_file():
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})


def fetch_bytes(url: str, timeout: int = 120) -> bytes:
    """GET ``url`` following redirects; raise RuntimeError on non-200."""
    try:
        with urllib.request.urlopen(
            _request(url), timeout=timeout, context=_ssl_context()
        ) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise RuntimeError(f"HTTP {status} for {url}")
            return response.read()
    except urllib.error.HTTPError as exc:  # noqa: N818 - propagated with context below
        raise RuntimeError(f"HTTP {exc.code} fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error fetching {url}: {exc.reason}") from exc


def download_to_file(url: str, target: Path, *, retries: int = 3) -> None:
    """Stream ``url`` into ``target`` (bytes already verified by caller afterward)."""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(
                _request(url), timeout=300, context=_ssl_context()
            ) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise RuntimeError(f"HTTP {status} for {url}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as stream:
                    shutil.copyfileobj(response, stream, length=1024 * 256)
            return
        except Exception as exc:  # noqa: BLE001 - network layer retries any transport fault
            last = exc
            if attempt == retries:
                break
    raise RuntimeError(f"download failed after {retries} attempts: {last}") from last


def pause_with_steps(message: str, steps: list[str]) -> None:
    """Print a human-executable remediation plan and exit non-zero (the pause contract)."""
    print(f"\nERROR: {message}", file=sys.stderr)
    print("The Phase 4 production corpus is incomplete. Run these steps by hand, then", file=sys.stderr)
    print("re-run this script; nothing else will proceed until the checksums match.", file=sys.stderr)
    print("\nExecutable steps:", file=sys.stderr)
    for step in steps:
        print(f"  $ {step}", file=sys.stderr)
    raise SystemExit(2)


# ------------------------------------------------------------------- Mendeley v3


def resolve_mendeley_files(dataset_id: str) -> dict[str, dict[str, Any]]:
    """Map filename -> {sha256, size, download_url} for the latest public version."""
    url = MENDELAY_PUBLIC_API.format(dataset_id=dataset_id)
    try:
        payload = json.loads(fetch_bytes(url).decode("utf-8"))
    except RuntimeError as exc:
        pause_with_steps(
            f"cannot resolve Mendeley dataset {dataset_id}: {exc}",
            [
                f"curl -L {url} -o /tmp/mendeley-dataset.json",
                "python3 -c \"import json;d=json.load(open('/tmp/mendeley-dataset.json'));"
                "[print(f['filename'], f['content_details']['sha256_hash'], "
                "f['content_details']['download_url']) for f in d['files']]\"",
            ],
        )
    files: dict[str, dict[str, Any]] = {}
    for item in payload.get("files", []):
        details = item.get("content_details", {})
        files[item["filename"]] = {
            "sha256": details.get("sha256_hash", ""),
            "size": details.get("size", 0),
            "download_url": details.get("download_url", ""),
        }
    return files


def download_mendeley(item: dict[str, Any], files_spec: dict[str, str]) -> list[Path]:
    dataset_id = Path(item["source_uri"]).parts[-2]
    remote = resolve_mendeley_files(dataset_id)
    target_dir = REFERENCE_RAW / "mendeley-helpdesk-v3"
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, expected in sorted(files_spec.items()):
        target = target_dir / filename
        if target.is_file() and sha256_of(target) == expected:
            print(f"  mendeley/{filename}: present, sha256 ok")
            written.append(target)
            continue
        if filename not in remote:
            pause_with_steps(
                f"Mendeley v3 API no longer exposes {filename!r} (expected sha256 {expected})",
                [
                    f"curl -L {item['source_uri']} -o /tmp/mendeley-landing.html",
                    f"# open {item['source_uri']} and download {filename} into {target_dir}/",
                    f"echo '{expected}  {target}' | sha256sum -c -",
                ],
            )
        if remote[filename]["sha256"] != expected:
            pause_with_steps(
                f"Mendeley upstream sha256 for {filename} changed "
                f"({remote[filename]['sha256']} != pinned {expected}); re-pin required",
                [
                    f"curl -L '{remote[filename]['download_url']}' -o {target}",
                    f"sha256sum {target}  # record the new hash and update the manifest",
                ],
            )
        print(f"  mendeley/{filename}: downloading ({remote[filename]['size']} bytes) ...")
        download_to_file(remote[filename]["download_url"], target)
        actual = sha256_of(target)
        if actual != expected:
            pause_with_steps(
                f"sha256 mismatch after download for mendeley/{filename}",
                [
                    f"rm -f {target}",
                    f"curl -L '{remote[filename]['download_url']}' -o {target}",
                    f"sha256sum {target}",
                    f"# expected: {expected}",
                ],
            )
        written.append(target)
        print(f"  mendeley/{filename}: sha256 ok")
    return written


# -------------------------------------------------------------- PagerDuty archive


def download_pagerduty(item: dict[str, Any]) -> tuple[Path, Path]:
    owner, repo = Path(item["source_uri"]).parts[-2], Path(item["source_uri"]).parts[-1]
    revision = item["revision"]
    expected = item["archive_sha256"]
    archive = PRODUCTION_RAW / "pagerduty-incident-response-docs-master.zip"
    extracted_root = PRODUCTION_RAW / "incident-response-docs-master"

    if archive.is_file() and sha256_of(archive) == expected:
        print(f"  pagerduty archive: present, sha256 ok ({archive.stat().st_size} bytes)")
    else:
        url = f"https://codeload.github.com/{owner}/{repo}/zip/{revision}"
        if archive.exists():
            archive.unlink()
        print(f"  pagerduty archive: downloading {url} ...")
        download_to_file(url, archive)
        actual = sha256_of(archive)
        if actual != expected:
            pause_with_steps(
                "pagerduty archive sha256 mismatch after download",
                [
                    f"rm -f {archive}",
                    f"curl -L '{url}' -o {archive}",
                    f"sha256sum {archive}",
                    f"# expected: {expected}",
                ],
            )
        print(f"  pagerduty archive: sha256 ok ({archive.stat().st_size} bytes)")

    if (extracted_root / "docs").is_dir():
        print(f"  pagerduty docs tree already at {extracted_root}")
        return archive, extracted_root
    extracted_root.parent.mkdir(parents=True, exist_ok=True)
    if extracted_root.exists():
        shutil.rmtree(extracted_root)
    with zipfile.ZipFile(archive) as zf:
        members = []
        for info in zf.infolist():
            name = info.filename
            if name.startswith(("/", "\\")) or ".." in name.split("/"):
                raise ValueError(f"unsafe zip member: {name}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:  # symlink
                raise ValueError(f"unsafe symlink member: {name}")
            members.append(name)
        common_root = Path(members[0].split("/", 1)[0])
        assert all(Path(name).parts[0] == common_root.as_posix() for name in members), "not single-root zip"
        tmp = extracted_root.parent / f".{extracted_root.name}.tmp"
        if tmp.exists():
            shutil.rmtree(tmp)
        zf.extractall(tmp)
        (tmp / common_root).rename(extracted_root)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"  pagerduty docs tree extracted to {extracted_root}")
    return archive, extracted_root


# --------------------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=["pagerduty", "mendeley"],
        default=["pagerduty"],
        help="Sources to fetch (default: production-eligible PagerDuty only).",
    )
    args = parser.parse_args()
    items = manifest_items()

    print("Phase 4 production corpus fetch")
    summary: dict[str, Any] = {"status": "passed", "sources": {}}
    if "pagerduty" in args.sources:
        archive, tree = download_pagerduty(items["pagerduty-incident-response-docs"])
        summary["sources"]["pagerduty"] = {
            "archive": str(archive.relative_to(ROOT)),
            "archive_sha256": sha256_of(archive),
            "docs_tree": str(tree.relative_to(ROOT)),
            "md_file_count": sum(1 for _ in (tree / "docs").rglob("*.md")),
        }
    if "mendeley" in args.sources:
        files = download_mendeley(
            items["mendeley-help-desk-tickets-v3"], items["mendeley-help-desk-tickets-v3"]["files"]
        )
        summary["sources"]["mendeley"] = {
            "dir": str(files[0].parent.relative_to(ROOT)),
            "files": {path.name: sha256_of(path) for path in files},
        }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
