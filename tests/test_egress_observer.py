"""Tests for the actual-exit check (egress_observer.py)."""
import threading

import pytest

import egress_observer as E

MODES = {"relay_vpn", "relay_backbone", "relay_direct", "local_direct"}
RULES = (E.ExitRule("relay_vpn", "vpn", ("on", "plus")),
         E.ExitRule("relay_backbone", "ip", ("203.0.113.10",)),
         E.ExitRule("relay_direct", "ip", ("198.51.100.20",)))


class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# --- config ---

def test_parse_observe_cfg_defaults_and_rules():
    c = E.parse_observe_cfg({"url": "https://probe.example.net/trace", "exits": [
        {"mode": "relay_backbone", "field": "ip", "values": ["203.0.113.10"]}]}, MODES)
    assert c.url == "https://probe.example.net/trace" and c.iface == "wg0"
    assert (c.interval_s, c.timeout_s, c.mismatch_checks) == (120.0, 8.0, 2)
    assert c.exits == (E.ExitRule("relay_backbone", "ip", ("203.0.113.10",)),)


@pytest.mark.parametrize("raw", [None, {}, {"url": ""}])
def test_parse_observe_cfg_off(raw):
    assert E.parse_observe_cfg(raw, MODES) is None


@pytest.mark.parametrize("raw, match", [
    ({"url": "ftp://x"}, "url"),
    ({"url": "https://x", "interval_s": 0}, "interval_s"),
    ({"url": "https://x", "mismatch_checks": 0}, "mismatch_checks"),
    ({"url": "https://x", "exits": [{"mode": "banana", "field": "ip", "values": ["1"]}]}, "mode"),
    ({"url": "https://x", "exits": [{"mode": "relay_vpn", "field": "", "values": ["1"]}]}, "field"),
    ({"url": "https://x", "exits": [{"mode": "relay_vpn", "field": "ip", "values": []}]}, "values"),
])
def test_parse_observe_cfg_rejects(raw, match):
    with pytest.raises(ValueError, match=match):
        E.parse_observe_cfg(raw, MODES)


# --- parse / classify / fetch ---

def test_parse_trace_reads_key_value_lines():
    assert E.parse_trace("fl=1\nip=203.0.113.10\nvpn=off\njunk\n=x\n") == {
        "fl": "1", "ip": "203.0.113.10", "vpn": "off"}


@pytest.mark.parametrize("fields, want", [
    ({"ip": "192.0.2.9", "vpn": "plus"}, "relay_vpn"),
    ({"ip": "203.0.113.10", "vpn": "off"}, "relay_backbone"),
    ({"ip": "198.51.100.20", "vpn": "off"}, "relay_direct"),
    ({"ip": "192.0.2.9", "vpn": "off"}, "unknown"),
])
def test_classify_first_matching_rule(fields, want):
    assert E.classify(fields, RULES) == want


def test_fetch_trace_binds_the_interface():
    seen = {}

    def run(argv, **kw):
        seen["argv"] = argv
        return R(out="ip=203.0.113.10\n")

    assert E.fetch_trace("https://probe.example.net/trace", "wg0", 8.0, run) == ("ip=203.0.113.10\n", None)
    assert seen["argv"] == ["curl", "--silent", "--show-error", "--max-time", "8",
                            "--interface", "wg0", "https://probe.example.net/trace"]


def test_fetch_trace_reports_curl_failure():
    body, err = E.fetch_trace("https://x", "wg0", 8.0, lambda *a, **k: R(rc=28, err="timed out"))
    assert body is None and err.startswith("curl rc=28")


# --- tracker ---

def test_tracker_match_resets_and_mismatch_needs_n_checks():
    clk = Clock()
    t = E.ExitTracker(2, clock=clk)
    t.select("relay_backbone")
    assert t.status == "checking"
    t.update("relay_backbone", "203.0.113.10", None)
    assert t.status == "match"
    clk.t += 120
    t.update("relay_direct", "198.51.100.20", None)
    assert t.status == "pending"
    clk.t += 120
    t.update("relay_direct", "198.51.100.20", None)
    assert t.status == "mismatch" and t.since == clk.t
    t.update("relay_backbone", "203.0.113.10", None)
    assert t.status == "match"


def test_tracker_error_keeps_the_count():
    t = E.ExitTracker(2, clock=Clock())
    t.select("relay_backbone")
    t.update("relay_direct", "198.51.100.20", None)
    t.update(None, None, "timeout")
    assert t.status == "error" and t.error == "timeout"
    t.update("relay_direct", "198.51.100.20", None)
    assert t.status == "mismatch"


def test_tracker_local_direct_is_skipped():
    t = E.ExitTracker(1, clock=Clock())
    t.select("local_direct")
    t.update("relay_direct", "198.51.100.20", None)
    assert t.status == "skipped" and t.observed is None


def test_tracker_mode_change_restarts_the_count():
    t = E.ExitTracker(2, clock=Clock())
    t.select("relay_backbone")
    t.update("relay_direct", "198.51.100.20", None)
    t.select("relay_vpn")
    assert t.status == "checking"
    t.update("relay_direct", "198.51.100.20", None)
    assert t.status == "pending"


def test_tracker_snapshot_shape():
    t = E.ExitTracker(2, clock=Clock(5.0))
    t.select("relay_direct")
    t.update("relay_direct", "198.51.100.20", None)
    assert t.snapshot() == {"selected": "relay_direct", "observed": "relay_direct",
                            "ip": "198.51.100.20", "status": "match", "since": 5.0,
                            "checked_at": 5.0, "error": None}


# --- observer ---

def _observer(body="ip=203.0.113.10\n", err=None):
    cfg = E.ObserveCfg(url="https://probe.example.net/trace", exits=RULES)
    calls = []

    def fetch(url, iface, timeout_s):
        calls.append((url, iface, timeout_s))
        return (None, err) if err else (body, None)

    return E.EgressObserver(cfg, fetch=fetch, clock=Clock()), calls


def test_check_once_waits_for_a_selected_mode():
    o, calls = _observer()
    o.check_once()
    assert calls == [] and o.snapshot()["status"] == "checking"


def test_check_once_classifies():
    o, calls = _observer()
    o.set_selected("relay_backbone")
    o.check_once()
    snap = o.snapshot()
    assert calls == [("https://probe.example.net/trace", "wg0", 8.0)]
    assert (snap["status"], snap["observed"], snap["ip"]) == ("match", "relay_backbone", "203.0.113.10")


def test_check_once_skips_the_fetch_in_local_direct():
    o, calls = _observer()
    o.set_selected("local_direct")
    o.check_once()
    assert calls == [] and o.snapshot()["status"] == "skipped"


def test_set_selected_kicks_only_on_change():
    o, _ = _observer()
    o.set_selected("relay_backbone")
    assert o._kick.is_set()
    o._kick.clear()
    o.set_selected("relay_backbone")
    assert not o._kick.is_set()


def test_thread_runs_and_stops():
    o, calls = _observer()
    o.cfg = E.ObserveCfg(url="https://probe.example.net/trace", exits=RULES, interval_s=0.05)
    o.settle_s = 0.0
    o.set_selected("relay_backbone")
    stop = threading.Event()
    o.start(stop)
    for _ in range(100):
        if calls:
            break
        threading.Event().wait(0.02)
    stop.set()
    assert calls and o.snapshot()["status"] == "match"
