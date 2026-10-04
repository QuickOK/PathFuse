"""Tests for the actual-exit check (egress_observer.py)."""
import logging
import subprocess
import sys
import threading
import time

import pytest

import egress_observer as E

MODES = {"relay_vpn", "relay_backbone", "relay_direct", "local_direct"}
RULES = (E.ExitRule("relay_vpn", "vpn", ("on", "plus")),
         E.ExitRule("relay_backbone", "ip", ("203.0.113.10",)),
         E.ExitRule("relay_direct", "ip", ("198.51.100.20",)))
URL = "https://probe.example.net/trace"
RAW_RULE = {"mode": "relay_vpn", "field": "vpn", "values": ["on"]}   # one valid raw `exits` entry


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
    # A url with no rule can never name an exit, so every check would read as a mismatch.
    # (The interval_s and mismatch_checks rows above carry no rule either: they pin the
    # order, a bad number is reported before the missing rules.)
    ({"url": "https://x"}, "exits"),
    ({"url": "https://x", "exits": []}, "exits"),
    # bounds
    ({"url": "https://x", "exits": [RAW_RULE], "interval_s": 4.9}, "interval_s"),
    ({"url": "https://x", "exits": [RAW_RULE], "interval_s": 86401}, "interval_s"),
    ({"url": "https://x", "exits": [RAW_RULE], "interval_s": float("nan")}, "interval_s"),
    ({"url": "https://x", "exits": [RAW_RULE], "interval_s": True}, "interval_s"),
    ({"url": "https://x", "exits": [RAW_RULE], "timeout_s": 0}, "timeout_s"),
    ({"url": "https://x", "exits": [RAW_RULE], "timeout_s": 121}, "timeout_s"),
    ({"url": "https://x", "exits": [RAW_RULE], "timeout_s": float("inf")}, "timeout_s"),
    ({"url": "https://x", "exits": [RAW_RULE], "timeout_s": "slow"}, "timeout_s"),
    ({"url": "https://x", "exits": [RAW_RULE], "mismatch_checks": 101}, "mismatch_checks"),
    # a mode that is not a string is a ValueError, not a TypeError out of the set lookup
    ({"url": "https://x", "exits": [{"mode": ["relay_vpn"], "field": "ip", "values": ["1"]}]}, "mode"),
    ({"url": "https://x", "exits": [{"mode": {}, "field": "ip", "values": ["1"]}]}, "mode"),
])
def test_parse_observe_cfg_rejects(raw, match):
    with pytest.raises(ValueError, match=match):
        E.parse_observe_cfg(raw, MODES)


@pytest.mark.parametrize("key, value", [
    ("interval_s", 5), ("interval_s", 86400), ("timeout_s", 0.5), ("timeout_s", 120),
    ("mismatch_checks", 1), ("mismatch_checks", 100)])
def test_parse_observe_cfg_accepts_the_bounds(key, value):
    c = E.parse_observe_cfg({"url": "https://x", "exits": [RAW_RULE], key: value}, MODES)
    assert getattr(c, key) == value


def test_parse_observe_cfg_warns_once_per_relay_mode_no_rule_names(caplog):
    with caplog.at_level(logging.WARNING):
        E.parse_observe_cfg({"url": "https://x", "exits": [
            {"mode": "relay_backbone", "field": "ip", "values": ["203.0.113.10"]}]}, MODES)
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 2
    assert sum("relay_vpn" in m for m in warned) == 1
    assert sum("relay_direct" in m for m in warned) == 1
    assert not any("relay_backbone" in m or "local_direct" in m for m in warned)


def test_parse_observe_cfg_is_quiet_when_every_relay_mode_has_a_rule(caplog):
    with caplog.at_level(logging.WARNING):
        E.parse_observe_cfg({"url": "https://x", "exits": [
            {"mode": m, "field": "ip", "values": ["1"]}
            for m in ("relay_vpn", "relay_backbone", "relay_direct")]}, MODES)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


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
        seen["argv"], seen["kw"] = argv, kw
        return R(out="ip=203.0.113.10\n")

    assert E.fetch_trace("https://probe.example.net/trace", "wg0", 8.0, run) == ("ip=203.0.113.10\n", None)
    assert seen["argv"] == ["curl", "--silent", "--show-error", "--fail", "--max-time", "8",
                            "--interface", "wg0", "https://probe.example.net/trace"]
    # decode leniently: a body that is not UTF-8 must not raise out of subprocess.run
    assert seen["kw"]["encoding"] == "utf-8" and seen["kw"]["errors"] == "replace"


def test_fetch_trace_reports_curl_failure():
    body, err = E.fetch_trace("https://x", "wg0", 8.0, lambda *a, **k: R(rc=28, err="timed out"))
    assert body is None and err.startswith("curl rc=28")


def test_fetch_trace_reports_an_http_error_status_as_a_curl_failure():
    # With --fail, curl exits 22 for an HTTP status >= 400 instead of handing back the error page.
    body, err = E.fetch_trace("https://x", "wg0", 8.0, lambda *a, **k: R(
        rc=22, err="curl: (22) The requested URL returned error: 503"))
    assert body is None and err.startswith("curl rc=22: ") and "503" in err


def test_fetch_trace_survives_a_body_that_is_not_utf8():
    def run(argv, **kw):   # a real child process stands in for curl and writes the bytes "ip=", 0xFF, "\n"
        return subprocess.run([sys.executable, "-c",
                               "import sys; sys.stdout.buffer.write(bytes([105, 112, 61, 255, 10]))"], **kw)

    assert E.fetch_trace("https://x", "wg0", 8.0, run) == ("ip=\ufffd\n", None)


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


def test_tracker_caps_the_stored_ip_text():
    t = E.ExitTracker(2, clock=Clock())
    t.select("relay_direct")
    t.update("relay_direct", "9" * 100, None)
    assert t.snapshot()["ip"] == "9" * 64
    t.update("relay_direct", "198.51.100.20", None)
    assert t.snapshot()["ip"] == "198.51.100.20"


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


def test_check_once_reports_a_fetch_error():
    o, calls = _observer(err="timeout")
    o.set_selected("relay_backbone")
    o.check_once()
    snap = o.snapshot()
    assert len(calls) == 1
    assert (snap["status"], snap["error"], snap["observed"]) == ("error", "timeout", None)


@pytest.mark.parametrize("body", [
    "",                                          # a 204, or an empty reply
    "<html>503 Service Unavailable</html>\n",    # an error or captive-portal page
    "fl=1\nloc=XX\n",                            # a trace page, but not one the rules key on
])
def test_check_once_page_without_rule_fields_is_an_error(body):
    o, _ = _observer(body=body)
    o.set_selected("relay_backbone")
    o.check_once()
    snap = o.snapshot()
    assert snap["status"] == "error" and snap["error"] == "trace page has none of the rule fields"
    assert snap["observed"] is None and snap["ip"] is None


def test_unreadable_pages_neither_count_toward_nor_reset_a_mismatch():
    pages = iter(["<html>503</html>\n", "ip=198.51.100.20\n", "<html>503</html>\n", "ip=198.51.100.20\n"])
    cfg = E.ObserveCfg(url=URL, exits=RULES)                 # mismatch_checks is 2
    o = E.EgressObserver(cfg, fetch=lambda url, iface, t: (next(pages), None), clock=Clock())
    o.set_selected("relay_backbone")
    seen = []
    for _ in range(4):
        o.check_once()
        seen.append(o.snapshot()["status"])
    # the first error leaves the count at 0 (so one real mismatch is only pending),
    # the second leaves it at 1 (so the next real mismatch reaches 2)
    assert seen == ["error", "pending", "error", "mismatch"]


def test_check_once_turns_a_raising_fetch_into_an_error():
    def fetch(url, iface, timeout_s):
        raise RuntimeError("boom")

    o = E.EgressObserver(E.ObserveCfg(url=URL, exits=RULES), fetch=fetch, clock=Clock())
    o.set_selected("relay_backbone")
    o.check_once()                                           # does not raise
    snap = o.snapshot()
    assert snap["status"] == "error" and snap["error"] == "fetch error: boom"


def test_check_once_still_counts_a_readable_page_with_an_unrecognised_exit():
    # The page carries a rule field, just not a configured value: a real "unknown" exit,
    # which counts toward a mismatch (unlike an unreadable page).
    o, _ = _observer(body="ip=192.0.2.9\nvpn=off\n")
    o.set_selected("relay_backbone")
    o.check_once()
    snap = o.snapshot()
    assert (snap["status"], snap["observed"], snap["ip"]) == ("pending", "unknown", "192.0.2.9")


def test_check_once_caps_the_ip_it_reports():
    o, _ = _observer(body="ip=" + "9" * 100 + "\n")
    o.set_selected("relay_backbone")
    o.check_once()
    assert o.snapshot()["ip"] == "9" * 64


# --- the check thread ---

def _wait_for(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    return pred()


def _stop_and_join(o, stop):
    """Stop the thread; True once it has exited. The loop sleeps on the kick, so wake it."""
    stop.set()
    o._kick.set()
    o._thread.join(timeout=2)
    return not o._thread.is_alive()


def _fast_observer():
    o, calls = _observer()
    o.cfg = E.ObserveCfg(url=URL, exits=RULES, interval_s=0.05)
    o.settle_s = 0.0
    return o, calls


def test_thread_runs_and_stops():
    o, _ = _fast_observer()
    o.set_selected("relay_backbone")
    stop = threading.Event()
    o.start(stop)
    try:
        assert _wait_for(lambda: o.snapshot()["status"] == "match")
    finally:
        exited = _stop_and_join(o, stop)
    assert exited


def test_thread_survives_a_fetch_that_raises():
    calls = []

    def fetch(url, iface, timeout_s):
        calls.append(1)
        raise RuntimeError("boom")

    o = E.EgressObserver(E.ObserveCfg(url=URL, exits=RULES, interval_s=0.05), fetch=fetch,
                         clock=Clock(), settle_s=0.0)
    o.set_selected("relay_backbone")
    stop = threading.Event()
    o.start(stop)
    try:
        # a second check proves the first raise did not end the thread
        assert _wait_for(lambda: len(calls) >= 2 and o.snapshot()["status"] == "error")
        assert o._thread.is_alive() and o.snapshot()["error"] == "fetch error: boom"
    finally:
        exited = _stop_and_join(o, stop)
    assert exited


def test_start_is_a_no_op_while_the_thread_is_alive():
    o, _ = _fast_observer()
    o.set_selected("relay_backbone")
    stop = threading.Event()
    o.start(stop)
    first = o._thread
    try:
        o.start(stop)
        assert o._thread is first
    finally:
        exited = _stop_and_join(o, stop)
    assert exited
    stop2 = threading.Event()
    o.start(stop2)                                           # the first thread is gone: this one starts
    try:
        assert o._thread is not first and o._thread.is_alive()
    finally:
        exited = _stop_and_join(o, stop2)
    assert exited


class FakeStop:
    """A stop Event whose waits return at once, so `_run` can be driven on the test's own thread."""

    def __init__(self, on_wait=None):
        self.on_wait, self.waits, self.done, self.polls = on_wait, [], False, 0

    def is_set(self):
        self.polls += 1
        assert self.polls < 50, "_run is not stopping"
        return self.done

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.on_wait:
            self.on_wait(len(self.waits))
        return self.done


def test_run_settles_before_the_first_check_of_a_selected_mode():
    log = []
    stop = FakeStop(lambda n: log.append("settle"))

    def fetch(url, iface, timeout_s):
        log.append("fetch")
        stop.done = True                                     # one check is all this run needs
        return "ip=203.0.113.10\n", None

    o = E.EgressObserver(E.ObserveCfg(url=URL, exits=RULES, interval_s=0.01), fetch=fetch,
                         clock=Clock(), settle_s=15.0)
    o.set_selected("relay_backbone")                         # selected before the thread starts
    o._run(stop)
    assert log == ["settle", "fetch"] and stop.waits == [15.0]


def test_run_settles_again_when_the_mode_changes_during_a_settle():
    log = []

    def on_wait(n):
        log.append("settle")
        if n == 1:
            o.set_selected("relay_vpn")                      # the mode changes mid-settle

    stop = FakeStop(on_wait)

    def fetch(url, iface, timeout_s):
        log.append("fetch")
        stop.done = True
        return "vpn=on\n", None

    o = E.EgressObserver(E.ObserveCfg(url=URL, exits=RULES, interval_s=0.01), fetch=fetch,
                         clock=Clock(), settle_s=15.0)
    o.set_selected("relay_backbone")
    o._run(stop)
    snap = o.snapshot()
    assert log == ["settle", "settle", "fetch"]
    assert (snap["selected"], snap["status"]) == ("relay_vpn", "match")


def test_run_checks_every_interval_and_settles_only_after_a_mode_change():
    log = []
    stop = FakeStop(lambda n: log.append("settle"))

    def fetch(url, iface, timeout_s):
        log.append("fetch")
        stop.done = log.count("fetch") == 3
        return "ip=203.0.113.10\n", None

    o = E.EgressObserver(E.ObserveCfg(url=URL, exits=RULES, interval_s=0.01), fetch=fetch,
                         clock=Clock(), settle_s=15.0)
    o.set_selected("relay_backbone")
    o._run(stop)
    assert log == ["settle", "fetch", "fetch", "fetch"]


def test_run_returns_without_checking_when_stopped_during_a_settle():
    stop = FakeStop()
    stop.on_wait = lambda n: setattr(stop, "done", True)
    o, calls = _observer()
    o.set_selected("relay_backbone")
    o._run(stop)
    assert calls == []
