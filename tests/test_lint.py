"""Tests for the lint gate (scripts/lint.py) and the ESLint config it runs with."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts/lint.py"
_loader = SourceFileLoader("lint", str(_PATH))
_spec = importlib.util.spec_from_loader("lint", _loader)
assert _spec is not None
L = importlib.util.module_from_spec(_spec)
_loader.exec_module(L)

ESLINT: Path = L.ESLINT   # the pinned one in node_modules/, whatever ROOT a test sets
REPO = Path(__file__).resolve().parent.parent   # this checkout, with eslint.config.mjs
SC_FINDING = ("a.sh:3:1: warning: x appears unused. Verify use (or export if used "
              "externally). [SC2034]\n")
ES_FINDING = ("\n/repo/ui/app.js\n  1:5  error  'x' is assigned a value but never used  "
              "no-unused-vars\n\n✖ 1 problem (1 error, 0 warnings)\n\n")
FAILED = "LINT FAILED: findings from {} (above); fix each one, the lint gate has no baseline\n"


@pytest.mark.parametrize("first, is_shell", [
    (b"#!/bin/sh\n", True),
    (b"#!/bin/bash\n", True),
    (b"#!/usr/bin/env bash\n", True),
    (b"#!/usr/bin/env sh\n", True),
    (b"#! /bin/sh -eu\n", True),
    (b"#!/usr/bin/env -S bash -e\n", True),
    (b"#!/bin/bash\r\n", True),
    (b"#!/usr/bin/env python3\n", False),
    (b"#!/bin/zsh\n", False),
    (b"#!/usr/bin/env fish\n", False),
    (b"#!/bin/bashful\n", False),
    (b"#!/opt/sh/bin/python3\n", False),
    (b"#!/usr/bin/env\n", False),
    (b"# bash helpers\n", False),
    (b"", False),
], ids=["sh", "bash", "env-bash", "env-sh", "space-and-flags", "env-options", "crlf", "python",
        "zsh", "fish", "bash-prefix", "sh-directory", "env-alone", "comment", "empty"])
def test_is_shell_shebang(first: bytes, is_shell: bool) -> None:
    # sh and bash, the shells the repo's scripts use; the interpreter is the program the
    # line names (after env and env's options), not a word that merely contains "sh".
    assert L.is_shell_shebang(first) is is_shell


def _fake_git(monkeypatch: pytest.MonkeyPatch, root: Path, listed: list[str]) -> None:
    """`git ls-files -z`, run in `root`, lists `listed`."""
    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        assert argv == ["git", "ls-files", "-z"] and kw.get("cwd") == root
        return subprocess.CompletedProcess(argv, 0, "".join(f + "\0" for f in listed), "")

    monkeypatch.setattr(L, "ROOT", root)
    monkeypatch.setattr(L, "subprocess", SimpleNamespace(run=run))


def test_lint_files_sorts_tracked_files_into_shell_scripts_and_javascript(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = {
        "deploy/install.sh": "set -e\n",              # *.sh: a shell script, shebang or not
        "deploy/a b.sh": "#!/bin/sh\n",
        "tools/hook": "#!/bin/sh\n",                  # extensionless, a shell shebang
        "tools/env-hook": "#!/usr/bin/env bash\n",
        "tools/py-tool": "#!/usr/bin/env python3\n",  # extensionless, another language
        "tools/NOTES": "bash and sh notes\n",         # extensionless, no shebang
        "tools/setup.cfg": "#!/bin/sh\n",             # a shebang, but an extension
        "ui/app.js": "", "eslint.config.mjs": "", "tests/js/t.js": "", "ui/app.css": "",
        "ui/vendor/leaflet.js": "",                   # a verbatim upstream release
        "node_modules/eslint/lib/api.js": "",         # dependencies, force-added
        "tools/web/node_modules/x/index.js": "",
    }
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    # gone.sh and gone.js are still in the index but deleted from disk (not yet staged).
    _fake_git(monkeypatch, tmp_path, [*files, "gone.sh", "gone.js"])
    assert L.lint_files() == (
        ["deploy/a b.sh", "deploy/install.sh", "tools/env-hook", "tools/hook"],
        ["eslint.config.mjs", "tests/js/t.js", "ui/app.js"])


def test_lint_files_on_this_repo() -> None:
    shell, js = L.lint_files()
    assert {"scripts/preflight.sh", "deploy/wizard.sh", "scripts/hooks/pre-push",
            "deploy/spool-notify/spool-notify", "deploy/ntfy-control/ntfy-dispatch"} <= set(shell)
    assert "deploy/relay/egress/relay-egress-watchdog" not in shell   # a python shebang
    assert {"ui/app.js", "ui/map.js", "tests/js/test_kpi_exit.js", "eslint.config.mjs"} <= set(js)
    assert [f for f in js if f.startswith("ui/vendor/")] == []


def _found(cmd: str) -> str:
    return cmd


def _stub_gate(monkeypatch: pytest.MonkeyPatch,
               outcomes: dict[str, tuple[int, str, str]] | None = None,
               shell: tuple[str, ...] = ("a.sh", "hook"),
               js: tuple[str, ...] = ("ui/app.js",)) -> list[tuple[list[str], dict[str, Any]]]:
    """main() over canned file lists, with both tools installed and each tool's run
    answered from `outcomes` (its name -> (rc, stdout, stderr); a clean run by default).

    Returns the runs main() made, as (argv, keyword arguments)."""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kw))
        rc, out, err = (outcomes or {}).get(Path(argv[0]).name, (0, "", ""))
        return subprocess.CompletedProcess(argv, rc, out, err)

    def lint_files() -> tuple[list[str], list[str]]:
        return list(shell), list(js)

    monkeypatch.setattr(L, "shutil", SimpleNamespace(which=_found))
    monkeypatch.setattr(L, "lint_files", lint_files)
    monkeypatch.setattr(L, "subprocess", SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired))
    return calls


def test_main_passes_when_neither_tool_finds_anything_and_runs_both(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls = _stub_gate(monkeypatch)
    assert L.main([]) == 0
    assert capsys.readouterr() == ("LINT OK (2 shell scripts, 1 JavaScript file)\n", "")
    # Both run in the repo root, where eslint.config.mjs is, each with its time limit. A
    # warning from ESLint counts as a finding: --max-warnings 0 makes it exit 1.
    assert [(argv, kw["cwd"], kw["timeout"]) for argv, kw in calls] == [
        (["shellcheck", "-f", "gcc", "a.sh", "hook"], L.ROOT, 300),
        ([str(ESLINT), "--max-warnings", "0", "ui/app.js"], L.ROOT, 300)]


@pytest.mark.parametrize("tool, finding", [("shellcheck", SC_FINDING), ("eslint", ES_FINDING)],
                         ids=["shellcheck", "eslint"])
def test_main_fails_on_a_finding_from_either_tool(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        tool: str, finding: str) -> None:
    calls = _stub_gate(monkeypatch, {tool: (1, finding, "")})
    assert L.main([]) == 1
    assert capsys.readouterr() == (
        "", f"lint: {tool} reports:\n{finding.rstrip()}\n" + FAILED.format(tool))
    assert len(calls) == 2   # the other tool ran all the same


def test_main_prints_the_findings_of_both_tools(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _stub_gate(monkeypatch, {"shellcheck": (1, SC_FINDING, ""), "eslint": (1, ES_FINDING, "")})
    assert L.main([]) == 1
    assert capsys.readouterr().err == (f"lint: shellcheck reports:\n{SC_FINDING.rstrip()}\n"
                                       f"lint: eslint reports:\n{ES_FINDING.rstrip()}\n"
                                       + FAILED.format("shellcheck and eslint"))


@pytest.mark.parametrize("shell, js, ran, ok", [
    ((), ("ui/app.js",), ["eslint"], "LINT OK (0 shell scripts, 1 JavaScript file)\n"),
    (("a.sh",), (), ["shellcheck"], "LINT OK (1 shell script, 0 JavaScript files)\n"),
], ids=["no-shell-scripts", "no-javascript"])
def test_a_tool_with_no_files_to_check_is_not_run(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        shell: tuple[str, ...], js: tuple[str, ...], ran: list[str], ok: str) -> None:
    # Named no files, eslint lints the whole working directory and shellcheck prints
    # its usage and fails.
    calls = _stub_gate(monkeypatch, shell=shell, js=js)
    assert L.main([]) == 0
    assert [Path(argv[0]).name for argv, _kw in calls] == ran
    assert capsys.readouterr().out == ok


@pytest.mark.parametrize("tool, result, msg", [
    ("shellcheck", (2, SC_FINDING,
                    "gone.sh: openBinaryFile: does not exist (No such file or directory)\n"),
     "shellcheck failed (rc=2): gone.sh: openBinaryFile: does not exist "
     "(No such file or directory)"),
    ("shellcheck", (3, "", "unrecognized option `--bogus'\n"),
     "shellcheck failed (rc=3): unrecognized option `--bogus'"),
    ("eslint", (2, "", "\nOops! Something went wrong! :(\n\nESLint: 10.12.0\n"),
     "eslint failed (rc=2): Oops! Something went wrong! :(\n\nESLint: 10.12.0"),
    ("eslint", (127, "", "/usr/bin/env: 'node': No such file or directory\n"),
     "eslint failed (rc=127): /usr/bin/env: 'node': No such file or directory"),
    ("eslint", (1, "", ""), "eslint failed (rc=1): no output"),
    ("shellcheck", (1, "\n", ""), "shellcheck failed (rc=1): no output"),
], ids=["shellcheck-unreadable-file", "shellcheck-bad-option", "eslint-crash", "eslint-no-node",
        "eslint-1-silent", "shellcheck-1-blank"])
def test_main_exits_2_when_a_tool_gives_no_verdict(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        tool: str, result: tuple[int, str, str], msg: str) -> None:
    """A crash, a usage error, a file the tool could not read (with findings in the others
    or not), or a "found something" exit that shows nothing: one line naming the tool, not
    a pass and not a list of findings that is not the whole list."""
    _stub_gate(monkeypatch, {tool: result})
    assert L.main([]) == 2
    assert capsys.readouterr() == ("", f"lint: {msg}\n")


@pytest.mark.parametrize("missing, advice", [
    ("shellcheck", "(sudo apt install shellcheck; see MAINTAINING.md)"),
    ("eslint", "in node_modules/ (run `npm ci` in the repo root; see MAINTAINING.md)"),
], ids=["shellcheck", "eslint"])
def test_main_exits_2_and_says_how_to_install_a_missing_tool(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        missing: str, advice: str) -> None:
    """Checked before anything runs: half a gate gives no verdict. Only the pinned ESLint
    counts; one on PATH (Debian's cannot parse `??` or `?.`) is not used."""
    ran, bin_dir = tmp_path / "ran", tmp_path / "bin"
    pinned = {"shellcheck": bin_dir / "shellcheck", "eslint": tmp_path / "node_modules/.bin/eslint"}
    # Stand-ins that record a run: every tool but the missing one, and an eslint on PATH.
    for path in [p for tool, p in pinned.items() if tool != missing] + [bin_dir / "eslint"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\necho {path.name} >> "{ran}"\n')
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(L, "ESLINT", pinned["eslint"])
    assert L.main([]) == 2
    assert capsys.readouterr() == ("", f"lint: {missing} is not installed {advice}\n")
    assert not ran.exists()


@pytest.mark.parametrize("tool", ["git", "shellcheck", "eslint"])
def test_main_exits_2_and_names_a_tool_that_hangs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        tool: str) -> None:
    """Each tool runs with a time limit, so a hung one cannot stall preflight."""
    order, limit = ["git", "shellcheck", "eslint"], {"git": 60, "shellcheck": 300, "eslint": 300}
    (tmp_path / "a.sh").write_text("")
    (tmp_path / "a.js").write_text("")
    limits: dict[str, float] = {}

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        name = Path(argv[0]).name
        limits[name] = kw["timeout"]
        if name == tool:
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        return subprocess.CompletedProcess(argv, 0, "a.sh\0a.js\0" if name == "git" else "", "")

    monkeypatch.setattr(L, "ROOT", tmp_path)
    monkeypatch.setattr(L, "shutil", SimpleNamespace(which=_found))
    monkeypatch.setattr(L, "subprocess", SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired))
    assert L.main([]) == 2
    assert capsys.readouterr().err == f"lint: {tool} timed out after {limit[tool]} s\n"
    # Every tool that ran had its limit, the hung one included.
    assert limits == {t: limit[t] for t in order[:order.index(tool) + 1]}


def test_main_exits_2_when_git_cannot_list_the_files(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Outside a work tree `git ls-files` exits 128: one line naming git, not a traceback."""
    stderr = "fatal: not a git repository (or any of the parent directories): .git\n"

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 128, "", stderr)

    monkeypatch.setattr(L, "shutil", SimpleNamespace(which=_found))
    monkeypatch.setattr(L, "subprocess", SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired))
    assert L.main([]) == 2
    assert capsys.readouterr().err == f"lint: git ls-files failed (rc=128): {stderr}"


def test_main_exits_2_when_git_cannot_be_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """With no git on PATH, subprocess raises FileNotFoundError: one line, not a traceback."""
    monkeypatch.setattr(L, "shutil", SimpleNamespace(which=_found))
    monkeypatch.setenv("PATH", str(tmp_path / "no-such-dir"))
    assert L.main([]) == 2
    assert capsys.readouterr().err == (
        "lint: git could not be run: [Errno 2] No such file or directory: 'git'\n")


def _one_js_file() -> tuple[list[str], list[str]]:
    return [], ["ui/app.js"]


@pytest.mark.parametrize("tool", ["git", "eslint"])
def test_main_exits_2_when_a_tool_cannot_be_executed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str], tool: str) -> None:
    """A git on PATH with no execute bit raises PermissionError, and an eslint that is no
    program at all (no #!, not a binary) raises OSError ENOEXEC: OSErrors that are not
    FileNotFoundError. Each is one line naming the tool, exit 2, not a traceback."""
    monkeypatch.setattr(L, "shutil", SimpleNamespace(which=_found))
    if tool == "git":
        git = tmp_path / "bin/git"
        git.parent.mkdir()
        git.write_text("#!/bin/sh\nexit 0\n")
        git.chmod(0o644)   # no execute bit at all, so execve refuses it, root included
        monkeypatch.setenv("PATH", str(git.parent))
        want = "lint: git could not be run: [Errno 13] Permission denied: 'git'\n"
    else:
        eslint = tmp_path / "node_modules/.bin/eslint"
        eslint.parent.mkdir(parents=True)
        eslint.write_text("not a program\n")
        eslint.chmod(0o755)
        monkeypatch.setattr(L, "ESLINT", eslint)
        monkeypatch.setattr(L, "lint_files", _one_js_file)   # shellcheck has nothing to run
        want = f"lint: eslint could not be run: [Errno 8] Exec format error: '{eslint}'\n"
    assert L.main([]) == 2
    assert capsys.readouterr() == ("", want)


def test_main_runs_the_tools_in_the_repo_root_end_to_end(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """Real git and real processes, with stand-ins for the two linters that record where
    and how they were run."""
    repo, bin_dir, log = tmp_path / "repo", tmp_path / "bin", tmp_path / "calls"
    for name, text in [("run.sh", ""), ("hook", "#!/bin/bash\n"), ("ui/app.js", ""),
                       ("README.md", "")]:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    bin_dir.mkdir()
    for name, script in [("shellcheck", "echo 'run.sh:1:1: note: a stand-in finding [SC0000]'"
                                        "\nexit 1"),
                         ("eslint", "exit 0")]:
        (bin_dir / name).write_text(f'#!/bin/sh\necho "{name} $(pwd -P) $*" >> "{log}"\n{script}\n')
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(L, "ROOT", repo)
    monkeypatch.setattr(L, "ESLINT", bin_dir / "eslint")
    assert L.main([]) == 1
    where = repo.resolve()
    assert log.read_text().splitlines() == [f"shellcheck {where} -f gcc hook run.sh",
                                            f"eslint {where} --max-warnings 0 ui/app.js"]
    assert capsys.readouterr().err == (
        "lint: shellcheck reports:\nrun.sh:1:1: note: a stand-in finding [SC0000]\n"
        + FAILED.format("shellcheck"))


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_shellcheck_report_from_the_real_tool(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ShellCheck exits 0 when it finds nothing and 1 with one gcc-style line per finding."""
    (tmp_path / "clean.sh").write_text('#!/bin/sh\necho "$1"\n')
    (tmp_path / "unused.sh").write_text("#!/bin/sh\nx=1\n")
    monkeypatch.setattr(L, "ROOT", tmp_path)
    assert L.shellcheck_report(["clean.sh"]) == ""
    report = L.shellcheck_report(["clean.sh", "unused.sh"])
    assert report.startswith("unused.sh:2:1: warning: ") and report.endswith(" [SC2034]\n")
    assert report.count("\n") == 1


@pytest.mark.skipif(not ESLINT.exists(), reason="eslint is not installed (npm ci)")
def test_eslint_report_from_the_real_tool_counts_a_warning(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pinned ESLint exits 0 when it finds nothing and 1 on an error, and on a warning
    only because the gate passes --max-warnings 0 (a warning alone exits 0)."""
    (tmp_path / "clean.js").write_text("let used = 1;\nconsole.log(used);\n")
    (tmp_path / "unused.js").write_text("let unused = 1;\n")
    (tmp_path / "unused-warn.js").write_text("let unused = 1;\n")
    (tmp_path / "eslint.config.mjs").write_text(
        'export default [{ rules: { "no-unused-vars": "error" } },\n'
        '  { files: ["unused-warn.js"], rules: { "no-unused-vars": "warn" } }];\n')
    monkeypatch.setattr(L, "ROOT", tmp_path)
    assert L.eslint_report(["clean.js"]) == ""
    for name, level in [("unused.js", "error"), ("unused-warn.js", "warning")]:
        report = L.eslint_report(["clean.js", name])
        assert f"{tmp_path / name}\n  1:5  {level}  'unused' is assigned a value but never used" \
            in report, report


# eslint.config.mjs itself: the pinned ESLint, run in the repo root as the gate runs it, on
# probes read from stdin as if they sat at the given paths. Nothing else checks the config:
# one that lost the recommended rules would let the gate pass any JavaScript at all.
# ui/: Leaflet's L and the browser globals are known, ES2020's ?? parses, and the
# recommended rules hold: an unused variable, an unused catch binding, an empty block.
UI_PROBE = ('"use strict";\n'
            'const map = L.map("m");\n'
            'document.title = map.getContainer().id ?? localStorage.getItem("k");\n'
            'const unused = 1;\n'
            'try { sessionStorage.clear(); } catch (e) {}\n')
# tests/js/: CommonJS under node, with node's globals.
NODE_PROBE = ('const fs = require("fs");\n'
              'module.exports = { fs, here: __dirname, argv: process.argv };\n')


def _lint_stdin(path: str, source: str) -> list[tuple[str | None, str]]:
    """(rule id, message) for each problem ESLint reports in `source` read as `path`."""
    r = subprocess.run([str(ESLINT), "--format", "json", "--stdin", "--stdin-filename", path],
                       cwd=REPO, input=source, capture_output=True, text=True, timeout=300)
    assert r.returncode in (0, 1), r.stderr
    results: list[dict[str, Any]] = json.loads(r.stdout)
    assert len(results) == 1, r.stdout
    messages: list[dict[str, Any]] = results[0]["messages"]
    return [(m["ruleId"], m["message"]) for m in messages]


@pytest.mark.skipif(not ESLINT.exists(), reason="eslint is not installed (npm ci)")
@pytest.mark.parametrize("path, source, rules", [
    ("ui/probe.js", UI_PROBE, ["no-empty", "no-unused-vars", "no-unused-vars"]),
    ("tests/js/probe.js", NODE_PROBE, []),
], ids=["ui", "tests-js"])
def test_repo_eslint_config_applies_the_recommended_rules_with_each_area_s_globals(
        path: str, source: str, rules: list[str]) -> None:
    problems = _lint_stdin(path, source)
    assert sorted(str(rule) for rule, _msg in problems) == rules, problems


@pytest.mark.skipif(not ESLINT.exists(), reason="eslint is not installed (npm ci)")
def test_repo_eslint_config_ignores_ui_vendor() -> None:
    # Verbatim upstream releases. The gate never names them; the config keeps them out when
    # ESLint runs another way (npx eslint ., an editor), and says so when one is named.
    problems = _lint_stdin("ui/vendor/probe.js", "let unused = 1;\n")
    assert len(problems) == 1 and problems[0][0] is None, problems
    assert problems[0][1].startswith("File ignored because of a matching ignore pattern"), problems


def test_preflight_runs_the_lint_gate() -> None:
    preflight = (Path(__file__).resolve().parent.parent / "scripts/preflight.sh").read_text()
    assert '\necho "== lint: shellcheck + eslint =="\n"$PY" scripts/lint.py || fail=1\n' \
        in preflight


def test_package_json_pins_each_dev_tool_and_the_lockfile_installs_those_versions() -> None:
    """A pin loosened to a range floats with the next `npm install`, and a lockfile out of
    step with the pins installs another version: the type baseline's counts are the pinned
    pyright's, so each dev tool is pinned to one exact version, and the lockfile matches."""
    pkg: dict[str, Any] = json.loads((REPO / "package.json").read_text())
    lock: dict[str, Any] = json.loads((REPO / "package-lock.json").read_text())
    dev: dict[str, str] = pkg["devDependencies"]
    assert pkg["private"] is True and "dependencies" not in pkg   # dev tools only, never published
    assert [v for v in dev.values() if not re.fullmatch(r"\d+\.\d+\.\d+", v)] == [], dev
    packages: dict[str, dict[str, Any]] = lock["packages"]
    assert packages[""]["devDependencies"] == dev   # the lockfile was made from these pins
    assert {name: packages[f"node_modules/{name}"]["version"] for name in dev} == dev


@pytest.mark.skipif(not ESLINT.exists(), reason="eslint is not installed (npm ci)")
@pytest.mark.parametrize("path, source, name", [
    ("ui/probe.js", '"use strict";\nrequire("fs");\n', "require"),   # node's, not the browser's
    ("tests/js/probe.js", "window.close();\n", "window"),            # the browser's, not node's
], ids=["ui", "tests-js"])
def test_repo_eslint_config_reports_a_name_the_area_does_not_define(
        path: str, source: str, name: str) -> None:
    """The negative control for the globals tests: with no-undef off, every name passes."""
    assert _lint_stdin(path, source) == [("no-undef", f"'{name}' is not defined.")]


@pytest.mark.skipif(not ESLINT.exists(), reason="eslint is not installed (npm ci)")
@pytest.mark.parametrize("path, source, problems", [
    ("ui/probe.js", "new (class { x = 1; })();\n", []),   # ES2022: a class field
    ("ui/probe.js", 'import "x";\n',                       # a classic script, not a module
     [(None, "Parsing error: 'import' and 'export' may appear only with 'sourceType: module'")]),
    ("tests/js/probe.js", "return;\n", []),               # CommonJS: a top-level return
], ids=["ui-es2022", "ui-script", "tests-js-commonjs"])
def test_repo_eslint_config_parses_each_area_as_its_runtime_does(
        path: str, source: str, problems: list[tuple[str | None, str]]) -> None:
    """ui/ runs as ES2022 classic scripts in the browser, tests/js/ as CommonJS in node."""
    assert _lint_stdin(path, source) == problems
