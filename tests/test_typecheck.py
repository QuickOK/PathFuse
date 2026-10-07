"""Tests for the type-check gate (scripts/typecheck.py)."""
import ast
import configparser
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tokenize
from collections import Counter
from collections.abc import Callable
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts/typecheck.py"
_loader = SourceFileLoader("typecheck", str(_PATH))
_spec = importlib.util.spec_from_loader("typecheck", _loader)
assert _spec is not None
T = importlib.util.module_from_spec(_spec)
_loader.exec_module(T)


def test_no_regression_when_at_or_under_baseline():
    base = {"a.py": {"pyright": 3, "mypy": 2}}
    assert T.regressions({"a.py": {"pyright": 3, "mypy": 1}}, base) == []
    assert T.regressions({}, base) == []


def test_regression_above_baseline_and_in_a_clean_file():
    base = {"a.py": {"pyright": 3}}
    out = T.regressions({"a.py": {"pyright": 4}, "b.py": {"mypy": 1}}, base)
    assert out == ["a.py: pyright 4 errors (baseline 3)", "b.py: mypy 1 errors (baseline 0)"]


def test_mypy_line_regex():
    m = T.MYPY_LINE.match("tests/test_x.py:12: error: Bad thing  [attr-defined]")
    assert m and m.group("file") == "tests/test_x.py"
    m = T.MYPY_LINE.match("deploy/relay/egress/relay-egress-watchdog:507:9: error: x")
    assert m and m.group("file") == "deploy/relay/egress/relay-egress-watchdog"
    assert T.MYPY_LINE.match("a.py:3: note: See https://...") is None


def test_mypy_line_regex_keeps_a_path_with_a_space():
    # python_files() passes such a path to mypy, which reports it verbatim.
    m = T.MYPY_LINE.match("tools/a b.py:1: error: Incompatible types  [assignment]")
    assert m and m.group("file") == "tools/a b.py"


def test_python_files_includes_extensionless_python_scripts():
    files = T.python_files()
    assert "sbfd_ctl.py" in files
    assert "deploy/relay/egress/relay-egress-watchdog" in files
    assert not any(f.endswith((".md", ".json", ".sh")) for f in files)


def test_python_files_keeps_names_with_spaces_and_skips_files_gone_from_disk(tmp_path, monkeypatch):
    for name, text in [("a b.py", ""), ("tool", "#!/usr/bin/env python3\n"),
                       ("hook", "#!/bin/sh\n"), ("NOTES", "python tools live here\n"),
                       ("notes.md", "")]:
        (tmp_path / name).write_text(text)
    # gone.py is still in the index but deleted from disk (the deletion not yet staged).
    listed = ["a b.py", "tool", "hook", "NOTES", "notes.md", "gone.py"]

    def run(argv, **kw):
        assert argv == ["git", "ls-files", "-z"]
        return subprocess.CompletedProcess(argv, 0, "".join(f + "\0" for f in listed), "")

    monkeypatch.setattr(T, "ROOT", tmp_path)
    monkeypatch.setattr(T, "subprocess", SimpleNamespace(run=run))
    assert T.python_files() == ["a b.py", "tool"]


@pytest.mark.skipif(shutil.which("mypy") is None, reason="mypy is not installed")
def test_mypy_checks_two_extensionless_scripts_in_one_run(tmp_path, monkeypatch):
    # mypy names every extensionless script "__main__" unless mypy.ini sets
    # scripts_are_modules, so two of them in one run abort it ("Duplicate module").
    monkeypatch.setenv("MYPY_CACHE_DIR", str(tmp_path / "mypy-cache"))
    files = []
    for name in ("tool-one", "tool-two"):
        p = tmp_path / name
        p.write_text('#!/usr/bin/env python3\nx: int = "not an int"\n')
        files.append(str(p))
    assert T.mypy_counts(files, sys.executable) == {files[0]: 1, files[1]: 1}


def _fake_run(monkeypatch, stdout, rc=1):
    """Replace the checker run; returns the list of (argv, cwd) it was called with."""
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw.get("cwd")))
        return subprocess.CompletedProcess(argv, rc, stdout, "")

    monkeypatch.setattr(T, "subprocess", SimpleNamespace(run=run))
    return calls


def test_pyright_counts_errors_per_repo_file(monkeypatch: pytest.MonkeyPatch) -> None:
    diags = [{"file": str(T.ROOT / f), "severity": sev} for f, sev in [
        ("a.py", "error"), ("a.py", "error"), ("a.py", "warning"), ("tools/b.py", "error")]]
    calls = _fake_run(monkeypatch, json.dumps({"generalDiagnostics": diags,
                                               "summary": {"filesAnalyzed": 2}}))
    assert T.pyright_counts(["a.py", "tools/b.py"], "/venv/python",
                            "/repo/node_modules/.bin/pyright") == {"a.py": 2, "tools/b.py": 1}
    # The pyright given runs; --outputjson makes its report parseable; cwd picks up
    # pyrightconfig.json.
    assert calls == [(["/repo/node_modules/.bin/pyright", "--outputjson", "--pythonpath",
                       "/venv/python", "a.py", "tools/b.py"], T.ROOT)]


def test_mypy_counts_errors_per_file_and_ignores_notes(monkeypatch):
    calls = _fake_run(monkeypatch, "a.py:1: error: x  [misc]\n"
                                   "a.py:1: note: See https://...\n"
                                   "a.py:7:3: error: y  [misc]\n"
                                   "b.py:2: error: z  [misc]\n")
    assert T.mypy_counts(["a.py", "b.py"], "/venv/python") == {"a.py": 2, "b.py": 1}
    # cwd picks up mypy.ini (scripts_are_modules, check_untyped_defs).
    assert calls == [(["mypy", "--python-executable", "/venv/python", "--no-error-summary",
                       "a.py", "b.py"], T.ROOT)]


@pytest.mark.parametrize("tool", ["pyright", "mypy"])
def test_counts_raise_when_the_checker_itself_fails(monkeypatch: pytest.MonkeyPatch,
                                                    tool: str) -> None:
    # Exit 2 is a crash or a usage error. mypy's "Duplicate module" lines carry no line
    # number, so read as counts they would pass every file as clean.
    _fake_run(monkeypatch, 'x: error: Duplicate module named "__main__"\n', rc=2)
    with pytest.raises(RuntimeError, match=rf"{tool} failed \(rc=2\)"):
        if tool == "pyright":
            T.pyright_counts(["a.py"], "python3", "pyright")
        else:
            T.mypy_counts(["a.py"], "python3")


_PYRIGHT = T.pyright_binary()   # the one the gate runs in this repo; None when there is none


@pytest.mark.skipif(_PYRIGHT is None, reason="pyright is not installed (npm ci)")
def test_pyright_counts_refuses_a_run_that_skipped_a_named_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # pyright silently skips a file named on its command line when the file lies under
    # an `exclude` of pyrightconfig.json; that file must not pass as clean.
    (tmp_path / "pyrightconfig.json").write_text('{"exclude": ["docs"]}\n')
    (tmp_path / "docs").mkdir()
    for f in ("top.py", "docs/x.py"):
        (tmp_path / f).write_text('x: int = "not an int"\n')
    monkeypatch.setattr(T, "ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="checked 1 of 2 files"):
        T.pyright_counts(["docs/x.py", "top.py"], sys.executable, _PYRIGHT)


def _stub_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pyright: dict[str, int],
               mypy: dict[str, int], baseline: dict[str, dict[str, int]] | None = None,
               files: tuple[str, ...] | None = ("a.py", "b.py", "c.py")) -> None:
    """main() over three files with canned counts and a baseline file in tmp_path.

    files=None keeps the real python_files(), and so the `git ls-files` it runs."""
    monkeypatch.setattr(T, "shutil", SimpleNamespace(which=lambda tool: f"/usr/bin/{tool}"))
    if files is not None:
        monkeypatch.setattr(T, "python_files", lambda: list(files))

    def pyright_counts(_files: list[str], _py: str, _pyright: str) -> Counter[str]:
        return Counter(pyright)

    def mypy_counts(_files: list[str], _py: str) -> Counter[str]:
        return Counter(mypy)

    monkeypatch.setattr(T, "pyright_counts", pyright_counts)
    monkeypatch.setattr(T, "mypy_counts", mypy_counts)
    monkeypatch.setattr(T, "BASELINE", tmp_path / "baseline.json")
    if baseline is not None:
        (tmp_path / "baseline.json").write_text(json.dumps(baseline))


def test_main_update_baseline_records_only_files_and_tools_with_errors(tmp_path, monkeypatch,
                                                                        capsys):
    _stub_gate(monkeypatch, tmp_path, pyright={"a.py": 2}, mypy={"a.py": 1, "b.py": 3})
    assert T.main(["--update-baseline"]) == 0
    assert json.loads((tmp_path / "baseline.json").read_text()) == {
        "a.py": {"pyright": 2, "mypy": 1}, "b.py": {"mypy": 3}}
    assert "baseline written (6 errors in 2 files)" in capsys.readouterr().out


def test_main_passes_at_or_under_the_baseline(tmp_path, monkeypatch, capsys):
    _stub_gate(monkeypatch, tmp_path, pyright={"a.py": 2}, mypy={"b.py": 1},
               baseline={"a.py": {"pyright": 2, "mypy": 1}, "b.py": {"mypy": 3}})
    assert T.main([]) == 0
    assert "TYPECHECK OK (3 files; 3 baseline errors left in 2 files)" in capsys.readouterr().out


@pytest.mark.parametrize("baseline", [{"a.py": {"pyright": 2}}, None], ids=["above", "no-file"])
def test_main_fails_above_the_baseline(tmp_path, monkeypatch, capsys, baseline):
    _stub_gate(monkeypatch, tmp_path, pyright={"a.py": 2, "c.py": 1}, mypy={},
               baseline=baseline)
    assert T.main([]) == 1
    err = capsys.readouterr().err
    assert "TYPECHECK FAILED" in err and "  c.py: pyright 1 errors (baseline 0)" in err


def _raise(exc: Exception) -> Callable[..., NoReturn]:
    def counts(*args: object) -> NoReturn:
        raise exc
    return counts


@pytest.mark.parametrize("missing, pyright, msg", [
    ("pyright", None, "pyright is not installed"),
    ("mypy", None, "mypy is not installed"),
    (None, _raise(RuntimeError("pyright failed (rc=3): bad config")), "pyright failed (rc=3)"),
    (None, _raise(ValueError("Expecting value")), "Expecting value"),
], ids=["no-pyright", "no-mypy", "crash", "bad-json"])
def test_main_exits_2_when_a_checker_is_missing_or_crashes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        missing: str | None, pyright: Callable[..., NoReturn] | None, msg: str) -> None:
    _stub_gate(monkeypatch, tmp_path, pyright={}, mypy={})

    def which(tool: str) -> str | None:   # the pinned pyright is "pyright" too
        return None if Path(tool).name == missing else f"/usr/bin/{tool}"

    monkeypatch.setattr(T, "shutil", SimpleNamespace(which=which))
    if pyright is not None:
        monkeypatch.setattr(T, "pyright_counts", pyright)
    assert T.main([]) == 2
    assert msg in capsys.readouterr().err


_CLEAN = json.dumps({"generalDiagnostics": [], "summary": {"filesAnalyzed": 1}})


def _stand_in(path: Path, log: Path, stdout: str = "") -> None:
    """An executable at `path` that appends the path it was run as to `log`, then prints
    `stdout` and exits 0."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho \"$0\" >> '{log}'\nprintf '%s' '{stdout}'\n")
    path.chmod(0o755)


def _gate_over_one_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                        pinned: bool, on_path: bool) -> tuple[Path, Path, Path]:
    """main() in a repo at tmp_path over one clean file, with real processes: stand-ins
    for the pinned pyright (when `pinned`), a pyright on PATH (when `on_path`) and mypy,
    PATH holding only their bin/.

    Returns the log each stand-in appends its path to when run, and the two pyrights'
    paths."""
    log, bin_dir = tmp_path / "ran", tmp_path / "bin"
    pinned_pyright, path_pyright = tmp_path / "node_modules/.bin/pyright", bin_dir / "pyright"
    for pyright, present in [(pinned_pyright, pinned), (path_pyright, on_path)]:
        if present:
            _stand_in(pyright, log, _CLEAN)
    _stand_in(bin_dir / "mypy", log)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(T, "ROOT", tmp_path)
    monkeypatch.setattr(T, "BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(T, "python_files", lambda: ["a.py"])
    return log, pinned_pyright, path_pyright


@pytest.mark.parametrize("pinned, on_path", [(True, True), (True, False), (False, True)],
                         ids=["pinned-and-path", "pinned-only", "path-only"])
def test_main_runs_the_pinned_pyright_else_the_one_on_path_and_says_which(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        pinned: bool, on_path: bool) -> None:
    """The pinned pyright (node_modules/.bin/, from `npm ci`) is the version the baseline
    was recorded with, so it wins over one on PATH; that one is the fallback, and the
    output names the one that ran."""
    log, pinned_pyright, path_pyright = _gate_over_one_file(tmp_path, monkeypatch,
                                                            pinned, on_path)
    assert T.main([]) == 0
    ran = pinned_pyright if pinned else path_pyright
    assert log.read_text().splitlines() == [str(ran), str(tmp_path / "bin/mypy")]
    says = ("typecheck: pyright is node_modules/.bin/pyright, the pinned one\n" if pinned else
            f"typecheck: pyright is {path_pyright} from PATH, not the pinned one "
            f"(run `npm ci`)\n")
    assert capsys.readouterr() == (
        says + "TYPECHECK OK (1 files; 0 baseline errors left in 0 files)\n", "")


def test_main_exits_2_and_names_npm_ci_when_there_is_no_pyright(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """Neither the pinned pyright nor one on PATH: no verdict, and nothing runs."""
    log, _pinned, _path = _gate_over_one_file(tmp_path, monkeypatch,
                                              pinned=False, on_path=False)
    assert T.main([]) == 2
    assert capsys.readouterr() == ("", "typecheck: pyright is not installed (run `npm ci` "
                                       "in the repo root; see MAINTAINING.md)\n")
    assert not log.exists()


@pytest.mark.parametrize("tool", ["git", "pyright", "mypy"])
def test_main_exits_2_and_names_a_tool_that_hangs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        tool: str) -> None:
    """Each tool runs with a time limit, so a hung one cannot stall preflight."""
    order, limit = ["git", "pyright", "mypy"], {"git": 60, "pyright": 900, "mypy": 900}
    (tmp_path / "a.py").write_text("")
    limits: dict[str, float | None] = {}

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        name = Path(argv[0]).name   # pyright runs by its path in node_modules/.bin/
        limits[name] = kw.get("timeout")
        if name == tool:
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        out = {"git": "a.py\0", "mypy": "",
               "pyright": json.dumps({"generalDiagnostics": [], "summary": {"filesAnalyzed": 1}})}
        return subprocess.CompletedProcess(argv, 0, out[name], "")

    monkeypatch.setattr(T, "ROOT", tmp_path)
    monkeypatch.setattr(T, "BASELINE", tmp_path / "baseline.json")   # main() reads it first
    monkeypatch.setattr(T, "shutil", SimpleNamespace(which=lambda t: f"/usr/bin/{t}"))
    monkeypatch.setattr(T, "subprocess", SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired))
    assert T.main([]) == 2
    assert capsys.readouterr().err == f"typecheck: {tool} timed out after {limit[tool]} s\n"
    # Every tool that ran had its limit, the hung one included.
    assert limits == {t: limit[t] for t in order[:order.index(tool) + 1]}


def test_main_exits_2_when_git_cannot_list_the_files(tmp_path, monkeypatch, capsys):
    """Outside a work tree `git ls-files` exits 128: one line naming git, not a traceback."""
    _stub_gate(monkeypatch, tmp_path, pyright={}, mypy={}, files=None)
    stderr = "fatal: not a git repository (or any of the parent directories): .git\n"

    def run(argv, **kw):
        r = subprocess.CompletedProcess(argv, 128, "", stderr)
        if kw.get("check"):
            r.check_returncode()   # what subprocess.run(check=True) does
        return r

    monkeypatch.setattr(T, "subprocess", SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired))
    assert T.main([]) == 2
    assert capsys.readouterr().err == f"typecheck: git ls-files failed (rc=128): {stderr}"


def test_main_exits_2_when_git_cannot_be_run(tmp_path, monkeypatch, capsys):
    """With no git on PATH, subprocess raises FileNotFoundError: one line, not a traceback."""
    _stub_gate(monkeypatch, tmp_path, pyright={}, mypy={}, files=None)
    monkeypatch.setenv("PATH", str(tmp_path / "no-such-dir"))
    assert T.main([]) == 2
    assert capsys.readouterr().err == (
        "typecheck: git could not be run: [Errno 2] No such file or directory: 'git'\n")


def test_main_exits_2_when_git_cannot_be_executed(tmp_path, monkeypatch, capsys):
    """A git on PATH with no execute bit raises PermissionError, an OSError that is not
    FileNotFoundError: one line naming git, not a traceback."""
    _stub_gate(monkeypatch, tmp_path, pyright={}, mypy={}, files=None)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text("#!/bin/sh\nexit 0\n")
    git.chmod(0o644)   # no execute bit at all, so execve refuses it, root included
    monkeypatch.setenv("PATH", str(bin_dir))
    assert T.main([]) == 2
    assert capsys.readouterr().err == (
        "typecheck: git could not be run: [Errno 13] Permission denied: 'git'\n")


@pytest.mark.parametrize("raw, problem", [
    (b'{"a.py": {"pyright": 2}', "is not valid JSON ("),
    (b"\xff\xfe", "is not valid JSON ("),
    (b'["a.py"]', "is not a {file: {tool: error count}} map"),
    (b'{"a.py": 2}', "is not a {file: {tool: error count}} map"),
    (b'{"a.py": {"pyright": "2"}}', "is not a {file: {tool: error count}} map"),
    (b'{"a.py": {"pyright": true}}', "is not a {file: {tool: error count}} map"),
    (b'{"a.py": {"pyright": -1}}', "is not a {file: {tool: error count}} map"),
], ids=["truncated", "not-utf8", "a-list", "row-a-number", "count-a-string", "count-a-bool",
        "count-negative"])
def test_main_exits_2_on_a_malformed_baseline(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        raw: bytes, problem: str) -> None:
    """One line naming the file, not a traceback, and not a verdict from counts that are
    not counts. The baseline is read first, so a broken one costs no checker run."""
    _stub_gate(monkeypatch, tmp_path, pyright={}, mypy={})
    ran: list[list[str]] = []

    def pyright_counts(files: list[str], py: str, pyright: str) -> Counter[str]:
        ran.append(files)
        return Counter({"a.py": 2})

    monkeypatch.setattr(T, "pyright_counts", pyright_counts)
    (tmp_path / "baseline.json").write_bytes(raw)
    assert T.main([]) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"typecheck: {tmp_path / 'baseline.json'} {problem}")
    assert err.endswith("; restore it from git\n") and err.count("\n") == 1
    assert ran == []


def test_main_update_baseline_replaces_a_malformed_baseline(tmp_path, monkeypatch):
    """Re-recording reads no baseline, so a broken one cannot block it."""
    _stub_gate(monkeypatch, tmp_path, pyright={"a.py": 2}, mypy={})
    (tmp_path / "baseline.json").write_text('{"a.py": {"pyright": 2}')
    assert T.main(["--update-baseline"]) == 0
    assert json.loads((tmp_path / "baseline.json").read_text()) == {"a.py": {"pyright": 2}}


def test_pyright_config_holds_only_strict_and_the_excludes() -> None:
    """The baseline holds strict counts and the ratchet only stops counts rising, so
    nothing in the config may lower a file: not the mode, not "ignore" (pyright still
    counts an ignored file as analyzed, with no errors), not a rule set at the top level
    or per execution environment. `# type: ignore` stays switched off. Excludes are
    safe: the gate refuses a run that skips a file it names."""
    config = json.loads((T.ROOT / "pyrightconfig.json").read_text())
    rest = {k: v for k, v in config.items() if k != "exclude"}
    assert rest == {"typeCheckingMode": "strict", "enableTypeIgnoreComments": False}


def test_mypy_config_lowers_no_file() -> None:
    """mypy.ini holds the settings the baseline's mypy counts were taken with, and no
    section for one module: one could switch a file's errors off (`ignore_errors`), or
    its untyped functions' checks, and the ratchet would pass the drop. Its exclude
    is safe: mypy checks a file named on its command line whatever the exclude says."""
    config = configparser.ConfigParser()
    assert config.read(T.ROOT / "mypy.ini") == [str(T.ROOT / "mypy.ini")]
    assert config.sections() == ["mypy"]
    assert {k: v for k, v in config["mypy"].items() if k != "exclude"} == {
        "check_untyped_defs": "True", "ignore_missing_imports": "True",
        "scripts_are_modules": "True"}


def test_no_tracked_stub_shadows_a_module() -> None:
    """A stub (`*.pyi`, in typings/, pyright's stub path, or beside a module) changes
    what the checkers see of a module in every file that imports it, and the gate
    checks no stub, so one could hide errors in those files from the ratchet. Adding
    one is a deliberate change here."""
    r = subprocess.run(["git", "ls-files", "-z", "--", "*.pyi"], cwd=T.ROOT,
                       capture_output=True, text=True, timeout=60, check=True)
    assert r.stdout == ""


def test_main_passes_over_a_pinned_pyright_that_is_not_executable(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The pinned pyright is taken only when it is there and executable; one that is
    there but cannot run is passed over for the one on PATH, and the output says so."""
    log, pinned_pyright, path_pyright = _gate_over_one_file(tmp_path, monkeypatch,
                                                            pinned=True, on_path=True)
    pinned_pyright.chmod(0o644)   # there, but running it would fail with EACCES
    assert T.main([]) == 0
    assert log.read_text().splitlines() == [str(path_pyright), str(tmp_path / "bin/mypy")]
    assert capsys.readouterr() == (
        f"typecheck: pyright is {path_pyright} from PATH, not the pinned one (run `npm ci`)\n"
        "TYPECHECK OK (1 files; 0 baseline errors left in 0 files)\n", "")


@pytest.mark.parametrize("mode", ["basic", "standard"])
@pytest.mark.parametrize("argv", [[], ["--update-baseline"]], ids=["check", "update-baseline"])
def test_main_refuses_a_comment_that_runs_a_file_below_strict(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str], mode: str, argv: list[str]) -> None:
    """`# pyright: basic` (or standard) takes one file out of strict, and the ratchet
    passes the drop. The gate refuses it before the checkers run, and before a
    baseline could record counts strict never saw."""
    log, _pinned, _path = _gate_over_one_file(tmp_path, monkeypatch, pinned=True, on_path=False)
    (tmp_path / "a.py").write_text(f'"""A module."""\n  # pyright: {mode}\nx = 1\n')
    assert T.main(argv) == 1
    out, err = capsys.readouterr()
    assert out == "typecheck: pyright is node_modules/.bin/pyright, the pinned one\n"
    assert err == ("TYPECHECK FAILED: a comment or decorator lowers type checking (remove it):\n"
                   f"  a.py:2: # pyright: {mode}\n")
    assert not log.exists()                        # neither checker ran
    assert not (tmp_path / "baseline.json").exists()


def test_main_refuses_a_mode_comment_in_an_extensionless_script(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The gate type-checks the extensionless python scripts, so it reads them for the
    comment too."""
    log, _pinned, _path = _gate_over_one_file(tmp_path, monkeypatch, pinned=True, on_path=False)
    monkeypatch.setattr(T, "python_files", lambda: ["deploy/tool"])
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/tool").write_text("#!/usr/bin/env python3\n# pyright: basic\nx = 1\n")
    assert T.main([]) == 1
    assert capsys.readouterr().err == (
        "TYPECHECK FAILED: a comment or decorator lowers type checking (remove it):\n"
        "  deploy/tool:2: # pyright: basic\n")
    assert not log.exists()                        # neither checker ran


@pytest.mark.parametrize("argv", [[], ["--update-baseline"]], ids=["check", "update-baseline"])
def test_main_exits_2_when_a_file_cannot_be_tokenized(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
    """The gate cannot check the comments of a file the tokenizer cannot read, so it
    has no verdict, and says so before the checkers run or a baseline is written."""
    log, _pinned, _path = _gate_over_one_file(tmp_path, monkeypatch, pinned=True, on_path=False)
    (tmp_path / "a.py").write_text('x = """never closed\n')
    assert T.main(argv) == 2
    out, err = capsys.readouterr()
    assert out == "typecheck: pyright is node_modules/.bin/pyright, the pinned one\n"
    assert err.startswith("typecheck: a.py: cannot check its comments: "), err
    assert not log.exists()                        # neither checker ran
    assert not (tmp_path / "baseline.json").exists()


# Comments that lower their file's checking, each with its line and the comment as the
# gate reports it. pyright takes a mode from any operand, and from a comment after code;
# it trims what JavaScript trims, U+FEFF and the Unicode spaces included. mypy reads a
# `# type: ignore` on a line before the first statement as silencing the file, and for a
# decorated def or class that statement starts at its first decorator's expression;
# mypy reads settings from a `# mypy: ` line anywhere.
_LOWERING: dict[str, tuple[bytes, int, str]] = {
    "own-line": (b"# pyright: basic\n", 1, "# pyright: basic"),
    "no-spaces": (b"#pyright:standard\n", 1, "#pyright:standard"),
    "indented": (b"if True:\n    # pyright: basic\n    pass\n", 2, "# pyright: basic"),
    "after-code": (b"x = 1  # pyright: basic\n", 1, "# pyright: basic"),
    "after-code-in-a-def": (b"def f(x):\n    return x  # pyright: standard\n", 2,
                            "# pyright: standard"),
    "after-a-rule": (b"# pyright: reportUnknownVariableType=none, basic\n", 1,
                     "# pyright: reportUnknownVariableType=none, basic"),
    "before-a-rule": (b"# pyright: basic, reportPrivateUsage=false\n", 1,
                      "# pyright: basic, reportPrivateUsage=false"),
    "trailing-comma": (b"# pyright: basic,\n", 1, "# pyright: basic,"),
    "tabs": (b"#\tpyright:\tbasic\n", 1, "#\tpyright:\tbasic"),
    "no-break-space": ("#\u00a0pyright: basic\n".encode(), 1, "#\u00a0pyright: basic"),
    "zero-width-no-break-space": ("#\ufeffpyright: basic\n".encode(), 1,
                                  "#\ufeffpyright: basic"),
    "after-a-byte-order-mark": ("\ufeff# pyright: basic\n".encode(), 1, "# pyright: basic"),
    "crlf": (b"x = 1\r\n# pyright: basic\r\n", 2, "# pyright: basic"),
    "cr": (b"x = 1\r# pyright: basic\r", 2, "# pyright: basic"),
    "last-line-unterminated": (b"x = 1\n# pyright: basic", 2, "# pyright: basic"),
    "after-a-form-feed": (b"\x0c# pyright: basic\n", 1, "# pyright: basic"),
    "ideographic-space-before": ("x = 1\n#\u3000pyright: basic\n".encode(), 2,
                                 "#\u3000pyright: basic"),
    "ogham-space-before": ("x = 1\n#\u1680pyright: standard\n".encode(), 2,
                           "#\u1680pyright: standard"),
    "em-space-operand": ("x = 1\n# pyright:\u2003basic\n".encode(), 2, "# pyright:\u2003basic"),
    "no-break-space-operand-after-a-rule": (
        "x = 1\n# pyright: reportPrivateUsage=false,\u00a0standard\n".encode(), 2,
        "# pyright: reportPrivateUsage=false,\u00a0standard"),
    "zero-width-no-break-space-operand-after-a-rule": (
        "x = 1\n# pyright: reportPrivateUsage=false,\ufeffbasic\n".encode(), 2,
        "# pyright: reportPrivateUsage=false,\ufeffbasic"),
    "narrow-no-break-space-after-the-mode": ("x = 1\n# pyright: basic\u202f\n".encode(), 2,
                                             "# pyright: basic\u202f"),
    "line-separator-after-the-mode": ("x = 1\n# pyright: standard\u2028\n".encode(), 2,
                                      "# pyright: standard\u2028"),
    # pyright keeps these two strict; the gate refuses them anyway.
    "beside-strict": (b"# pyright: strict, basic\n", 1, "# pyright: strict, basic"),
    "after-ignore": (b"# pyright: ignore, standard\n", 1, "# pyright: ignore, standard"),
    "type-ignore": (b"# type: ignore\nx = 1\n", 1, "# type: ignore"),
    "type-ignore-a-code": (b"# type: ignore[misc]\nx = 1\n", 1, "# type: ignore[misc]"),
    "type-ignore-no-spaces": (b"#type:ignore\nx = 1\n", 1, "#type:ignore"),
    "type-ignore-after-a-shebang": (b"#!/usr/bin/env python3\n# type: ignore\nx = 1\n", 2,
                                    "# type: ignore"),
    "type-ignore-after-another-comment": (b"# noqa # type: ignore\nx = 1\n", 1,
                                          "# noqa # type: ignore"),
    "type-ignore-in-an-empty-file": (b"# type: ignore\n", 1, "# type: ignore"),
    "type-ignore-before-a-decorator-expression": (
        b"@(  # type: ignore\n    staticmethod)\ndef first(): ...\n", 1, "# type: ignore"),
    "type-ignore-on-its-own-line-in-a-decorator": (
        b"@(\n    # type: ignore\n    staticmethod)\ndef first(): ...\n", 2, "# type: ignore"),
    "type-ignore-before-a-class-decorator-expression": (
        b"@(  # type: ignore[misc]\n    lambda c: c)\nclass First: ...\n", 1,
        "# type: ignore[misc]"),
    "type-ignore-before-a-walrus-decorator": (
        b"@(  # type: ignore\n    d := staticmethod)\ndef first(): ...\n", 1, "# type: ignore"),
    "type-ignore-in-nested-parentheses": (
        b"@((  # type: ignore\n    lambda f: f))\ndef first(): ...\n", 1, "# type: ignore"),
    "type-ignore-before-an-async-def-decorator-expression": (
        b"@(  # type: ignore\n    lambda f: f)\nasync def first() -> None: ...\n", 1,
        "# type: ignore"),
    "no-type-check": (b"from typing import no_type_check\n\n\n@no_type_check\ndef f(x):\n"
                      b"    return x\n", 4, "@no_type_check"),
    "typing-dot-no-type-check": (b"import typing\n\n\n@typing.no_type_check\ndef f(x):\n"
                                 b"    return x\n", 4, "@typing.no_type_check"),
    "no-type-check-on-an-async-def": (b"from typing import no_type_check\n\n\n@no_type_check\n"
                                      b"async def f(x):\n    return x\n", 4, "@no_type_check"),
    "no-type-check-on-a-method": (b"from typing import no_type_check\n\n\nclass C:\n"
                                  b"    @no_type_check\n    def m(self, x):\n        return x\n",
                                  5, "@no_type_check"),
    # Neither checker honours it on a class; the gate refuses it anyway.
    "no-type-check-on-a-class": (b"from typing import no_type_check\n\n\n@no_type_check\n"
                                 b"class C: ...\n", 4, "@no_type_check"),
    "mypy-ignore-errors": (b"# mypy: ignore-errors\nx = 1\n", 1, "# mypy: ignore-errors"),
    "mypy-setting-after-code": (b"x = 1\n# mypy: no-check-untyped-defs\n", 2,
                                "# mypy: no-check-untyped-defs"),
    "mypy-setting-in-a-docstring": (b'"""Doc.\n# mypy: ignore-errors\n"""\n', 2,
                                    "# mypy: ignore-errors"),
    "mypy-setting-after-a-byte-order-mark": ("\ufeff# mypy: ignore-errors\nx = 1\n".encode(), 1,
                                             "# mypy: ignore-errors"),
    # mypy does not read the first of these two, and the second silences one named code
    # only; the gate refuses both, since mypy's settings belong in mypy.ini.
    "mypy-no-spaces": (b"#mypy:ignore-errors\n", 1, "#mypy:ignore-errors"),
    "mypy-one-code": (b"# mypy: disable-error-code=misc\n", 1,
                      "# mypy: disable-error-code=misc"),
}

# What lowers no file's checking: other directives, line-level ignores, look-alikes.
_NOT_LOWERING: dict[str, bytes] = {
    "strict": b"# pyright: strict\n",
    "a-rule": b"# pyright: reportUnknownMemberType=false\n",
    "rules": b"# pyright: reportPrivateUsage=false, reportUnusedVariable=false\n",
    "a-mode-name-as-a-rule": b"# pyright: basic=true\n",
    "off": b"# pyright: off\n",                       # no such mode: an unknown rule
    "text-after-the-mode": b"# pyright: basic # why\n",  # the operand is "basic # why"
    "line-ignore": b"x = 1  # pyright: ignore[reportUnknownMemberType]\n",
    "ignore-list": b"# pyright: ignore[reportPrivateUsage, reportUnusedVariable]\n",
    "in-a-string": b'x = "# pyright: basic"\n',
    "in-a-multiline-string": b'"""\n# pyright: basic\n"""\n',
    "prose": b"# notes on pyright: basic, standard\n",
    "after-another-comment": b"# noqa # pyright: basic\n",
    "doubled-hash": b"## pyright: basic\n",
    "capitalised": b"# Pyright: basic\n",
    "space-before-colon": b"# pyright : basic\n",
    "not-a-mode-name": b"#pyright:basics\n",
    "type-ignore-on-a-line": b"x = 1  # type: ignore\n",
    "type-ignore-a-code-on-a-later-line": b"x = 1\ny = 2  # type: ignore[misc]\n",
    "type-ignore-after-a-docstring": b'"""Doc."""\n# type: ignore\nx = 1\n',
    "mypy-in-a-string": b'x = "# mypy: ignore-errors"\n',
    "mypy-in-prose": b"# notes on mypy: none\n",
    "type-ignore-on-the-decorator-line": b"@staticmethod  # type: ignore[misc]\ndef first(): ...\n",
    "type-ignore-on-the-decorator-expression-line": (
        b"@(\n    staticmethod  # type: ignore\n)\ndef first(): ...\n"),
    "type-ignore-between-decorator-and-def": b"@(lambda f: f)\n# type: ignore\ndef first(): ...\n",
    "type-ignore-in-a-second-decorator": (
        b"@(lambda f: f)\n@(  # type: ignore\n    lambda f: f)\ndef first(): ...\n"),
    "type-ignore-in-a-subscripted-decorator": (
        b"@[\n    # type: ignore\n    lambda f: f][0]\ndef first(): ...\n"),
    "type-ignore-inside-the-first-statement": b"x = print(  # type: ignore\n    1)\n",
    "utf-8-declared": b"# -*- coding: utf-8 -*-\nx = 1\n",
    "utf-8-declared-without-a-hyphen": b"# coding: UTF8\nx = 1\n",
    "ascii-declared": b"# -*- coding: ascii -*-\nx = 1\n",
    "type-ignore-on-a-decorated-class-s-first-decorator": (
        b"@(lambda c: c)  # type: ignore[misc]\nclass First: ...\n"),
    "type-ignore-on-a-decorated-async-def-s-first-decorator": (
        b"@(lambda f: f)  # type: ignore[misc]\nasync def first() -> None: ...\n"),
    "another-decorator": (b"from functools import cache\n\n\n@cache\ndef f() -> int:\n"
                          b"    return 1\n"),
    "no-type-check-in-a-string": b'x = "@no_type_check"\n',
}


@pytest.mark.parametrize("source, line, comment", list(_LOWERING.values()), ids=list(_LOWERING))
def test_mode_lowering_comments_finds_a_lower_mode_wherever_pyright_reads_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        source: bytes, line: int, comment: str) -> None:
    monkeypatch.setattr(T, "ROOT", tmp_path)
    (tmp_path / "a.py").write_bytes(source)
    assert T.mode_lowering_comments(["a.py"]) == [f"a.py:{line}: {comment}"]


@pytest.mark.parametrize("source", list(_NOT_LOWERING.values()), ids=list(_NOT_LOWERING))
def test_mode_lowering_comments_passes_what_sets_no_lower_mode(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: bytes) -> None:
    monkeypatch.setattr(T, "ROOT", tmp_path)
    (tmp_path / "a.py").write_bytes(source)
    assert T.mode_lowering_comments(["a.py", "gone.py"]) == []   # an unreadable file is skipped


def test_mode_lowering_comments_lists_every_kind_in_line_order(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """mypy settings are found line by line after the comments are tokenized; the
    report still runs in line order, and lists each kind."""
    monkeypatch.setattr(T, "ROOT", tmp_path)
    (tmp_path / "a.py").write_bytes(b"# mypy: ignore-errors\n# type: ignore\n"
                                    b"x = 1  # pyright: basic\n")
    assert T.mode_lowering_comments(["a.py"]) == [
        "a.py:1: # mypy: ignore-errors", "a.py:2: # type: ignore", "a.py:3: # pyright: basic"]


# Files the gate cannot read as the checkers do. pyright reads every file as UTF-8, and
# Python and mypy by its declared encoding, so under another declaration they can see
# different comments: one might hide a mode the other applies. mypy reads the
# declaration with its own pattern, which takes the last one on a line.
_UNREADABLE: dict[str, bytes] = {
    "unclosed-string": b'x = """never closed\n',
    "bad-dedent": b"if True:\n        x = 1\n    y = 2\n",
    "does-not-parse": b"x = = 1\n",
    "not-utf-8-first-line": b"x = '\xff'\n",
    "not-utf-8-third-line": b"x = 1\ny = 2\nz = '\xff'\n",
    "latin-1-declared": b"# -*- coding: latin-1 -*-\nx = 1\n",
    "shift-jis-declared": b"# -*- coding: shift_jis -*-\nx = 1\n",
    "declared-after-a-shebang": b"#!/usr/bin/env python3\n# coding: latin-1\nx = 1\n",
    "declared-after-a-form-feed": b"\x0c# -*- coding: latin-1 -*-\nx = 1\n",
    "an-unknown-encoding-declared": b"# -*- coding: no-such-codec -*-\nx = 1\n",
    "utf-8-then-shift-jis-declared": b"# coding=utf-8 coding=shift_jis\nx = 1\n",
    "utf-8-then-an-unknown-codec-declared": b"# coding=utf-8 coding=no-such-codec\nx = 1\n",
    "utf-8-then-latin-1-declared": (
        b"# -*- coding: utf-8 -*- vim: set fileencoding=latin-1 :\nx = 1\n"),
    "a-transform-codec-declared": b"# -*- coding: rot13 -*-\nx = 1\n",
    "hex-declared": b"# coding: hex\nx = 1\n",
    "too-deep-to-parse": b"x = " + b"+".join([b"1"] * 10000) + b"\n",
}


@pytest.mark.parametrize("source", list(_UNREADABLE.values()), ids=list(_UNREADABLE))
def test_mode_lowering_comments_raises_on_a_file_it_cannot_read_as_the_checkers_do(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: bytes) -> None:
    monkeypatch.setattr(T, "ROOT", tmp_path)
    (tmp_path / "a.py").write_bytes(source)
    with pytest.raises(ValueError, match=r"^a\.py: cannot check its comments: "):
        T.mode_lowering_comments(["a.py"])


def test_mode_lowering_comments_says_which_encoding_a_file_declares(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(T, "ROOT", tmp_path)
    (tmp_path / "a.py").write_bytes(_UNREADABLE["latin-1-declared"])
    with pytest.raises(ValueError) as e:
        T.mode_lowering_comments(["a.py"])
    assert str(e.value) == ("a.py: cannot check its comments: it declares the iso8859-1 "
                            "encoding, and pyright reads every file as UTF-8")


# Four errors from three rules that strict reports and basic and standard do not: a
# file with none of them left ran below strict.
_STRICT_ONLY = b"def strict_only(x):\n    return x\n"
_STRICT_ONLY_RULES = {"reportUnknownParameterType", "reportMissingParameterType",
                      "reportUnknownVariableType"}


@pytest.mark.skipif(_PYRIGHT is None, reason="pyright is not installed (npm ci)")
def test_no_comment_runs_a_file_below_strict_past_the_gate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Asks the pyright the gate runs, with the repo's pyrightconfig.json, which of the
    forms above take a file out of strict: the gate must refuse each of them. A
    `# type: ignore` before any code would silence the whole file in pyright, but the
    config turns those comments off, so they must leave the file in strict. An
    upgraded pyright is re-checked here."""
    assert _PYRIGHT is not None
    shutil.copy(T.ROOT / "pyrightconfig.json", tmp_path / "pyrightconfig.json")
    sources = {**{k: v[0] for k, v in _LOWERING.items()}, **_NOT_LOWERING, "control": b""}
    names = {k: f"v{i}.py" for i, k in enumerate(sources)}
    for k, source in sources.items():
        (tmp_path / names[k]).write_bytes(source + b"\n" + _STRICT_ONLY)
    r = subprocess.run([_PYRIGHT, "--outputjson", *names.values()], cwd=tmp_path,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode in (0, 1), r.stderr
    report: dict[str, Any] = json.loads(r.stdout)
    rules: dict[str, set[str]] = {name: set() for name in names.values()}
    for d in report["generalDiagnostics"]:
        rules.setdefault(Path(str(d["file"])).name, set()).add(str(d.get("rule")))
    lowered = {k for k, name in names.items() if not rules[name] & _STRICT_ONLY_RULES}
    assert "own-line" in lowered and "control" not in lowered   # the probe tells them apart
    monkeypatch.setattr(T, "ROOT", tmp_path)
    refused = {k for k, name in names.items() if T.mode_lowering_comments([name])}
    assert sorted(lowered - refused) == []
    # The config turns `# type: ignore` off for pyright, so not even those lower a file.
    assert sorted(k for k in lowered if k.startswith("type-ignore")) == []


# Two of the same function, one under `@no_type_check`: lines 10-12 hold the other.
_NO_TYPE_CHECK_TWINS = (b"from typing import no_type_check\n\n\n"
                        b"@no_type_check\ndef silenced(x):\n    y: int = 'x'\n    return x\n\n\n"
                        b"def checked(x):\n    y: int = 'x'\n    return x\n")


@pytest.mark.skipif(_PYRIGHT is None or shutil.which("mypy") is None,
                    reason="pyright (npm ci) or mypy is not installed")
def test_no_type_check_switches_both_checkers_off_for_a_function(tmp_path: Path) -> None:
    """Why the gate refuses `@no_type_check`: with the repo's configs, only the twin
    without it has errors, in pyright and in mypy."""
    assert _PYRIGHT is not None
    shutil.copy(T.ROOT / "pyrightconfig.json", tmp_path / "pyrightconfig.json")
    shutil.copy(T.ROOT / "mypy.ini", tmp_path / "mypy.ini")
    (tmp_path / "twins.py").write_bytes(_NO_TYPE_CHECK_TWINS)
    r = subprocess.run([_PYRIGHT, "--outputjson", "twins.py"], cwd=tmp_path,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode in (0, 1), r.stderr
    report: dict[str, Any] = json.loads(r.stdout)
    pyright_lines = {int(d["range"]["start"]["line"]) + 1
                     for d in report["generalDiagnostics"] if d["severity"] == "error"}
    m = subprocess.run(["mypy", "--no-error-summary", "--python-executable", sys.executable,
                        "twins.py"], cwd=tmp_path, capture_output=True, text=True, timeout=600)
    assert m.returncode in (0, 1), m.stdout + m.stderr
    mypy_lines = {int(x.group(1)) for x in map(re.compile(r"^twins\.py:(\d+)").match,
                                                m.stdout.splitlines()) if x}
    checked = set(range(10, 13))
    assert pyright_lines and pyright_lines <= checked, pyright_lines
    assert mypy_lines and mypy_lines <= checked, mypy_lines


# Two mypy errors under mypy.ini: one in a typed function, and one in an untyped
# function, which check_untyped_defs checks.
_MYPY_BODY = (b"def typed() -> int:\n    return 'x'\n\n\n"
              b"def untyped():\n    y: int = 'x'\n    return y\n")
_MYPY_BODY_CODES = {"return-value", "assignment"}
_MYPY_ERROR = re.compile(r"^(?P<file>[^:]+):\d+(?::\d+)?: error: .*\[(?P<code>[a-z-]+)\]$")


@pytest.mark.skipif(shutil.which("mypy") is None, reason="mypy is not installed")
def test_no_comment_silences_a_file_for_mypy_past_the_gate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Asks mypy, with the repo's mypy.ini, which of the forms above silence one of a
    file's errors: the gate must refuse each of them."""
    shutil.copy(T.ROOT / "mypy.ini", tmp_path / "mypy.ini")
    sources = {**{k: v[0] for k, v in _LOWERING.items()}, **_NOT_LOWERING, "control": b""}
    names = {k: f"v{i}.py" for i, k in enumerate(sources)}
    for k, source in sources.items():
        (tmp_path / names[k]).write_bytes(source + b"\n" + _MYPY_BODY)
    r = subprocess.run(["mypy", "--no-error-summary", "--python-executable", sys.executable,
                        *names.values()], cwd=tmp_path, capture_output=True, text=True,
                       timeout=600)
    assert r.returncode in (0, 1), r.stdout + r.stderr
    codes: dict[str, set[str]] = {name: set() for name in names.values()}
    for line in r.stdout.splitlines():
        m = _MYPY_ERROR.match(line)
        if m:
            codes.setdefault(m.group("file"), set()).add(m.group("code"))
    lowered = {k for k, name in names.items() if not _MYPY_BODY_CODES <= codes[name]}
    # The probe tells them apart, and these rows are mypy's own behaviour.
    assert {"type-ignore", "mypy-ignore-errors", "mypy-setting-in-a-docstring",
            "mypy-setting-after-a-byte-order-mark", "type-ignore-before-a-decorator-expression",
            "type-ignore-on-its-own-line-in-a-decorator",
            "type-ignore-before-a-class-decorator-expression"} <= lowered
    assert "control" not in lowered
    monkeypatch.setattr(T, "ROOT", tmp_path)
    refused = {k for k, name in names.items() if T.mode_lowering_comments([name])}
    assert sorted(lowered - refused) == []


# Under shift_jis the bytes C2 83 5C are two characters, the second swallowing the
# backslash, so the string `_OPEN` starts does not end where it does in UTF-8: it runs
# to `_CLOSE`.
_OPEN = '_ = """\u0083\\\\"""\n'.encode()
_CLOSE = b"_ = '\"\"\"' # '\n"


@pytest.mark.skipif(shutil.which("mypy") is None, reason="mypy is not installed")
def test_mypy_reads_the_last_declaration_on_a_line_where_python_reads_the_first(
        tmp_path: Path) -> None:
    """Why the gate also reads the declaration as mypy does: under
    `# coding=utf-8 coding=shift_jis` Python decodes UTF-8 and sees the code, while mypy
    decodes shift_jis and reads the code as part of a string."""
    shutil.copy(T.ROOT / "mypy.ini", tmp_path / "mypy.ini")
    two = b"# coding=utf-8 coding=shift_jis\n" + _OPEN + _MYPY_BODY + _CLOSE
    one = b"# coding=utf-8\n" + _OPEN + _MYPY_BODY + _CLOSE
    (tmp_path / "two.py").write_bytes(two)
    (tmp_path / "one.py").write_bytes(one)
    for source in (two, one):   # Python reads each as UTF-8, with the body as code
        assert tokenize.detect_encoding(io.BytesIO(source).readline)[0] == "utf-8"
        assert [n.name for n in ast.parse(source.decode()).body
                if isinstance(n, ast.FunctionDef)] == ["typed", "untyped"]
    r = subprocess.run(["mypy", "--no-error-summary", "--python-executable", sys.executable,
                        "two.py", "one.py"], cwd=tmp_path, capture_output=True, text=True,
                       timeout=600)
    assert r.returncode in (0, 1), r.stdout + r.stderr
    files = [m.group("file") for m in map(_MYPY_ERROR.match, r.stdout.splitlines()) if m]
    assert files == ["one.py", "one.py"]   # mypy read two.py's body as a string
