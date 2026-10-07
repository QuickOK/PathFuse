"""Tests for the type-check gate (scripts/typecheck.py)."""
import importlib.util
import json
import shutil
import subprocess
import sys
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


def test_pyright_runs_in_strict_mode() -> None:
    """The baseline holds strict counts, and the ratchet only stops counts rising: a
    lower mode would pass every file under it, so the mode itself is pinned here."""
    config = json.loads((T.ROOT / "pyrightconfig.json").read_text())
    assert config.get("typeCheckingMode") == "strict"


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


@pytest.mark.parametrize("mode", ["basic", "standard", "off"])
@pytest.mark.parametrize("argv", [[], ["--update-baseline"]], ids=["check", "update-baseline"])
def test_main_refuses_a_comment_that_runs_a_file_below_strict(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str], mode: str, argv: list[str]) -> None:
    """`# pyright: basic` (or standard, or off) takes one file out of strict, and the
    ratchet passes the drop. The gate refuses it before the checkers run, and before a
    baseline could record counts strict never saw."""
    log, _pinned, _path = _gate_over_one_file(tmp_path, monkeypatch, pinned=True, on_path=False)
    (tmp_path / "a.py").write_text(f'"""A module."""\n  # pyright: {mode}\nx = 1\n')
    assert T.main(argv) == 1
    out, err = capsys.readouterr()
    assert out == "typecheck: pyright is node_modules/.bin/pyright, the pinned one\n"
    assert err == ("TYPECHECK FAILED: a comment runs a file below strict mode (remove it):\n"
                   f"  a.py:2: # pyright: {mode}\n")
    assert not log.exists()                        # neither checker ran
    assert not (tmp_path / "baseline.json").exists()


@pytest.mark.parametrize("text", [
    "# pyright: strict\n",                                 # raises the mode, if anything
    "# pyright: reportUnknownMemberType=false\n",          # one rule, not the mode
    'x = "# pyright: basic"\n',                            # in a string on a code line
    "x = 1  # pyright: ignore[reportUnknownMemberType]\n",  # a line-level ignore
    "#pyright:basics\n",                                  # not a mode name
], ids=["strict", "rule-toggle", "in-a-string", "line-ignore", "not-a-mode"])
def test_mode_lowering_comments_finds_only_a_lowered_mode(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(T, "ROOT", tmp_path)
    (tmp_path / "a.py").write_text(text)
    assert T.mode_lowering_comments(["a.py", "gone.py"]) == []   # an unreadable file is skipped
