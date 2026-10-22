"""Run mutation experiments in distinct worktrees; never interleave mutations in one tree.

Run after the ordinary tests if the selected experiments share database tables.
Only disposable database URLs belong in the inherited environment.
"""

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from servicemind.evaluation.source_revision import source_revision

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--workers", type=int, default=3)
parser.add_argument("scripts", nargs="+")
args = parser.parse_args()
root = args.root.resolve()
args.output.mkdir(parents=True, exist_ok=True)
revision = source_revision(root)
assert revision and revision != "unknown"
changed = set()
for command in [
    ("diff", "HEAD", "--name-only", "-z"),
    ("ls-files", "--others", "--exclude-standard", "-z"),
]:
    changed.update(
        p for p in subprocess.check_output(["git", *command], cwd=root).decode().split("\0") if p
    )


def run(script):
    script, _, shard = script.partition("@")
    name = Path(script).stem
    if shard:
        name += "_" + shard.replace(":", "_")
    work = Path(tempfile.mkdtemp(prefix=f"servicemind-security-{name}-"))
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(work), "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    for path in changed:
        if path.startswith(("docs/", "evaluation/")):
            continue
        source, target = root / path, work / path
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        elif target.is_file():
            target.unlink()
    before = source_revision(work)
    assert before == revision, (before, revision)
    env = {**os.environ, "PYTHONPATH": f"{work}/src:{work}/scripts"}
    try:
        command = [sys.executable, script]
        if shard:
            start, stop = (int(value) for value in shard.split(":"))
            shim = (
                "import mutation_harness,runpy,sys\n"
                "original=mutation_harness.run_mutations\n"
                "def selected(**kwargs):\n"
                f" kwargs['mutations']=kwargs['mutations'][{start}:{stop}]\n"
                f" kwargs['experiment']+='_{start}_{stop}'\n"
                " return original(**kwargs)\n"
                "mutation_harness.run_mutations=selected\n"
                "runpy.run_path(sys.argv[1],run_name='__main__')\n"
            )
            command = [sys.executable, "-c", shim, script]
        process = subprocess.run(
            command,
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=7200,
        )
        after = source_revision(work)
        (args.output / f"{name}.log").write_text(process.stdout + process.stderr)
        runtime = work / ".runtime"
        for evidence in runtime.glob("mutate_*.evidence.json"):
            shutil.copy2(evidence, args.output / evidence.name)
        summary = {
            "script": script,
            "shard": shard,
            "before_revision": before,
            "after_revision": after,
            "exit_code": process.returncode,
        }
        (args.output / f"{name}.result.json").write_text(json.dumps(summary, indent=2) + "\n")
        assert before == after, f"mutation did not restore {work}"
        return summary
    finally:
        # Keep the isolated worktree for inspection if a failure interrupted restoration.
        if source_revision(work) == revision:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(work)],
                cwd=root,
                check=True,
                capture_output=True,
            )


with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
    for summary in pool.map(run, args.scripts):
        print(json.dumps(summary), flush=True)
