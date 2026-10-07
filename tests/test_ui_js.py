"""The UI's JavaScript tests (tests/js/), run under node. The repo has no browser
harness: each one takes the code it covers from ui/app.js as written and runs it
against stub elements."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed, and the UI's JS tests run under it")
def test_the_exit_kpi_tile_renders_every_status():
    r = subprocess.run(["node", str(ROOT / "tests/js/test_kpi_exit.js"), str(ROOT / "ui/app.js")],
                       capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    out = f"node exited {r.returncode}:\n{r.stdout}{r.stderr}"
    assert r.returncode == 0, out
    assert r.stdout.rstrip().endswith(" checks, 0 failed"), out    # it ran its checks
