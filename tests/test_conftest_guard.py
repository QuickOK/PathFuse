"""Self-tests of the live-state guard in tests/conftest.py.

The guard fails any test that opens something under the box's live config and state
dirs (LIVE_STATE_DIRS), the relay actuator's included. Once every test is hermetic,
nothing else in the suite would notice if the guard stopped working: a missing dir, a
blinded prefix check, a misspelt event name or a teardown that never fails would all
pass it. These tests make each of those fail. None of them opens anything under the
live dirs: they feed the audit hook the event an open() would raise, directly or with
sys.audit, or run an inner pytest session that does.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import conftest

CONFTEST = Path(__file__).resolve().parent / "conftest.py"


@pytest.mark.parametrize("path", [
    "/etc/sbfd-ctl/sbfd-ctl.json",                  # still covered (not a vacuous pass)
    "/etc/relay-egress-watchdog/config.json",       # the relay actuator's live config
    "/run/relay-egress-watchdog/deadman.json",      # the dead-man record (DEADMAN_RECORD)
    "/run/relay-egress-watchdog/state.json",        # the actuator's default state_path
])
def test_the_live_state_guard_records_opens_of_the_boxes_live_files(path):
    guard = conftest.LiveStateGuard()
    guard.opened = []
    guard.audit("open", (path, "r", 0))
    assert guard.opened == [path]


def test_the_live_state_guard_ignores_a_lookalike_dir():
    guard = conftest.LiveStateGuard()
    guard.opened = []
    guard.audit("open", ("/run/relay-egress-watchdog-old/deadman.json", "r", 0))
    assert guard.opened == []


def test_the_guard_records_opens_under_the_live_dirs_and_nothing_else(live_state_guard):
    live = ["/etc/sbfd-ctl/config.json", "/var/lib/sbfd-ctl/stations.json",
            "/run/sbfd-ctl/state.json", "/run/sbfd-ctl", "/run/./sbfd-ctl//points.json"]
    elsewhere = ["/run/sbfd-ctl2/state.json", "/var/lib/sbfd-ctl.old/stations.json",
                 "/etc/sbfd-ctlx", "/tmp/state.json"]
    for p in live + elsewhere:
        sys.audit("open", p, "r", 0)
    sys.audit("open", b"/run/sbfd-ctl/handoff.json", "rb", 0)   # a bytes path counts too
    sys.audit("open", 7, "r", 0)                                 # an fd names no path
    sys.audit("os.remove", "/run/sbfd-ctl/state.json", -1)       # not an open
    # Take what was recorded, so this test does not fail its own teardown.
    recorded, live_state_guard.opened = live_state_guard.opened, []
    assert recorded == ["/etc/sbfd-ctl/config.json", "/var/lib/sbfd-ctl/stations.json",
                        "/run/sbfd-ctl/state.json", "/run/sbfd-ctl",
                        "/run/sbfd-ctl/points.json", "/run/sbfd-ctl/handoff.json"]


def test_the_guard_fails_a_test_that_opened_a_live_file(tmp_path):
    shutil.copy(CONFTEST, tmp_path / "conftest.py")
    (tmp_path / "test_inner.py").write_text(
        "import sys\n"
        "\n"
        "def test_touches_live_state():\n"
        "    sys.audit('open', '/run/sbfd-ctl/state.json', 'r', 0)\n"
        "\n"
        "def test_stays_in_tmp(tmp_path):\n"
        "    (tmp_path / 'state.json').write_text('{}')\n")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--rootdir", str(tmp_path), "test_inner.py"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    assert "ERROR at teardown of test_touches_live_state" in out, out
    assert "\n  /run/sbfd-ctl/state.json\n" in out, out
    assert "test_stays_in_tmp" not in out, out   # a hermetic test is left alone
