"""The FEC tests make their temp files in each test's tmp_path, which pytest removes, not
in the system temp dir, where every run of the suite used to leave about 25 of them (on a
box whose /tmp is in RAM)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# The modules whose helpers make files with tempfile.mkstemp and tempfile.mkdtemp.
MODULES = ["tests/test_fec_config.py", "tests/test_udpspeeder_fec.py",
           "tests/test_fec_control.py"]


def test_the_fec_tests_leave_nothing_in_the_temp_dir(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The run below must not take the caller's PYTEST_ADDOPTS: its -k would deselect
    # these modules, and its --ff or --lf needs the cache plugin this run switches off.
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k no_such_test")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    env.update(TMPDIR=str(tmp), PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "--basetemp", str(tmp_path / "base"), *MODULES],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
    assert sorted(p.name for p in tmp.iterdir()) == []
