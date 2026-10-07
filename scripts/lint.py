#!/usr/bin/env python3
"""Lint gate: ShellCheck over every tracked shell script, ESLint over the tracked JavaScript.

Shell scripts are the tracked *.sh files plus extensionless files whose first line is
a sh or bash shebang. JavaScript is the tracked *.js and *.mjs files, except ui/vendor/
(verbatim upstream releases) and anything under a node_modules/. ShellCheck runs at its
default severity; ESLint is the one `npm ci` pins in node_modules/, never one on PATH
(Debian's cannot parse `??` or `?.`), with eslint.config.mjs. There is no baseline:
every finding fails the gate, an ESLint warning included.

Exit 0 when neither tool finds anything, 1 when either does, 2 when there is no
verdict to give: a tool is missing, crashes or hangs, or git cannot list the files.
Each tool and git run with a time limit, so a hung tool cannot stall preflight.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ESLINT = ROOT / "node_modules/.bin/eslint"
GIT_TIMEOUT_S = 60     # `git ls-files` takes well under a second
LINT_TIMEOUT_S = 300   # each of shellcheck and eslint, which take a few seconds


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run a tool in the repo root and capture its output.

    RuntimeError, naming the tool, if it cannot be started or hangs.
    """
    tool = Path(argv[0]).name
    try:
        return subprocess.run(argv, cwd=ROOT, capture_output=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{tool} timed out after {timeout:g} s") from None
    except OSError as e:   # not installed, or not executable
        raise RuntimeError(f"{tool} could not be run: {e}") from None


def is_shell_shebang(first: bytes) -> bool:
    """Whether a file's first line runs it with sh or bash.

    `#!/bin/sh`, `#! /bin/bash -e` and `#!/usr/bin/env -S bash -e` do; the interpreter
    is the program the line names (after env and env's options), not any word with
    "sh" in it.
    """
    if not first.startswith(b"#!"):
        return False
    words = first[2:].split()
    if words and words[0].rsplit(b"/", 1)[-1] == b"env":
        words = [w for w in words[1:] if not w.startswith(b"-")]
    return bool(words) and words[0].rsplit(b"/", 1)[-1] in (b"sh", b"bash")


def lint_files() -> tuple[list[str], list[str]]:
    """The tracked shell scripts and the tracked JavaScript files, each list sorted.

    A tracked file missing from the working tree (deleted, deletion not yet staged)
    is skipped: there is nothing left to check.
    """
    r = _run(["git", "ls-files", "-z"], GIT_TIMEOUT_S)
    if r.returncode != 0:
        raise RuntimeError(f"git ls-files failed (rc={r.returncode}): {r.stderr.strip()[:300]}")
    shell: list[str] = []
    js: list[str] = []
    for f in r.stdout.split("\0"):
        p = ROOT / f
        if not p.is_file():   # also drops the empty name after the last NUL: ROOT / "" is ROOT
            continue
        if f.endswith((".js", ".mjs")):
            if not f.startswith("ui/vendor/") and "node_modules" not in f.split("/"):
                js.append(f)
        elif f.endswith(".sh"):
            shell.append(f)
        elif "." not in p.name:
            with p.open("rb") as fh:
                if is_shell_shebang(fh.readline()):
                    shell.append(f)
    return sorted(shell), sorted(js)


def _findings(argv: list[str]) -> str:
    """What a linter reports: "" when it exits 0, its output when it exits 1 with some.

    RuntimeError, naming the tool, for any other exit (a crash or a usage error), or an
    exit 1 that shows nothing.
    """
    r = _run(argv, LINT_TIMEOUT_S)
    if r.returncode == 0:
        return ""
    if r.returncode == 1 and r.stdout.strip():
        return r.stdout
    detail = (r.stderr or r.stdout).strip()[:300]
    raise RuntimeError(f"{Path(argv[0]).name} failed (rc={r.returncode}): {detail or 'no output'}")


def shellcheck_report(files: list[str]) -> str:
    """ShellCheck's findings in `files`, one gcc-style line each; "" when it has none."""
    return _findings(["shellcheck", "-f", "gcc", *files]) if files else ""


def eslint_report(files: list[str]) -> str:
    """ESLint's findings in `files`; "" when it has none.

    A warning is a finding too: --max-warnings 0 makes ESLint exit 1 on one, where it
    would otherwise print it and exit 0.
    """
    return _findings([str(ESLINT), "--max-warnings", "0", *files]) if files else ""


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        description="ShellCheck + ESLint gate over the tracked shell scripts and JavaScript"
    ).parse_args(argv)
    if shutil.which("shellcheck") is None:
        print("lint: shellcheck is not installed (sudo apt install shellcheck; "
              "see MAINTAINING.md)", file=sys.stderr)
        return 2
    if shutil.which(str(ESLINT)) is None:
        print("lint: eslint is not installed in node_modules/ (run `npm ci` in the repo root; "
              "see MAINTAINING.md)", file=sys.stderr)
        return 2
    try:
        shell, js = lint_files()
        reports = {"shellcheck": shellcheck_report(shell), "eslint": eslint_report(js)}
    except RuntimeError as e:
        print(f"lint: {e}", file=sys.stderr)
        return 2
    failed = [tool for tool, report in reports.items() if report]
    if failed:
        for tool in failed:
            print(f"lint: {tool} reports:\n{reports[tool].rstrip()}", file=sys.stderr)
        print(f"LINT FAILED: findings from {' and '.join(failed)} (above); fix each one, "
              f"the lint gate has no baseline", file=sys.stderr)
        return 1
    print(f"LINT OK ({_count(len(shell), 'shell script')}, {_count(len(js), 'JavaScript file')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
