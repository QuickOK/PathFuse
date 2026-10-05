#!/usr/bin/env python3
"""Type-check gate: pyright and mypy over every tracked Python file, against a baseline.

pyright runs in the "basic" mode of pyrightconfig.json; mypy runs with mypy.ini
(check_untyped_defs). A file may not have more errors from either tool than
scripts/typecheck-baseline.json records for it, and a file the baseline does
not list must have none. Fixing errors only lowers the counts: afterwards run
`scripts/typecheck.py --update-baseline` to record the new floor.

Exit 0 when every file is at or under its baseline, 1 when one is above it,
2 when there is no verdict to give: a checker is missing, crashes or hangs, git
cannot list the files, or the baseline is not the JSON --update-baseline writes.
Each checker and git run with a time limit, so a hung tool cannot stall preflight.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "scripts/typecheck-baseline.json"
MYPY_LINE = re.compile(r"^(?P<file>[^:]+):\d+(?::\d+)?: error:")
GIT_TIMEOUT_S = 60        # `git ls-files` takes well under a second
CHECKER_TIMEOUT_S = 900   # each of pyright and mypy, over the whole repo with a cold cache


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run a tool in the repo root and capture its output.

    RuntimeError, naming the tool, if it cannot be started or hangs.
    """
    try:
        return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{argv[0]} timed out after {timeout:g} s") from None
    except OSError as e:   # not installed, or not executable
        raise RuntimeError(f"{argv[0]} could not be run: {e}") from None


def python_files() -> list[str]:
    """Tracked *.py files plus extensionless scripts with a python shebang.

    A tracked file missing from the working tree (deleted, deletion not yet
    staged) is skipped: there is nothing left to check.
    """
    r = _run(["git", "ls-files", "-z"], GIT_TIMEOUT_S)
    if r.returncode != 0:
        raise RuntimeError(f"git ls-files failed (rc={r.returncode}): {r.stderr.strip()[:300]}")
    files = []
    for f in r.stdout.split("\0"):
        p = ROOT / f
        if not p.is_file():   # also drops the empty name after the last NUL: ROOT / "" is ROOT
            continue
        if f.endswith(".py"):
            files.append(f)
        elif "." not in p.name:
            with p.open("rb") as fh:
                first = fh.readline()
            if first.startswith(b"#!") and b"python" in first:
                files.append(f)
    return sorted(files)


def _interpreter() -> str:
    venv = ROOT / ".venv/bin/python"
    return str(venv) if venv.exists() else sys.executable


def pyright_counts(files: list[str], py: str) -> Counter[str]:
    r = _run(["pyright", "--outputjson", "--pythonpath", py, *files], CHECKER_TIMEOUT_S)
    if r.returncode not in (0, 1):
        raise RuntimeError(f"pyright failed (rc={r.returncode}): {r.stderr.strip()[:300]}")
    report = json.loads(r.stdout)
    # pyright silently skips a named file that lies under an `exclude` of pyrightconfig.json.
    analyzed = report.get("summary", {}).get("filesAnalyzed")
    if analyzed != len(files):
        raise RuntimeError(f"pyright checked {analyzed} of {len(files)} files; is one of them "
                           f"under an exclude in pyrightconfig.json?")
    counts: Counter[str] = Counter()
    for d in report.get("generalDiagnostics", []):
        if d.get("severity") == "error":
            counts[str(Path(d["file"]).resolve().relative_to(ROOT))] += 1
    return counts


def mypy_counts(files: list[str], py: str) -> Counter[str]:
    r = _run(["mypy", "--python-executable", py, "--no-error-summary", *files],
             CHECKER_TIMEOUT_S)
    if r.returncode not in (0, 1):
        raise RuntimeError(f"mypy failed (rc={r.returncode}): "
                           f"{(r.stderr or r.stdout).strip()[:300]}")
    counts: Counter[str] = Counter()
    for line in r.stdout.splitlines():
        m = MYPY_LINE.match(line)
        if m:
            counts[m.group("file")] += 1
    return counts


def load_baseline(path: Path) -> dict[str, dict[str, int]]:
    """The per-file counts recorded at `path`, or {} when there is no file.

    ValueError, naming the file, when it is not the {file: {tool: count}} JSON
    that --update-baseline writes.
    """
    if not path.exists():
        return {}
    try:
        baseline = json.loads(path.read_text())
    except ValueError as e:   # UnicodeDecodeError is a ValueError too
        raise ValueError(f"{path} is not valid JSON ({e}); restore it from git") from None
    if not (isinstance(baseline, dict) and all(
            isinstance(row, dict) and all(type(n) is int and n >= 0 for n in row.values())
            for row in baseline.values())):
        raise ValueError(f"{path} is not a {{file: {{tool: error count}}}} map; "
                         f"restore it from git")
    return baseline


def regressions(current: dict, baseline: dict) -> list[str]:
    """Files whose error count from a tool exceeds the baseline (0 when unlisted)."""
    out = []
    for f in sorted(current):
        for tool in sorted(current[f]):
            allowed = baseline.get(f, {}).get(tool, 0)
            if current[f][tool] > allowed:
                out.append(f"{f}: {tool} {current[f][tool]} errors (baseline {allowed})")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="pyright + mypy gate against a per-file baseline")
    ap.add_argument("--update-baseline", action="store_true",
                    help="record the current per-file counts as the new baseline")
    args = ap.parse_args(argv)
    for tool in ("pyright", "mypy"):
        if shutil.which(tool) is None:
            print(f"typecheck: {tool} is not installed (see MAINTAINING.md)", file=sys.stderr)
            return 2
    try:
        # Read first, so a broken baseline costs no run of the checkers. Re-recording
        # does not read it, so a broken one cannot block that.
        baseline = {} if args.update_baseline else load_baseline(BASELINE)
        files, py = python_files(), _interpreter()
        by_tool = {"pyright": pyright_counts(files, py), "mypy": mypy_counts(files, py)}
    except (RuntimeError, ValueError) as e:
        print(f"typecheck: {e}", file=sys.stderr)
        return 2
    current = {}
    for f in files:
        row = {t: c[f] for t, c in by_tool.items() if c[f]}
        if row:
            current[f] = row
    total = sum(sum(v.values()) for v in current.values())
    if args.update_baseline:
        BASELINE.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"typecheck: baseline written ({total} errors in {len(current)} files)")
        return 0
    worse = regressions(current, baseline)
    if worse:
        print("TYPECHECK FAILED: errors above the baseline (run pyright/mypy on these files):",
              file=sys.stderr)
        for line in worse:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"TYPECHECK OK ({len(files)} files; {total} baseline errors left in "
          f"{len(current)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
