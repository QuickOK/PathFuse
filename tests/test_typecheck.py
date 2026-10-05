"""Tests for the type-check gate (scripts/typecheck.py)."""
import importlib.util
import json
import shutil
import subprocess
import sys
from collections import Counter
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

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


def test_pyright_counts_errors_per_repo_file(monkeypatch):
    diags = [{"file": str(T.ROOT / f), "severity": sev} for f, sev in [
        ("a.py", "error"), ("a.py", "error"), ("a.py", "warning"), ("tools/b.py", "error")]]
    calls = _fake_run(monkeypatch, json.dumps({"generalDiagnostics": diags,
                                               "summary": {"filesAnalyzed": 2}}))
    assert T.pyright_counts(["a.py", "tools/b.py"], "/venv/python") == {"a.py": 2, "tools/b.py": 1}
    # --outputjson makes the report parseable; cwd picks up pyrightconfig.json.
    assert calls == [(["pyright", "--outputjson", "--pythonpath", "/venv/python",
                       "a.py", "tools/b.py"], T.ROOT)]


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
def test_counts_raise_when_the_checker_itself_fails(monkeypatch, tool):
    # Exit 2 is a crash or a usage error. mypy's "Duplicate module" lines carry no line
    # number, so read as counts they would pass every file as clean.
    _fake_run(monkeypatch, 'x: error: Duplicate module named "__main__"\n', rc=2)
    with pytest.raises(RuntimeError, match=rf"{tool} failed \(rc=2\)"):
        getattr(T, f"{tool}_counts")(["a.py"], "python3")


@pytest.mark.skipif(shutil.which("pyright") is None, reason="pyright is not installed")
def test_pyright_counts_refuses_a_run_that_skipped_a_named_file(tmp_path, monkeypatch):
    # pyright silently skips a file named on its command line when the file lies under
    # an `exclude` of pyrightconfig.json; that file must not pass as clean.
    (tmp_path / "pyrightconfig.json").write_text('{"exclude": ["docs"]}\n')
    (tmp_path / "docs").mkdir()
    for f in ("top.py", "docs/x.py"):
        (tmp_path / f).write_text('x: int = "not an int"\n')
    monkeypatch.setattr(T, "ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="checked 1 of 2 files"):
        T.pyright_counts(["docs/x.py", "top.py"], sys.executable)


def _stub_gate(monkeypatch, tmp_path, pyright, mypy, baseline=None):
    """main() over three files with canned counts and a baseline file in tmp_path."""
    monkeypatch.setattr(T, "shutil", SimpleNamespace(which=lambda tool: f"/usr/bin/{tool}"))
    monkeypatch.setattr(T, "python_files", lambda: ["a.py", "b.py", "c.py"])
    monkeypatch.setattr(T, "pyright_counts", lambda files, py: Counter(pyright))
    monkeypatch.setattr(T, "mypy_counts", lambda files, py: Counter(mypy))
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


def _raise(exc):
    def counts(files, py):
        raise exc
    return counts


@pytest.mark.parametrize("missing, pyright, msg", [
    ("pyright", None, "pyright is not installed"),
    ("mypy", None, "mypy is not installed"),
    (None, _raise(RuntimeError("pyright failed (rc=3): bad config")), "pyright failed (rc=3)"),
    (None, _raise(ValueError("Expecting value")), "Expecting value"),
], ids=["no-pyright", "no-mypy", "crash", "bad-json"])
def test_main_exits_2_when_a_checker_is_missing_or_crashes(tmp_path, monkeypatch, capsys,
                                                         missing, pyright, msg):
    _stub_gate(monkeypatch, tmp_path, pyright={}, mypy={})
    monkeypatch.setattr(T, "shutil", SimpleNamespace(
        which=lambda tool: None if tool == missing else f"/usr/bin/{tool}"))
    if pyright is not None:
        monkeypatch.setattr(T, "pyright_counts", pyright)
    assert T.main([]) == 2
    assert msg in capsys.readouterr().err


@pytest.mark.parametrize("tool", ["git", "pyright", "mypy"])
def test_main_exits_2_and_names_a_tool_that_hangs(tmp_path, monkeypatch, capsys, tool):
    """Each tool runs with a time limit, so a hung one cannot stall preflight."""
    order, limit = ["git", "pyright", "mypy"], {"git": 60, "pyright": 900, "mypy": 900}
    (tmp_path / "a.py").write_text("")
    limits = {}

    def run(argv, **kw):
        limits[argv[0]] = kw.get("timeout")
        if argv[0] == tool:
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        out = {"git": "a.py\0", "mypy": "",
               "pyright": json.dumps({"generalDiagnostics": [], "summary": {"filesAnalyzed": 1}})}
        return subprocess.CompletedProcess(argv, 0, out[argv[0]], "")

    monkeypatch.setattr(T, "ROOT", tmp_path)
    monkeypatch.setattr(T, "shutil", SimpleNamespace(which=lambda t: f"/usr/bin/{t}"))
    monkeypatch.setattr(T, "subprocess", SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired))
    assert T.main([]) == 2
    assert capsys.readouterr().err == f"typecheck: {tool} timed out after {limit[tool]} s\n"
    # Every tool that ran had its limit, the hung one included.
    assert limits == {t: limit[t] for t in order[:order.index(tool) + 1]}
