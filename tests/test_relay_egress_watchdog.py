"""Tests for the relay-side egress actuator (deploy/relay/egress/).

The deployed scripts have no .py extension and use hyphens, so they are loaded
by path. These tests pin the client<->relay egress vocabulary (drift there
silently pins the relay to its default mode) and the route invariant: at most
one preferred default, and only toward a healthy upstream the mode selects.
"""
import contextlib
import http.client
import importlib.util
import io
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_DIR = _ROOT / "deploy/relay/egress"


def _load(name, filename):
    loader = SourceFileLoader(name, str(_DIR / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


M = _load("relay_egress_watchdog", "relay-egress-watchdog")

GW = "192.0.2.1"


def raw_cfg(**over):
    raw = {
        "table": "egress",
        "client": {"control_url": "http://127.0.0.1:9/x", "default_mode": "relay_direct"},
        "mode_upstreams": {"relay_vpn": "vpn", "relay_backbone": "backbone"},
        "exempt": {"via": GW, "dev": "eth0", "prefixes": ["10.0.0.0/8", "198.51.100.7/32"]},
        "upstreams": {
            "vpn": {"route": {"via": "10.200.0.2", "dev": "veth-vpn"}, "link": "veth-vpn",
                    "probe": {"url": "https://probe.example.net/trace", "netns": "vpn",
                              "expect": "^vpn=on$"}},
            "backbone": {"route": {"dev": "wg-exit"}, "link": "wg-exit",
                         "probe": {"url": "https://probe.example.net/trace",
                                   "source": "10.99.1.2",
                                   "expect": r"^ip=203\.0\.113\.10$"}},
        },
    }
    raw.update(over)
    return raw


def cfg(**over):
    return M.validate_config(raw_cfg(**over))


class R:
    """subprocess.run result stand-in."""

    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeIp:
    """One routing table with `ip route` replace/del semantics."""

    def __init__(self, routes=None, fail=()):
        self.routes = [dict(r) for r in (routes or [])]
        self.calls = []
        self.fail = set(fail)   # any argv word in here makes apply() fail

    def show_table(self, table):
        return [dict(r) for r in self.routes]

    def apply(self, argv):
        self.calls.append(list(argv))
        if self.fail & set(argv):
            return False, "RTNETLINK answers: simulated failure"
        op, dst = argv[1], argv[2]
        kv = dict(zip(argv[3::2], argv[4::2]))
        metric = int(kv.get("metric", 0))
        if dst != "default" and dst.endswith("/32"):
            dst = dst[:-3]      # the kernel lists host routes without /32
        if op == "del":
            for i, r in enumerate(self.routes):
                if r["dst"] == dst and r.get("metric", 0) == metric:
                    del self.routes[i]
                    return True, ""
            return False, "RTNETLINK answers: No such process"
        new = {"dst": dst, "dev": kv["dev"]}
        if "via" in kv:
            new["gateway"] = kv["via"]
        if metric:
            new["metric"] = metric
        self.routes = [r for r in self.routes
                       if not (r["dst"] == dst and r.get("metric", 0) == metric)]
        self.routes.append(new)
        return True, ""

    def preferred(self):
        return [r for r in self.routes if r["dst"] == "default" and r.get("metric") == 100]


BASE = [{"dst": "default", "gateway": GW, "dev": "eth0", "metric": 200}]
OK_VPN, OK_BB, FAIL = (True, "vpn=on"), (True, "ip=203.0.113.10"), (False, "timeout")


def run_tick(c, state, now, ip, probes, mode="relay_backbone", fetch_err=None):
    lines: list[str] = []

    def fetch(url, timeout):
        return (None, None, fetch_err) if fetch_err else (mode, "wan2", None)

    new, rc = M.tick(c, state, now, ip=ip, probe=lambda up: probes[up["name"]],
                     fetch=fetch, log=lines.append)
    return new, rc, lines


def _healthy_state(name, route=None):
    """A state where upstream `name` has been healthy for a while, with `route` installed."""
    return {"route": route, "upstreams": {name: dict(M.new_health(), healthy=True,
                                                     first_pass_seen=True, consecutive_pass=5)}}


def _preferred_route(c, name):
    """The preferred default toward upstream `name`, as `ip -j route show` lists it."""
    r = c["upstreams"][name]["route"]
    return dict({"dst": "default", "dev": r["dev"], "metric": 100},
                **({"gateway": r["via"]} if r["via"] else {}))


def _replaces_default(ip):
    """Every `ip route replace default ...` the tick ran."""
    return [call for call in ip.calls if call[:3] == ["route", "replace", "default"]]


# --- vocabulary ---------------------------------------------------------------

def test_vocabulary_includes_relay_backbone():
    assert M.VALID_DESIRED_MODES == {"relay_vpn", "relay_backbone", "relay_direct", "local_direct"}


def test_canonical_modes_passthrough():
    for m in M.VALID_DESIRED_MODES:
        assert M.normalize_mode(m) == m


def test_alias_names_normalized_to_canonical():
    assert M.normalize_mode("upstream_vpn") == "relay_vpn"
    assert M.normalize_mode("relay_wan") == "relay_direct"
    assert M.normalize_mode("local") == "local_direct"


def test_unknown_and_none_passthrough():
    assert M.normalize_mode("banana") == "banana"
    assert M.normalize_mode(None) is None


# --- config --------------------------------------------------------------------

def test_validate_config_fills_defaults():
    c = cfg()
    assert c["preferred_metric"] == 100 and c["dry_run"] is False
    assert c["client"]["grace_s"] == 60.0 and c["client"]["fetch_timeout_s"] == 1.0
    bb = c["upstreams"]["backbone"]
    assert bb["name"] == "backbone" and bb["route"] == {"dev": "wg-exit", "via": None}
    assert (bb["fail_threshold"], bb["pass_threshold"]) == (3, 2)
    assert bb["probe"]["timeout_s"] == 5.0
    assert c["exempt"]["prefixes"] == ["10.0.0.0/8", "198.51.100.7/32"]


def test_default_mode_defaults_to_relay_direct():
    raw = raw_cfg()
    raw["client"].pop("default_mode")
    assert M.validate_config(raw)["client"]["default_mode"] == "relay_direct"


@pytest.mark.parametrize("mutate, match", [
    (lambda c: c.update(table=""), "table"),
    (lambda c: c["client"].update(default_mode="banana"), "default_mode"),
    (lambda c: c["mode_upstreams"].update(relay_direct="vpn"), "never uses an upstream"),
    (lambda c: c["mode_upstreams"].update(relay_vpn="nope"), "not a configured upstream"),
    (lambda c: c["mode_upstreams"].update(banana="vpn"), "unknown mode"),
    (lambda c: c["upstreams"]["vpn"]["probe"].update(source="10.99.1.2"), "not both"),
    (lambda c: c["upstreams"]["vpn"]["probe"].update(expect="("), "expect"),
    (lambda c: c["upstreams"]["vpn"]["route"].update(dev="bad name!"), "interface"),
    (lambda c: c["exempt"].update(prefixes=["10.0.0.1/8"]), "exempt.prefixes"),
    (lambda c: c["upstreams"]["vpn"].update(fail_threshold=0), "fail_threshold"),
    (lambda c: c.update(dry_run="yes"), "dry_run"),
    (lambda c: c["client"].update(control_url="ftp://example.com"), "http://"),
    pytest.param(lambda c: c["client"].update(control_url=" http://127.0.0.1:9/x"), "http://",
                 id="url-leading-space"),
    pytest.param(lambda c: c["client"].update(control_url="http://[::1/x"), "not a valid URL",
                 id="url-unclosed-bracket"),
    pytest.param(lambda c: c["client"].update(control_url="http://127.0.0.1:99999/x"),
                 "not a valid URL", id="url-port-out-of-range"),
    pytest.param(lambda c: c["client"].update(control_url="http://127.0.0.1:abc/x"),
                 "not a valid URL", id="url-port-not-a-number"),
    pytest.param(lambda c: c["client"].update(control_url="http://"), "with a host",
                 id="url-no-host"),
    pytest.param(lambda c: c["client"].update(control_url="https:///x"), "with a host",
                 id="url-no-host-with-path"),
    (lambda c: c["client"].update(default_mode=["relay_vpn"]), "must be a string"),
    (lambda c: c["mode_upstreams"].update({123: "vpn"}), "key must be a string"),
    (lambda c: c["mode_upstreams"].update(relay_vpn=["backbone"]), "value must be a string"),
    (lambda c: c["exempt"].update(prefixes=["0.0.0.0/0"]), "prefix length 0"),
    pytest.param(lambda c: c["exempt"].update(prefixes=["not-a-prefix"]), "exempt.prefixes",
                 id="exempt-unparsable"),
    # A tick must end within the unit's TimeoutStartSec=25 (see MAX_PROBE_TIMEOUT_S).
    pytest.param(lambda c: c["upstreams"]["vpn"]["probe"].update(timeout_s=10.5),
                 r"^upstreams\.vpn\.probe\.timeout_s must be at most 10 s",
                 id="probe-timeout-over-10"),
    pytest.param(lambda c: c["client"].update(fetch_timeout_s=10.5),
                 r"^client\.fetch_timeout_s must be at most 10 s", id="fetch-timeout-over-10"),
    pytest.param(lambda c: c["client"].update(bootstrap_timeout_s=15.5),
                 r"^client\.bootstrap_timeout_s must be at most 15 s",
                 id="bootstrap-timeout-over-15"),
])
def test_validate_config_rejects(mutate, match):
    raw = raw_cfg()
    mutate(raw)
    with pytest.raises(M.ConfigError, match=match):
        M.validate_config(raw)


def test_validate_config_accepts_timeouts_at_their_caps():
    raw = raw_cfg()
    raw["upstreams"]["vpn"]["probe"]["timeout_s"] = 10
    raw["client"].update(fetch_timeout_s=10, bootstrap_timeout_s=15)
    c = M.validate_config(raw)
    assert (c["upstreams"]["vpn"]["probe"]["timeout_s"], c["client"]["fetch_timeout_s"],
            c["client"]["bootstrap_timeout_s"]) == (10.0, 10.0, 15.0)


@pytest.mark.parametrize("url", ["", "https://relay.example.net/api/desired_egress",
                                 "http://[::1]:8081/x"])
def test_validate_config_accepts_control_urls(url):
    """An empty URL turns polling off; any other needs an http(s) scheme and a host."""
    raw = raw_cfg()
    raw["client"]["control_url"] = url
    assert M.validate_config(raw)["client"]["control_url"] == url


@pytest.mark.parametrize("url", [123, True, ["http://127.0.0.1:9/x"], [], {"u": 1}],
                         ids=["int", "bool", "list", "empty-list", "dict"])
def test_validate_config_rejects_a_control_url_that_is_not_a_string(url):
    """Without the type check urlsplit raises AttributeError or TypeError on these, which
    escapes load_config (main() crashes with exit 1, not a CONFIG ERROR exit 2), and an
    empty list reads as "polling off"."""
    raw = raw_cfg()
    raw["client"]["control_url"] = url
    with pytest.raises(M.ConfigError, match="control_url must be a string"):
        M.validate_config(raw)


def test_validate_config_rejects_zero_prefix_with_exact_message():
    """Verify the /0 error message is single-prefixed, not double."""
    raw = raw_cfg()
    raw["exempt"]["prefixes"] = ["0.0.0.0/0"]
    with pytest.raises(M.ConfigError) as exc_info:
        M.validate_config(raw)
    # Message should be single-prefixed, not double
    msg = str(exc_info.value)
    assert msg == "exempt.prefixes: '0.0.0.0/0': prefix length 0 would route everything"
    # Should not contain double prefix like "exempt.prefixes: ... exempt.prefixes: ..."
    assert msg.count("exempt.prefixes:") == 1


def test_load_config_unreadable_is_config_error(tmp_path):
    with pytest.raises(M.ConfigError):
        M.load_config(tmp_path / "missing.json")


# --- health hysteresis ---------------------------------------------------------------

def test_first_pass_ever_is_healthy_at_once():
    h = M.advance_health(M.new_health(), True, "vpn=on", 1.0, 3, 2)
    assert h["healthy"] is True and h["last_change"] == 1.0


def test_unhealthy_only_after_fail_threshold():
    h = M.advance_health(M.new_health(), True, "ok", 1.0, 3, 2)
    for i in range(2):
        h = M.advance_health(h, False, "timeout", 2.0 + i, 3, 2)
        assert h["healthy"] is True
    h = M.advance_health(h, False, "timeout", 5.0, 3, 2)
    assert h["healthy"] is False and h["last_change"] == 5.0


def test_recovery_needs_pass_threshold():
    h = M.advance_health(M.new_health(), True, "ok", 1.0, 1, 2)
    h = M.advance_health(h, False, "x", 2.0, 1, 2)
    assert h["healthy"] is False
    h = M.advance_health(h, True, "ok", 3.0, 1, 2)
    assert h["healthy"] is False
    h = M.advance_health(h, True, "ok", 4.0, 1, 2)
    assert h["healthy"] is True


def test_never_passed_stays_unhealthy():
    h = M.advance_health(M.new_health(), False, "x", 1.0, 3, 2)
    assert h["healthy"] is False and h["first_pass_seen"] is False


# --- probes ----------------------------------------------------------------------

def test_probe_argv_netns():
    assert M.probe_argv(cfg()["upstreams"]["vpn"]["probe"]) == [
        "ip", "netns", "exec", "vpn", "curl", "--silent", "--show-error",
        "--max-time", "5", "https://probe.example.net/trace"]


def test_probe_argv_source():
    assert M.probe_argv(cfg()["upstreams"]["backbone"]["probe"]) == [
        "curl", "--silent", "--show-error", "--max-time", "5",
        "--interface", "10.99.1.2", "https://probe.example.net/trace"]


def test_probe_matches_is_line_anchored():
    body = "fl=1\nip=203.0.113.10\nvpn=off\n"
    assert M.probe_matches(body, r"^ip=203\.0\.113\.10$") == "ip=203.0.113.10"
    assert M.probe_matches(body, "^vpn=on$") is None


def _runner(flags=("UP", "LOWER_UP"), body="vpn=on\n", rc=0):
    def run(argv, **kw):
        if argv[:3] == ["ip", "-j", "link"]:
            return R(out=json.dumps([{"ifname": argv[-1], "flags": list(flags)}]))
        return R(rc=rc, out=body, err="boom" if rc else "")
    return run


def test_run_probe_ok():
    assert M.run_probe(cfg()["upstreams"]["vpn"], _runner()) == (True, "vpn=on", False)


def test_run_probe_link_down_skips_curl():
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return R(out=json.dumps([{"flags": ["BROADCAST"]}]))

    ok, detail, hard = M.run_probe(cfg()["upstreams"]["vpn"], run)
    assert not ok and detail == "link veth-vpn down" and hard and len(calls) == 1


def test_run_probe_curl_failure_and_unexpected_body():
    """Soft failures: the hysteresis decides when they make the upstream unhealthy."""
    up = cfg()["upstreams"]["vpn"]
    ok, detail, hard = M.run_probe(up, _runner(rc=28))
    assert not ok and detail.startswith("curl rc=28") and not hard
    assert M.run_probe(up, _runner(body="vpn=off\n")) == (False, "unexpected body", False)


@pytest.mark.parametrize("answer", [
    pytest.param(R(rc=1, err='Device "veth-vpn" does not exist.'), id="missing"),
    pytest.param(R(out=json.dumps([{"ifname": "veth-vpn", "flags": ["BROADCAST", "NOARP"]}])),
                 id="not-admin-up"),
])
def test_run_probe_a_missing_or_downed_link_is_a_hard_failure(answer):
    """`wg-quick down` deletes its device; `ip link set ... down` clears UP. Either way
    the upstream cannot carry traffic, so the failure skips the hysteresis."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return answer

    assert M.run_probe(cfg()["upstreams"]["vpn"], run) == (False, "link veth-vpn down", True)
    assert calls == [["ip", "-j", "link", "show", "dev", "veth-vpn"]]   # and no curl


@pytest.mark.parametrize("answer", [
    pytest.param(subprocess.TimeoutExpired("ip", 2), id="ip-hangs"),
    pytest.param(OSError("No such file or directory: 'ip'"), id="ip-cannot-run"),
    pytest.param(R(out="not json"), id="garbage"),
    pytest.param(R(out="[]"), id="empty-list"),
])
def test_run_probe_a_link_state_ip_cannot_tell_is_a_soft_failure(answer):
    """Only a link known to be missing or down skips the hysteresis. A hiccup of `ip`
    itself is an ordinary failure."""
    def run(argv, **kw):
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert M.run_probe(cfg()["upstreams"]["vpn"], run) == (
        False, "cannot check link veth-vpn", False)


# --- selection and planning -----------------------------------------------------------

def healthy(*names):
    return {n: dict(M.new_health(), healthy=n in names) for n in ("vpn", "backbone")}


@pytest.mark.parametrize("mode, up, want", [
    ("relay_vpn", ("vpn",), "vpn"),
    ("relay_backbone", ("backbone",), "backbone"),
    ("relay_backbone", ("vpn",), None),
    ("relay_direct", ("vpn", "backbone"), None),
    ("local_direct", ("vpn", "backbone"), None),
])
def test_select_route(mode, up, want):
    c = cfg()
    _target, route = M.select_route(c, mode, healthy(*up))
    assert route == (c["upstreams"][want]["route"] if want else None)


def test_plan_installs_preferred_and_exemptions_on_bare_table():
    c = cfg()
    pref, ex = M.plan_actions(c, BASE, c["upstreams"]["backbone"]["route"])
    assert pref == [["route", "replace", "default", "dev", "wg-exit",
                     "metric", "100", "table", "egress"]]
    assert ex == [
        ["route", "replace", "10.0.0.0/8", "via", GW, "dev", "eth0", "table", "egress"],
        ["route", "replace", "198.51.100.7/32", "via", GW, "dev", "eth0", "table", "egress"]]


def test_plan_is_empty_when_table_already_right():
    c = cfg()
    routes = BASE + [{"dst": "default", "dev": "wg-exit", "metric": 100},
                     {"dst": "10.0.0.0/8", "gateway": GW, "dev": "eth0"},
                     {"dst": "198.51.100.7", "gateway": GW, "dev": "eth0"}]
    assert M.plan_actions(c, routes, c["upstreams"]["backbone"]["route"]) == ([], [])


def test_plan_withdraws_every_preferred_when_none_wanted():
    routes = BASE + [{"dst": "default", "dev": "wg-exit", "metric": 100},
                     {"dst": "default", "gateway": "10.200.0.2", "dev": "veth-vpn", "metric": 100}]
    pref, _ = M.plan_actions(cfg(), routes, None)
    assert pref == [["route", "del", "default", "metric", "100", "table", "egress"]] * 2


def test_plan_switch_is_a_single_replace():
    c = cfg()
    routes = BASE + [{"dst": "default", "gateway": "10.200.0.2", "dev": "veth-vpn", "metric": 100}]
    pref, _ = M.plan_actions(c, routes, c["upstreams"]["backbone"]["route"])
    assert pref == [["route", "replace", "default", "dev", "wg-exit",
                     "metric", "100", "table", "egress"]]


# --- tick ------------------------------------------------------------------------

def test_tick_installs_backbone_when_selected_and_healthy():
    c, ip = cfg(), FakeIp(BASE)
    new, rc, lines = run_tick(c, {}, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB})
    assert rc == 0 and new["route"] == "backbone"
    assert ip.preferred() == [{"dst": "default", "dev": "wg-exit", "metric": 100}]
    assert any(line.startswith("ROUTE: none -> backbone") for line in lines)


def test_tick_fails_open_after_fail_threshold():
    c = cfg()
    ip = FakeIp(BASE + [{"dst": "default", "dev": "wg-exit", "metric": 100}])
    state = {"route": "backbone", "upstreams": {
        "backbone": dict(M.new_health(), healthy=True, first_pass_seen=True)}}
    for t in range(2):
        state, _, _ = run_tick(c, state, 100.0 + t, ip, {"vpn": OK_VPN, "backbone": FAIL})
        assert len(ip.preferred()) == 1
    state, _, lines = run_tick(c, state, 102.0, ip, {"vpn": OK_VPN, "backbone": FAIL})
    assert ip.preferred() == [] and state["route"] is None
    assert any(line.startswith("HEALTH: backbone healthy -> unhealthy") for line in lines)


def _real_probe(missing=(), curl_rc=0):
    """The real run_probe over a fake runner: `ip -j link show` fails for a link in
    `missing`, as for a deleted device, and finds any other link UP; curl prints a
    body both upstreams' probes accept, or fails with `curl_rc`."""
    def run(argv, **kw):
        if argv[:3] == ["ip", "-j", "link"]:
            if argv[-1] in missing:
                return R(rc=1, err=f'Device "{argv[-1]}" does not exist.')
            return R(out=json.dumps([{"ifname": argv[-1], "flags": ["UP", "LOWER_UP"]}]))
        if curl_rc:
            return R(rc=curl_rc, err="curl: (28) Operation timed out")
        return R(out="vpn=on\nip=203.0.113.10\n")
    return lambda up: M.run_probe(up, run)


def _backbone(url, timeout):
    return "relay_backbone", "wan2", None


@pytest.mark.parametrize("route_left", [True, False], ids=["route-still-listed", "route-gone"])
def test_a_link_that_goes_down_withdraws_its_route_on_the_first_tick(route_left):
    """The fail-open drill: stopping the backbone's tunnel deletes its device, and the
    kernel drops the routes through it. Waiting out fail_threshold would leave the
    upstream "healthy" for two more ticks, each trying to re-install its route on the
    missing device: an ERROR, exit 1 and a dead-man run every 10 s."""
    c = cfg()
    ip = FakeIp(BASE + ([_preferred_route(c, "backbone")] if route_left else []),
                fail={"wg-exit"})   # the device is gone: any command naming it fails
    lines: list[str] = []
    new, rc = M.tick(c, _healthy_state("backbone", route="backbone"), 100.0, ip=ip,
                     probe=_real_probe(missing={"wg-exit"}), fetch=_backbone, log=lines.append)
    assert rc == 0 and new["route"] is None and ip.preferred() == []
    assert _replaces_default(ip) == []
    assert not any(line.startswith("ERROR") for line in lines)
    assert "HEALTH: backbone healthy -> unhealthy (link wg-exit down)" in lines
    assert "ROUTE: backbone -> none (mode=relay_backbone)" in lines


def test_a_failing_probe_on_an_up_link_still_waits_for_fail_threshold():
    """Only a missing or downed link skips the hysteresis. The probe itself failing,
    a blip on the path, still takes fail_threshold (3) failures in a row."""
    c = cfg()
    ip = FakeIp(BASE + [_preferred_route(c, "backbone")])
    state = _healthy_state("backbone", route="backbone")
    probe = _real_probe(curl_rc=28)
    for t in range(2):
        state, rc = M.tick(c, state, 100.0 + t, ip=ip, probe=probe, fetch=_backbone,
                           log=lambda line: None)
        assert rc == 0 and state["route"] == "backbone" and len(ip.preferred()) == 1
        assert state["upstreams"]["backbone"]["last_probe"].startswith("curl rc=28")
    state, rc = M.tick(c, state, 102.0, ip=ip, probe=probe, fetch=_backbone,
                       log=lambda line: None)
    assert rc == 0 and state["route"] is None and ip.preferred() == []


def test_tick_falls_back_to_relay_direct_past_grace():
    c, ip = cfg(), FakeIp(BASE)
    state, _, _ = run_tick(c, {}, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB}, mode="relay_vpn")
    assert ip.preferred()[0]["dev"] == "veth-vpn"
    state, _, _ = run_tick(c, state, 200.0, ip, {"vpn": OK_VPN, "backbone": OK_BB},
                           fetch_err="timeout")
    assert ip.preferred() == [] and state["effective_mode"] == "relay_direct"


def test_tick_counts_rejected_modes():
    c, ip = cfg(), FakeIp(BASE)
    state, _, _ = run_tick(c, {}, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB},
                           fetch_err="invalid mode: 'banana'")
    state, _, _ = run_tick(c, state, 110.0, ip, {"vpn": OK_VPN, "backbone": OK_BB},
                           fetch_err="invalid mode: 'banana'")
    assert state["desired_mode_fetch_fail"] == 2


def test_at_most_one_preferred_route_across_mode_and_health_changes():
    c, ip = cfg(), FakeIp(BASE)
    seq = [("relay_vpn", OK_VPN, OK_BB), ("relay_backbone", OK_VPN, OK_BB),
           ("relay_backbone", OK_VPN, FAIL), ("relay_backbone", OK_VPN, FAIL),
           ("relay_backbone", OK_VPN, FAIL), ("relay_vpn", OK_VPN, FAIL),
           ("relay_direct", OK_VPN, OK_BB), ("relay_backbone", FAIL, OK_BB),
           ("relay_backbone", OK_VPN, OK_BB), ("local_direct", OK_VPN, OK_BB)]
    state: dict = {}
    for i, (mode, pv, pb) in enumerate(seq):
        state, rc, _ = run_tick(c, state, 100.0 + i, ip, {"vpn": pv, "backbone": pb}, mode=mode)
        assert rc == 0
        pref = ip.preferred()
        assert len(pref) <= 1
        _target, route = M.select_route(c, mode, state["upstreams"])
        if route is None:
            assert pref == []
        else:
            assert (pref[0].get("gateway"), pref[0]["dev"]) == (route["via"], route["dev"])


def test_dry_run_issues_no_route_commands():
    c, ip = cfg(dry_run=True), FakeIp(BASE)
    new, rc, lines = run_tick(c, {}, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB})
    assert ip.calls == [] and rc == 0 and new["route"] == "backbone"
    assert any("DRY-RUN would run: ip route replace default dev wg-exit" in line for line in lines)


def test_preferred_failure_fails_open_and_exits_1():
    c = cfg()
    ip = FakeIp(BASE + [{"dst": "default", "gateway": "10.200.0.2", "dev": "veth-vpn", "metric": 100}],
                fail={"wg-exit"})
    new, rc, lines = run_tick(c, {}, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB})
    assert rc == 1 and ip.preferred() == [] and new["route"] is None
    assert any(line.startswith("ERROR: ip route replace default dev wg-exit") for line in lines)


HOST_EXEMPT = "198.51.100.7/32"   # the exemption these tests make fail
EXEMPT_ALL = raw_cfg()["exempt"]["prefixes"]


def _exempt_error(named):
    return f"ERROR: exemption {named} not in place: no preferred route this tick (fail open)"


@pytest.mark.parametrize("fail", [[HOST_EXEMPT], EXEMPT_ALL], ids=["one", "all"])
def test_an_exemption_that_cannot_be_added_blocks_the_preferred_route(fail):
    """The selected upstream is healthy, but an exempt prefix would follow its default
    into the upstream. Exit 0: the tick has failed open itself, and a failed tick
    would only fire the dead-man every 10 s."""
    c, ip = cfg(), FakeIp(BASE, fail=fail)
    new, rc, lines = run_tick(c, {}, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB})
    assert rc == 0 and new["route"] is None and new["target"] == "backbone"
    assert ip.preferred() == [] and _replaces_default(ip) == []   # not even for a moment
    assert _exempt_error(", ".join(fail)) in lines
    assert any(line.startswith(f"ERROR: ip route replace {HOST_EXEMPT} ") for line in lines)


@pytest.mark.parametrize("seeded", ["backbone", "vpn"], ids=["would-keep", "would-switch"])
def test_an_exemption_that_cannot_be_added_withdraws_the_preferred_route(seeded):
    """Whether the tick would keep the route it finds (backbone, already right) or
    switch it (vpn, to backbone), a missing exemption leaves no preferred route."""
    c = cfg()
    ip = FakeIp(BASE + [_preferred_route(c, seeded)], fail={HOST_EXEMPT})
    new, rc, lines = run_tick(c, _healthy_state(seeded, route=seeded), 100.0, ip,
                              {"vpn": OK_VPN, "backbone": OK_BB})
    assert rc == 0 and new["route"] is None and ip.preferred() == []
    assert _replaces_default(ip) == []
    assert f"ROUTE: {seeded} -> none (mode=relay_backbone)" in lines
    assert _exempt_error(HOST_EXEMPT) in lines


def test_an_exemption_that_cannot_be_added_withdraws_every_preferred_default():
    """Two preferred defaults stand (one per upstream). A missing exemption must take
    both away: either one left behind would carry the exempt prefix into its upstream."""
    c = cfg()
    ip = FakeIp(BASE + [_preferred_route(c, "backbone"), _preferred_route(c, "vpn")],
                fail={HOST_EXEMPT})
    new, rc, lines = run_tick(c, _healthy_state("backbone", route="backbone"), 100.0, ip,
                              {"vpn": OK_VPN, "backbone": OK_BB})
    assert rc == 0 and new["route"] is None
    assert ip.preferred() == [] and _replaces_default(ip) == []
    assert _exempt_error(HOST_EXEMPT) in lines


def test_the_preferred_route_returns_once_the_exemption_is_in_place():
    c, ip = cfg(), FakeIp(BASE, fail={HOST_EXEMPT})
    probes = {"vpn": OK_VPN, "backbone": OK_BB}
    state, rc, _ = run_tick(c, {}, 100.0, ip, probes)
    assert rc == 0 and ip.preferred() == []
    ip.fail = set()   # the WAN route can be added again
    state, rc, lines = run_tick(c, state, 110.0, ip, probes)
    assert rc == 0 and state["route"] == "backbone"
    assert {"dst": "198.51.100.7", "gateway": GW, "dev": "eth0"} in ip.routes
    assert ip.preferred() == [_preferred_route(c, "backbone")]
    assert "ROUTE: none -> backbone (mode=relay_backbone)" in lines
    assert not any(line.startswith("ERROR") for line in lines)


def test_an_exemption_failure_whose_withdraw_also_fails_exits_1():
    """A preferred default may then still stand, so the dead-man must get its turn."""
    c = cfg()
    ip = FakeIp(BASE + [_preferred_route(c, "backbone")], fail={HOST_EXEMPT, "del"})
    new, rc, lines = run_tick(c, _healthy_state("backbone", route="backbone"), 100.0, ip,
                              {"vpn": OK_VPN, "backbone": OK_BB})
    assert rc == 1 and new["route"] is None
    assert any(line.startswith("ERROR: ip route del default metric 100 table egress: ")
               for line in lines)


def test_unreadable_table_fails_open():
    class Broken(FakeIp):
        def show_table(self, table):
            raise RuntimeError("table id value is invalid")

    ip = Broken(BASE + [{"dst": "default", "dev": "wg-exit", "metric": 100}])
    _new, rc, _ = run_tick(cfg(), {}, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB})
    assert rc == 1 and ip.preferred() == []


# --- state and entry point ----------------------------------------------------------

def test_with_defaults_drops_unknown_upstreams_and_keeps_known_health():
    s = M.with_defaults({"upstreams": {"gone": {"healthy": True},
                                       "vpn": {"healthy": True}}}, cfg())
    assert set(s["upstreams"]) == {"vpn", "backbone"}
    assert s["upstreams"]["vpn"]["healthy"] is True
    assert s["upstreams"]["backbone"] == M.new_health()


def test_load_state_tolerates_garbage(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    assert M.load_state(p) == {}


DEEP = 100_000   # far past the JSON parser's nesting limit


@pytest.mark.parametrize("text", ["[" * DEEP, "[" * DEEP + "]" * DEEP, '{"a":' * DEEP],
                         ids=["unclosed-lists", "closed-lists", "objects"])
def test_load_state_tolerates_a_deeply_nested_file(tmp_path, text):
    """json.loads raises RecursionError here, which is not a ValueError."""
    p = tmp_path / "s.json"
    p.write_text(text)
    assert M.load_state(p) == {}


def test_main_runs_the_tick_over_a_deeply_nested_state_file(tmp_path, monkeypatch):
    """The state file is only the tick's own memory, so a corrupt one means a fresh start.
    Raised out of main(), it would fail every tick until someone deleted the file, each
    failure firing the dead-man switch."""
    state = tmp_path / "state.json"
    state.write_text("[" * DEEP)
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "egress", "state_path": str(state)}))
    monkeypatch.setattr(M, "IpRoute", lambda: FakeIp(BASE))
    assert M.main(["--config", str(p)]) == 0
    assert isinstance(json.loads(state.read_text()), dict)   # replaced by a fresh state


def test_main_returns_2_on_bad_config(tmp_path, capsys):
    p = tmp_path / "c.json"
    p.write_text("{}")
    assert M.main(["--config", str(p)]) == 2
    assert "CONFIG ERROR" in capsys.readouterr().err


def test_every_tick_writes_the_config_table_into_the_state():
    """The dead-man reads it there when the config is unreadable. Written every tick,
    so a renamed table replaces the old name."""
    new, _, _ = run_tick(cfg(), {"table": "old-name"}, 100.0, FakeIp(BASE),
                         {"vpn": OK_VPN, "backbone": OK_BB})
    assert new["table"] == "egress"


@pytest.mark.parametrize("readable", [True, False], ids=["tick-ok", "tick-fails"])
def test_main_saves_the_table_even_from_a_failing_tick(tmp_path, monkeypatch, readable):
    """A failing tick is what fires the dead-man, so its state must name the table too."""
    class Broken(FakeIp):
        def show_table(self, table):
            raise RuntimeError("table id value is invalid")

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"table": "old-name"}))
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "egress", "state_path": str(state)}))
    monkeypatch.setattr(M, "IpRoute", lambda: (FakeIp if readable else Broken)(BASE))
    assert M.main(["--config", str(p)]) == (0 if readable else 1)
    assert json.loads(state.read_text())["table"] == "egress"


# --- the real fetch path against a stub client endpoint -------------------------------

def _serve_once(payload):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            b = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    return srv


@pytest.mark.parametrize("mode", ["relay_backbone", "relay_direct"])
def test_fetch_accepts_canonical_modes(mode):
    srv = _serve_once({"mode": mode, "master_wan": "wan2", "ts": 1.0})
    port = srv.server_address[1]
    try:
        assert M.fetch_desired_mode(f"http://127.0.0.1:{port}/x", 2.0) == (mode, "wan2", None)
    finally:
        srv.server_close()


def test_fetch_accepts_alias_and_normalizes():
    srv = _serve_once({"mode": "relay_wan", "master_wan": "wan2", "ts": 1.0})
    port = srv.server_address[1]
    try:
        mode, _master, err = M.fetch_desired_mode(f"http://127.0.0.1:{port}/x", 2.0)
        assert mode == "relay_direct" and err is None
    finally:
        srv.server_close()


def test_fetch_rejects_unknown_mode():
    srv = _serve_once({"mode": "banana"})
    port = srv.server_address[1]
    try:
        mode, _master, err = M.fetch_desired_mode(f"http://127.0.0.1:{port}/x", 2.0)
        assert mode is None and "invalid mode" in err
    finally:
        srv.server_close()


def test_fetch_rejects_non_string_mode():
    srv = _serve_once({"mode": ["relay_backbone"]})
    port = srv.server_address[1]
    try:
        mode, _master, err = M.fetch_desired_mode(f"http://127.0.0.1:{port}/x", 2.0)
        assert mode is None and "invalid mode" in err
    finally:
        srv.server_close()


def test_fetch_handles_http_incomplete_read():
    """Test that fetch_desired_mode handles IncompleteRead (Content-Length but closes early)."""
    import socket
    import threading

    def server_incomplete():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        def handle():
            conn, _ = sock.accept()
            try:
                conn.recv(1024)  # read request
                # Send incomplete response: Content-Length says 100 but send only 10 bytes
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
                conn.sendall(b"short")
            finally:
                conn.close()
                sock.close()

        threading.Thread(target=handle, daemon=True).start()
        return port

    port = server_incomplete()
    mode, master, err = M.fetch_desired_mode(f"http://127.0.0.1:{port}/x", 2.0)
    assert mode is None and err is not None and "protocol" in err


def test_fetch_handles_http_bad_status_line():
    """Test that fetch_desired_mode handles BadStatusLine (garbage response)."""
    import socket
    import threading

    def server_badline():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        def handle():
            conn, _ = sock.accept()
            try:
                conn.recv(1024)  # read request
                # Send garbage instead of HTTP
                conn.sendall(b"GARBAGE\r\n")
            finally:
                conn.close()
                sock.close()

        threading.Thread(target=handle, daemon=True).start()
        return port

    port = server_badline()
    mode, master, err = M.fetch_desired_mode(f"http://127.0.0.1:{port}/x", 2.0)
    assert mode is None and err is not None and "protocol" in err


def test_tick_with_fetch_error_counts_failure():
    c, ip = cfg(), FakeIp(BASE)
    state, rc, _ = run_tick(c, {}, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB},
                            fetch_err="fetch error: connection reset")
    assert rc == 0 and state["desired_mode_fetch_fail"] == 1
    state, rc, _ = run_tick(c, state, 110.0, ip, {"vpn": OK_VPN, "backbone": OK_BB},
                            fetch_err="fetch error: connection reset")
    assert rc == 0 and state["desired_mode_fetch_fail"] == 2 and state["effective_mode"] == "relay_direct"


@pytest.mark.parametrize("exc", [
    pytest.param(ValueError("Invalid IPv6 URL"), id="value-error"),
    pytest.param(http.client.BadStatusLine("x"), id="bad-status-line"),
    pytest.param(RuntimeError("boom"), id="runtime-error"),
])
def test_tick_survives_a_fetch_that_raises(exc):
    """A poll glitch is a counted fetch error, never a failed tick: a failed tick
    fires the dead-man switch, which withdraws a healthy preferred route."""
    c, ip = cfg(), FakeIp(BASE)
    lines: list[str] = []

    def fetch(url, timeout):
        raise exc

    new, rc = M.tick(c, {}, 100.0, ip=ip, probe=lambda up: (True, "ok"), fetch=fetch,
                     log=lines.append)
    assert rc == 0 and new["desired_mode_fetch_fail"] == 1
    assert any("fetch_err=fetch error:" in line for line in lines)


def test_tick_survives_the_real_fetch_on_an_unparseable_control_url():
    """urlopen raises ValueError (not URLError) for this URL. validate_config now
    rejects it, so it is forced in here: the tick's own guard must hold anyway."""
    c, ip = cfg(), FakeIp(BASE)
    lines: list[str] = []
    c["client"]["control_url"] = "http://[::1/x"
    new, rc = M.tick(c, {}, 100.0, ip=ip, probe=lambda up: (True, "ok"), log=lines.append)
    assert rc == 0 and new["desired_mode_fetch_fail"] == 1
    assert any("fetch_err=fetch error: Invalid IPv6 URL" in line for line in lines)


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit], ids=["ctrl-c", "exit"])
def test_tick_does_not_swallow_an_interrupt_from_the_fetch(exc):
    """The fetch guard is for a poll glitch (an Exception). A BaseException that is not
    one, such as ^C, must still stop the tick, not count as a failed fetch."""
    c, ip = cfg(), FakeIp(BASE)

    def fetch(url, timeout):
        raise exc()

    with pytest.raises(exc):
        M.tick(c, {}, 100.0, ip=ip, probe=lambda up: (True, "ok"), fetch=fetch,
               log=lambda line: None)


def test_choose_fetch_timeout_cold_start_uses_bootstrap():
    assert M.choose_fetch_timeout(None, 5.0, 1.0) == 5.0


def test_choose_fetch_timeout_known_mode_uses_regular():
    assert M.choose_fetch_timeout("relay_backbone", 5.0, 1.0) == 1.0


def test_plan_actions_with_duplicate_stale_defaults():
    c = cfg()
    routes = BASE + [{"dst": "default", "gateway": "10.200.0.2", "dev": "veth-vpn", "metric": 100},
                     {"dst": "default", "dev": "wg-exit", "metric": 100}]
    pref, _ = M.plan_actions(c, routes, c["upstreams"]["backbone"]["route"])
    # Should have del, del, replace
    assert len([a for a in pref if a[1] == "del"]) == 2
    assert len([a for a in pref if a[1] == "replace"]) == 1


def test_tick_collapses_duplicate_metric_100_defaults():
    c, ip = cfg(), FakeIp()
    # Seed routes with two metric-100 defaults
    ip.routes = [{"dst": "default", "gateway": "10.200.0.2", "dev": "veth-vpn", "metric": 100},
                 {"dst": "default", "dev": "wg-exit", "metric": 100}]
    state, rc, _ = run_tick(c, {}, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB})
    assert rc == 0
    pref = ip.preferred()
    assert len(pref) == 1


def test_iproute_parses_real_json():
    r = M.IpRoute()
    # Mock the runner to return valid JSON
    runner = lambda argv, **kw: R(out='[{"dst":"default","dev":"eth0","metric":200}]')
    r._run = runner
    routes = r.show_table("main")
    assert len(routes) == 1 and routes[0]["dst"] == "default"


def test_iproute_raises_on_nonzero_rc():
    r = M.IpRoute()
    runner = lambda argv, **kw: R(rc=1, err="table id value is invalid")
    r._run = runner
    with pytest.raises(RuntimeError, match="table id"):
        r.show_table("bad")


def test_iproute_empty_stdout_gives_empty_list():
    r = M.IpRoute()
    runner = lambda argv, **kw: R(out="")
    r._run = runner
    assert r.show_table("main") == []


def test_iproute_timeout_propagates():
    r = M.IpRoute()
    def runner(argv, **kw):
        raise subprocess.TimeoutExpired("ip", 5)
    r._run = runner
    with pytest.raises(subprocess.TimeoutExpired):
        r.show_table("main")


def test_iproute_apply_timeout_returns_false():
    r = M.IpRoute()
    def runner(argv, **kw):
        raise subprocess.TimeoutExpired("ip", 5)
    r._run = runner
    ok, err = r.apply(["route", "replace", "default"])
    assert ok is False and "ip" in err and "5" in err


def test_probe_that_raises_records_failure():
    """A probe callable that raises for one upstream is caught, recorded as unhealthy."""
    c, ip = cfg(), FakeIp(BASE)

    def mixed_probe(up):
        if up["name"] == "vpn":
            raise RuntimeError("vpn probe crashed")
        return (True, "backbone ok")

    def fetch(url, timeout):
        return ("relay_backbone", "wan2", None)

    new, rc = M.tick(c, {}, 100.0, ip=ip, probe=mixed_probe, fetch=fetch, log=lambda x: None)

    # vpn failed: should be unhealthy with error in last_probe
    assert new["upstreams"]["vpn"]["healthy"] is False
    assert new["upstreams"]["vpn"]["last_probe"].startswith("probe error:")
    # backbone succeeded: should be healthy
    assert new["upstreams"]["backbone"]["healthy"] is True
    # tick should still succeed
    assert rc == 0


def test_default_with_no_metric_not_preferred():
    c = cfg()
    routes = [{"dst": "default", "dev": "eth0"}]  # metric defaults to 0
    pref, _ = M.plan_actions(c, routes, None)
    assert pref == []  # No actions; metric 0 default is not touched


def test_exemptions_are_installed_before_the_preferred_default():
    """Otherwise the exempt prefixes would briefly leave through the upstream."""
    c, ip = cfg(), FakeIp(BASE)
    run_tick(c, {}, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB})
    # ip.calls holds argv tails ["route", op, dst, ...]: dst is the third word
    assert [call[2] for call in ip.calls] == ["10.0.0.0/8", "198.51.100.7/32", "default"]


def test_after_the_first_preferred_failure_no_more_preferred_commands():
    """The plan here is [del, del, replace]. When the first del fails the tick must
    sweep and stop: the replace after it would install a route the tick reports
    as withdrawn."""
    class FirstDelFails(FakeIp):
        failed = False

        def apply(self, argv):
            if argv[1] == "del" and not self.failed:
                self.failed = True
                self.calls.append(list(argv))
                return False, "RTNETLINK answers: simulated failure"
            return super().apply(argv)

    c = cfg()
    ip = FirstDelFails(BASE + [{"dst": "default", "gateway": "10.200.0.2", "dev": "veth-vpn", "metric": 100},
                               {"dst": "default", "dev": "wg-exit", "metric": 100}])
    new, rc, _ = run_tick(c, {}, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB})
    assert rc == 1 and new["route"] is None
    assert not any(call[1] == "replace" and call[2] == "default" for call in ip.calls)
    assert ip.preferred() == []


def test_with_defaults_tolerates_bad_state_file():
    """with_defaults resets bad types from hand-edited state files."""
    c = cfg()

    # State with wrong types
    bad_state = {
        "desired_mode": 123,  # should be None or str
        "desired_mode_fetched_at": "not a float",  # should be float
        "desired_mode_fetch_fail": "not an int",  # should be int
        "last_route_change": [],  # should be float
        "last_check": True,  # bool is wrong type
        "effective_mode": "relay_backbone",  # this is OK
        "target": "vpn",  # this is OK
        "route": "vpn",  # this is OK
        "upstreams": {
            "vpn": {
                "healthy": "yes",  # should be bool
                "consecutive_pass": "5",  # should be int
                "last_probe": 123,  # should be str
                "last_change": "now"  # should be float
            },
            "backbone": {}  # will get defaults
        }
    }

    result = M.with_defaults(bad_state, c)

    # Scalars should be reset
    assert result["desired_mode"] is None
    assert result["desired_mode_fetched_at"] == 0.0
    assert result["desired_mode_fetch_fail"] == 0
    assert result["last_route_change"] == 0.0
    assert result["last_check"] == 0.0
    # Good scalars preserved
    assert result["effective_mode"] == "relay_backbone"
    # Per-upstream: bad types reset, good ones checked
    assert result["upstreams"]["vpn"]["healthy"] == M.new_health()["healthy"]
    assert result["upstreams"]["vpn"]["consecutive_pass"] == M.new_health()["consecutive_pass"]
    assert result["upstreams"]["vpn"]["last_probe"] == M.new_health()["last_probe"]
    assert result["upstreams"]["vpn"]["last_change"] == M.new_health()["last_change"]


# A timestamp the state file must not be trusted with: wrong type, negative, NaN,
# infinite, past 1e11 (year 5138), or an int too big for a float (it made the age
# arithmetic raise OverflowError).
BAD_TIMES = [pytest.param("bad", id="str"), pytest.param(True, id="bool"),
             pytest.param(None, id="none"), pytest.param(-1.0, id="negative"),
             pytest.param(float("nan"), id="nan"), pytest.param(float("inf"), id="inf"),
             pytest.param(float("-inf"), id="-inf"), pytest.param(1e11 + 1, id="past-the-limit"),
             pytest.param(10 ** 400, id="huge-int")]


@pytest.mark.parametrize("state", [
    pytest.param({"desired_mode": ["relay_vpn"], "desired_mode_fetched_at": "bad",
                  "desired_mode_fetch_fail": "x",
                  "upstreams": {"vpn": {"consecutive_pass": "5", "healthy": "yes"}}},
                 id="wrong-types"),
    pytest.param({"desired_mode": ["relay_vpn"], "desired_mode_fetched_at": 95.0}, id="list-mode"),
    pytest.param({"desired_mode": "banana", "desired_mode_fetched_at": 95.0}, id="unknown-mode"),
    pytest.param({"desired_mode_fetch_fail": True}, id="bool-fail-count"),
])
def test_tick_on_hand_edited_state_with_a_failing_fetch(state):
    """The fetch fails, so nothing overwrites a bad field: with_defaults has to make it
    safe, or this tick crashes or pins a mode nobody asked for."""
    c, ip = cfg(), FakeIp(BASE)
    new, rc, _ = run_tick(c, state, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB},
                          fetch_err="timeout")
    assert rc == 0 and new["desired_mode_fetch_fail"] == 1
    assert new["effective_mode"] == "relay_direct" and ip.preferred() == []


@pytest.mark.parametrize("fetched_at", BAD_TIMES)
def test_tick_never_pins_a_mode_whose_fetch_time_is_corrupt(fetched_at):
    c, ip = cfg(), FakeIp(BASE)
    state = {"desired_mode": "relay_vpn", "desired_mode_fetched_at": fetched_at}
    new, rc, _ = run_tick(c, state, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB},
                          fetch_err="timeout")
    assert rc == 0 and new["effective_mode"] == "relay_direct" and ip.preferred() == []


def test_tick_keeps_the_last_known_mode_within_grace_when_the_fetch_fails():
    c, ip = cfg(), FakeIp(BASE)
    probes = {"vpn": OK_VPN, "backbone": OK_BB}
    state, _, _ = run_tick(c, {}, 100.0, ip, probes, mode="relay_vpn")
    state, rc, _ = run_tick(c, state, 130.0, ip, probes, fetch_err="timeout")
    assert rc == 0 and state["effective_mode"] == "relay_vpn"
    assert [r["dev"] for r in ip.preferred()] == ["veth-vpn"]


def test_tick_keeps_the_last_known_mode_within_grace_when_the_fetch_raises():
    """A poll that RAISES is the same glitch as one that returns an error: within
    grace the last-known mode holds and its healthy route stays. The fetch guard
    must only count the failure, never drop the mode, or one malformed reply
    withdraws a working backbone route at once instead of after grace_s."""
    c, ip = cfg(), FakeIp(BASE)
    probes = {"vpn": OK_VPN, "backbone": OK_BB}

    def ok(url, timeout):
        return "relay_backbone", "wan2", None

    def boom(url, timeout):
        raise ValueError("Invalid IPv6 URL")

    state, rc = M.tick(c, {}, 100.0, ip=ip, probe=lambda up: probes[up["name"]],
                       fetch=ok, log=lambda line: None)
    assert rc == 0 and [r["dev"] for r in ip.preferred()] == ["wg-exit"]

    state, rc = M.tick(c, state, 130.0, ip=ip, probe=lambda up: probes[up["name"]],
                       fetch=boom, log=lambda line: None)
    assert rc == 0
    assert state["desired_mode"] == "relay_backbone"
    assert state["desired_mode_fetched_at"] == 100.0       # a failed poll does not refresh it
    assert state["desired_mode_fetch_fail"] == 1
    assert state["effective_mode"] == "relay_backbone"
    assert [r["dev"] for r in ip.preferred()] == ["wg-exit"]


@pytest.mark.parametrize("mode", ["banana", "", ["relay_vpn"], {"mode": "relay_vpn"}, 123],
                         ids=["unknown", "empty", "list", "dict", "int"])
def test_with_defaults_resets_a_mode_that_is_not_a_known_name(mode):
    """The fetch time is valid, so only the mode check can drop it."""
    s = M.with_defaults({"desired_mode": mode, "desired_mode_fetched_at": 95.0}, cfg())
    assert s["desired_mode"] is None


@pytest.mark.parametrize("fails", [True, "x", 1.5, None], ids=["bool", "str", "float", "none"])
def test_with_defaults_resets_a_fail_count_that_is_not_an_int(fails):
    s = M.with_defaults({"desired_mode_fetch_fail": fails}, cfg())
    assert s["desired_mode_fetch_fail"] == 0 and type(s["desired_mode_fetch_fail"]) is int


@pytest.mark.parametrize("fetched_at", BAD_TIMES)
def test_with_defaults_drops_the_mode_along_with_an_unusable_fetch_time(fetched_at):
    """tick() reads a fetch time of 0 as no age, so a mode kept here would never age out."""
    s = M.with_defaults({"desired_mode": "relay_vpn", "desired_mode_fetched_at": fetched_at}, cfg())
    assert s["desired_mode"] is None and s["desired_mode_fetched_at"] == 0.0


@pytest.mark.parametrize("extra", [{}, {"desired_mode_fetched_at": 0}, {"desired_mode_fetched_at": 0.0}],
                         ids=["missing", "int-zero", "float-zero"])
def test_with_defaults_drops_a_mode_that_has_no_fetch_time(extra):
    assert M.with_defaults({"desired_mode": "relay_vpn", **extra}, cfg())["desired_mode"] is None


@pytest.mark.parametrize("fetched_at", [1.7e9, 1700000000, 1e11], ids=["float", "int", "the-limit"])
def test_with_defaults_keeps_a_mode_with_a_usable_fetch_time(fetched_at):
    s = M.with_defaults({"desired_mode": "relay_vpn", "desired_mode_fetched_at": fetched_at}, cfg())
    assert s["desired_mode"] == "relay_vpn" and s["desired_mode_fetched_at"] == fetched_at


@pytest.mark.parametrize("bad", BAD_TIMES)
def test_with_defaults_resets_every_unusable_timestamp(bad):
    s = M.with_defaults({"desired_mode_fetched_at": bad, "last_route_change": bad,
                         "last_check": bad, "upstreams": {"vpn": {"last_change": bad}}}, cfg())
    assert (s["desired_mode_fetched_at"], s["last_route_change"], s["last_check"],
            s["upstreams"]["vpn"]["last_change"]) == (0.0, 0.0, 0.0, 0.0)


def test_with_defaults_keeps_usable_timestamps():
    s = M.with_defaults({"desired_mode_fetched_at": 1.7e9, "last_route_change": 1700000001,
                         "last_check": 1.7e9 + 2, "upstreams": {"vpn": {"last_change": 1.7e9 + 5}}},
                        cfg())
    assert (s["desired_mode_fetched_at"], s["last_route_change"], s["last_check"],
            s["upstreams"]["vpn"]["last_change"]) == (1.7e9, 1700000001, 1.7e9 + 2, 1.7e9 + 5)


def test_main_with_unwritable_state_path(tmp_path, capsys, monkeypatch):
    """main() handles unwritable state_path: logs ERROR but returns tick's rc."""
    # Create config
    cfg_path = tmp_path / "c.json"
    cfg_path.write_text(json.dumps({
        "table": "egress",
        "client": {"control_url": ""},
        "mode_upstreams": {},
        "upstreams": {},
        "exempt": {"via": "192.0.2.1", "dev": "eth0", "prefixes": []}
    }))

    # Make state_path unwritable: path under a file
    file_path = tmp_path / "state_file.txt"
    file_path.write_text("x")
    bad_state = file_path / "state.json"

    raw = json.loads(cfg_path.read_text())
    raw["state_path"] = str(bad_state)
    cfg_path.write_text(json.dumps(raw))

    # Monkeypatch IP and probes to avoid real network calls
    monkeypatch.setattr(M, "IpRoute", lambda: FakeIp(BASE))
    monkeypatch.setattr(M, "run_probe", lambda up: (True, "ok"))

    # Call main with unwritable state_path
    rc = M.main(["--config", str(cfg_path)])

    # Should return tick's rc (0), not error
    assert rc == 0
    # Should log the error
    stderr = capsys.readouterr().err
    assert "ERROR: cannot save state" in stderr


def test_main_unwritable_state_keeps_a_failing_ticks_rc(tmp_path, capsys, monkeypatch):
    """A failed state save must not turn a failing tick into success: the exit code is
    what fires the dead-man switch."""
    class Broken(FakeIp):
        def show_table(self, table):
            raise RuntimeError("no table")

    p = tmp_path / "c.json"
    blocker = tmp_path / "f"
    blocker.write_text("x")
    p.write_text(json.dumps({"table": "egress", "state_path": str(blocker / "s.json")}))
    monkeypatch.setattr(M, "IpRoute", lambda: Broken(BASE))
    assert M.main(["--config", str(p)]) == 1
    assert "ERROR: cannot save state" in capsys.readouterr().err


def test_effective_mode_within_grace_uses_desired():
    assert M.effective_mode_for("relay_backbone", 10.0, 60.0, "relay_direct") == "relay_backbone"


def test_effective_mode_past_grace_falls_back_to_default():
    assert M.effective_mode_for("relay_backbone", 61.0, 60.0, "relay_direct") == "relay_direct"


# --- dead-man switch, units, example config -------------------------------------------

def _load_deadman():
    """Load the dead-man module lazily so a broken script doesn't break test collection."""
    return _load("relay_egress_deadman", "relay-egress-deadman")


class _ThreadStderr(io.StringIO):
    """A sys.stderr that keeps what the test's own thread writes, and passes any other
    thread's writes on to the stream it replaced.

    capsys captures the process-wide sys.stderr, so a thread left over from an earlier
    test (a request handler, say) can print into a dead-man test mid-run. Filtering the
    captured lines by the dead-man's prefix would survive that, but it would also let
    an unprefixed line from the dead-man itself go unnoticed. The dead-man runs on the
    test's thread, so keeping only that thread's writes lets each test compare the
    dead-man's whole stderr exactly."""

    def __init__(self, passthrough):
        super().__init__()
        self._passthrough = passthrough
        self._owner = threading.get_ident()

    def write(self, s):
        if threading.get_ident() != self._owner:
            return self._passthrough.write(s)
        return super().write(s)

    def flush(self):
        if threading.get_ident() != self._owner:
            self._passthrough.flush()


@contextlib.contextmanager
def _deadman_stderr():
    """Swap in a _ThreadStderr for the body of the `with`; yields it."""
    saved = sys.stderr
    sys.stderr = own = _ThreadStderr(saved)
    try:
        yield own
    finally:
        sys.stderr = saved


def test_deadman_stderr_keeps_only_the_test_threads_writes(capsys):
    """Why the dead-man's exact stderr assertions cannot be broken by another thread."""
    def stray():
        print("stray line from another thread", file=sys.stderr, flush=True)

    with _deadman_stderr() as err:
        t = threading.Thread(target=stray)
        t.start()
        t.join()
        print("relay-egress-deadman: mine", file=sys.stderr, flush=True)

    assert err.getvalue() == "relay-egress-deadman: mine\n"
    # The other thread's line went on to capsys. `in`, not `==`: capsys also holds
    # whatever any other thread wrote meanwhile, the very noise this helper keeps out.
    assert "stray line from another thread\n" in capsys.readouterr().err


def test_deadman_deletes_every_preferred_default(tmp_path):
    D = _load_deadman()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "egress"}))
    calls, left = [], [2]

    def run(argv, **kw):
        calls.append(argv)
        if left[0]:
            left[0] -= 1
            return R()
        return R(rc=2, err="No such process")

    assert D.main(["--config", str(p)], runner=run) == 0
    assert calls == [["ip", "route", "del", "default", "metric", "100", "table", "egress"]] * 3


def test_deadman_honours_dry_run(tmp_path):
    D = _load_deadman()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "egress", "dry_run": True}))

    # Runner should never be called
    def run(argv, **kw):
        raise AssertionError("runner called during dry_run")

    assert D.main(["--config", str(p)], runner=run) == 0


def test_deadman_deletes_when_the_config_says_dry_run_false(tmp_path):
    """After the shadow run the live config carries an explicit "dry_run": false.
    The dead-man must read that as live and fail open, exactly as when the key is
    absent; only a real true skips it."""
    D = _load_deadman()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "egress", "preferred_metric": 100, "dry_run": False}))
    calls, left = [], [1]

    def run(argv, **kw):
        calls.append(argv)
        if left[0]:
            left[0] -= 1
            return R()
        return R(rc=2, err="RTNETLINK answers: No such process")

    assert D.main(["--config", str(p)], runner=run) == 0
    assert calls == [["ip", "route", "del", "default", "metric", "100", "table", "egress"]] * 2


@pytest.mark.parametrize("value", ["yes", "true", 1], ids=["yes", "string-true", "one"])
def test_deadman_fails_open_on_a_dry_run_that_is_not_a_real_true(tmp_path, value):
    """The actuator rejects such a config (exit 2) rather than reading it as a dry run,
    so the dead-man must not read it as one either: it deletes (fails open), not skips."""
    with pytest.raises(M.ConfigError, match="dry_run"):
        M.validate_config(raw_cfg(dry_run=value))
    D = _load_deadman()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "egress", "dry_run": value}))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return R(rc=2, err="RTNETLINK answers: No such process")

    assert D.main(["--config", str(p)], runner=run) == 0
    assert calls == [["ip", "route", "del", "default", "metric", "100", "table", "egress"]]


def test_deadman_falls_back_to_env_table(tmp_path, monkeypatch):
    D = _load_deadman()
    monkeypatch.setenv("EGRESS_TABLE", "egress2")
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return R(rc=2)

    assert D.main(["--config", str(tmp_path / "missing.json")], runner=run) == 0
    assert calls[0][-1] == "egress2"


def test_deadman_without_any_table_exits_1(tmp_path, monkeypatch):
    D = _load_deadman()
    monkeypatch.delenv("EGRESS_TABLE", raising=False)
    monkeypatch.setattr(D, "DEFAULT_STATE", str(tmp_path / "no-state.json"))   # not this box's
    assert D.main(["--config", str(tmp_path / "missing.json")], runner=lambda *a, **k: R()) == 1


def test_deadman_uses_config_metric(tmp_path):
    D = _load_deadman()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "egress", "preferred_metric": 50}))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return R(rc=2)

    assert D.main(["--config", str(p)], runner=run) == 0
    assert calls[0][5] == "50"  # metric argument


def test_deadman_bad_metric_defaults_to_100(tmp_path):
    D = _load_deadman()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "egress", "preferred_metric": "bad"}))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return R(rc=2)

    assert D.main(["--config", str(p)], runner=run) == 0
    assert calls[0][5] == "100"  # metric defaults to 100


def test_deadman_bool_metric_defaults_to_100(tmp_path):
    D = _load_deadman()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "egress", "preferred_metric": True}))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return R(rc=2)

    assert D.main(["--config", str(p)], runner=run) == 0
    assert calls[0][5] == "100"


@pytest.mark.parametrize("ok_before", [0, 2], ids=["first-call-hangs", "third-call-hangs"])
def test_deadman_timeout_stops_loop_says_so_and_exits_1(tmp_path, capsys, ok_before):
    """`ok_before` deletes work, then the next `ip route del` hangs. 0 is the likeliest
    real case (a wedged `ip` hangs at once, nothing removed); 2 shows the count is kept."""
    D = _load_deadman()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "egress"}))
    calls, left = [], [ok_before]

    def run(argv, **kw):
        calls.append(argv)
        if left[0]:
            left[0] -= 1
            return R()
        raise subprocess.TimeoutExpired("ip", 5)

    with _deadman_stderr() as err:
        assert D.main(["--config", str(p)], runner=run) == 1
    # The hung call is not retried.
    assert len(calls) == ok_before + 1
    out = capsys.readouterr().out
    # What was removed, then why it stopped. A bare "(fail open)" would hide the hang.
    assert out == (f"relay-egress-deadman: removed {ok_before} preferred default(s) from "
                   "table egress (fail open)\n"
                   "relay-egress-deadman: ip route del timed out after 5 s\n")
    assert err.getvalue() == ""


def test_deadman_timeout_passes_timeout_kwarg(tmp_path):
    D = _load_deadman()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "egress"}))
    timeout_values = []

    def run(argv, **kw):
        timeout_values.append(kw.get("timeout"))
        return R(rc=2)

    D.main(["--config", str(p)], runner=run)
    assert timeout_values[0] == 5


@pytest.mark.parametrize("metric", [0, -1, -100], ids=["zero", "minus-one", "minus-100"])
def test_deadman_metric_below_1_defaults_to_100_keeps_table(tmp_path, metric):
    """The actuator rejects a preferred_metric below 1, so none of its routes has such a
    metric: the dead-man falls back to 100 and keeps the table it read."""
    D = _load_deadman()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "mytable", "preferred_metric": metric}))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return R(rc=2)

    assert D.main(["--config", str(p)], runner=run) == 0
    assert calls[0][5] == "100"
    assert calls[0][7] == "mytable"


# A config that reads fine but names no usable table: null, "", a number, a list.
BAD_TABLES = [pytest.param(None, id="null"), pytest.param("", id="empty-string"),
              pytest.param(5, id="number"), pytest.param(["x"], id="list")]
NO_TABLE_MSG = ("relay-egress-deadman: no table (config has no usable table, EGRESS_TABLE "
                "is unset/empty, and the state file {} names none)")


@pytest.mark.parametrize("bad", BAD_TABLES)
def test_deadman_bad_table_falls_back_to_env(tmp_path, monkeypatch, bad):
    D = _load_deadman()
    monkeypatch.setenv("EGRESS_TABLE", "envtable")
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": bad}))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return R(rc=2, err="RTNETLINK answers: No such process")

    assert D.main(["--config", str(p)], runner=run) == 0
    assert calls == [["ip", "route", "del", "default", "metric", "100", "table", "envtable"]]


@pytest.mark.parametrize("env", [None, ""], ids=["env-unset", "env-empty"])
@pytest.mark.parametrize("bad", BAD_TABLES)
def test_deadman_bad_table_and_no_env_exits_1_with_the_exact_message(
        tmp_path, monkeypatch, capsys, bad, env):
    D = _load_deadman()
    if env is None:
        monkeypatch.delenv("EGRESS_TABLE", raising=False)
    else:
        monkeypatch.setenv("EGRESS_TABLE", env)
    no_state = str(tmp_path / "no-state.json")
    monkeypatch.setattr(D, "DEFAULT_STATE", no_state)
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": bad}))

    def run(argv, **kw):
        raise AssertionError(f"no usable table, so no ip command: {argv}")

    with _deadman_stderr() as err:
        assert D.main(["--config", str(p)], runner=run) == 1
    assert capsys.readouterr().out == ""
    assert err.getvalue() == NO_TABLE_MSG.format(no_state) + "\n"


DEL_EGRESS = ["ip", "route", "del", "default", "metric", "100", "table", "egress"]


def _nothing_left(calls):
    """A dead-man runner that records each call and finds nothing to delete."""
    def run(argv, **kw):
        calls.append(argv)
        return R(rc=2, err="RTNETLINK answers: No such process")
    return run


@pytest.mark.parametrize("config", [None, "{not json", "", "[]", '{"table": null}'],
                         ids=["missing", "truncated-json", "empty-file", "list", "null-table"])
def test_deadman_takes_the_table_from_the_state_file_when_the_config_names_none(
        tmp_path, monkeypatch, config):
    """The shipped unit sets no EGRESS_TABLE, so with a broken config the actuator's
    own state file is the dead-man's last way to the table."""
    D = _load_deadman()
    monkeypatch.delenv("EGRESS_TABLE", raising=False)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"table": "egress", "route": "backbone"}))
    monkeypatch.setattr(D, "DEFAULT_STATE", str(state))
    p = tmp_path / "c.json"
    if config is not None:
        p.write_text(config)
    calls: list = []
    assert D.main(["--config", str(p)], runner=_nothing_left(calls)) == 0
    assert calls == [DEL_EGRESS]


@pytest.mark.parametrize("env", [None, ""], ids=["env-unset", "env-empty"])
def test_deadman_takes_the_state_table_when_egress_table_is_unset_or_empty(
        tmp_path, monkeypatch, env):
    """Only a NON-EMPTY $EGRESS_TABLE outranks the state file. A drop-in that sets
    `Environment=EGRESS_TABLE=` (empty) must not cut the dead-man off from the table
    the actuator saved."""
    D = _load_deadman()
    if env is None:
        monkeypatch.delenv("EGRESS_TABLE", raising=False)
    else:
        monkeypatch.setenv("EGRESS_TABLE", env)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"table": "egress"}))
    monkeypatch.setattr(D, "DEFAULT_STATE", str(state))
    p = tmp_path / "c.json"
    p.write_text('{"table": "egress", "state_pa')   # a broken edit: unparseable
    calls: list = []
    assert D.main(["--config", str(p)], runner=_nothing_left(calls)) == 0
    assert calls == [DEL_EGRESS]


def test_deadman_reads_the_state_path_from_a_readable_config(tmp_path, monkeypatch):
    """A config that names no usable table may still say where the state file is."""
    D = _load_deadman()
    monkeypatch.delenv("EGRESS_TABLE", raising=False)
    default = tmp_path / "default-state.json"
    default.write_text(json.dumps({"table": "wrong"}))
    monkeypatch.setattr(D, "DEFAULT_STATE", str(default))
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"table": "egress"}))
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": None, "state_path": str(state)}))
    calls: list = []
    assert D.main(["--config", str(p)], runner=_nothing_left(calls)) == 0
    assert calls == [DEL_EGRESS]


@pytest.mark.parametrize("bad", [None, "", 5, ["state.json"]],
                         ids=["null", "empty-string", "number", "list"])
def test_deadman_falls_back_to_the_default_state_path(tmp_path, monkeypatch, bad):
    """The actuator rejects such a state_path, so its state file is at the default."""
    D = _load_deadman()
    monkeypatch.delenv("EGRESS_TABLE", raising=False)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"table": "egress"}))
    monkeypatch.setattr(D, "DEFAULT_STATE", str(state))
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": None, "state_path": bad}))
    calls: list = []
    assert D.main(["--config", str(p)], runner=_nothing_left(calls)) == 0
    assert calls == [DEL_EGRESS]


def test_deadman_default_state_path_is_the_actuators():
    """Drift here would quietly cut the dead-man's last way to the table."""
    assert _load_deadman().DEFAULT_STATE == M.validate_config({"table": "t"})["state_path"]


def test_deadman_takes_the_config_table_then_env_then_the_state_file(tmp_path, monkeypatch):
    D = _load_deadman()
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"table": "statetable"}))
    monkeypatch.setattr(D, "DEFAULT_STATE", str(state))
    monkeypatch.setenv("EGRESS_TABLE", "envtable")
    p = tmp_path / "c.json"
    tables = []
    for config in [{"table": "egress"}, {"table": None}]:
        p.write_text(json.dumps(config))
        calls: list = []
        assert D.main(["--config", str(p)], runner=_nothing_left(calls)) == 0
        tables.append(calls[0][-1])
    assert tables == ["egress", "envtable"]


def test_deadman_finds_the_table_the_actuator_saved_once_the_config_breaks(
        tmp_path, monkeypatch, capsys):
    """End to end: a good tick saves the table; an edit then breaks the config, the
    tick exits 2, and the dead-man (no EGRESS_TABLE) still finds the table."""
    D = _load_deadman()
    monkeypatch.delenv("EGRESS_TABLE", raising=False)
    state = tmp_path / "state.json"
    monkeypatch.setattr(D, "DEFAULT_STATE", str(state))   # where the actuator keeps it
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "egress", "state_path": str(state)}))
    monkeypatch.setattr(M, "IpRoute", lambda: FakeIp(BASE))
    assert M.main(["--config", str(p)]) == 0
    p.write_text('{"table": "egress", "state_pa')
    assert M.main(["--config", str(p)]) == 2
    assert "CONFIG ERROR" in capsys.readouterr().err
    calls: list = []
    assert D.main(["--config", str(p)], runner=_nothing_left(calls)) == 0
    assert calls == [DEL_EGRESS]


@pytest.mark.parametrize("text", [
    None, "{not json", "", "[]", "null", '"egress"', "{}", '{"table": null}', '{"table": ""}',
    '{"table": 5}', '{"table": ["egress"]}', "[" * DEEP,
], ids=["missing", "truncated-json", "empty-file", "list", "null", "string", "no-table",
        "null-table", "empty-table", "number-table", "list-table", "nested-too-deep"])
def test_deadman_with_no_env_and_no_usable_state_exits_1(tmp_path, monkeypatch, capsys, text):
    D = _load_deadman()
    monkeypatch.delenv("EGRESS_TABLE", raising=False)
    state = tmp_path / "state.json"
    if text is not None:
        state.write_text(text)
    monkeypatch.setattr(D, "DEFAULT_STATE", str(state))

    def run(argv, **kw):
        raise AssertionError(f"no usable table, so no ip command: {argv}")

    with _deadman_stderr() as err:
        assert D.main(["--config", str(tmp_path / "missing.json")], runner=run) == 1
    assert capsys.readouterr().out == ""
    assert err.getvalue() == NO_TABLE_MSG.format(state) + "\n"


@pytest.mark.parametrize("text", ["{not json", "", "[]", "null", '"egress"'],
                         ids=["truncated-json", "empty-file", "list", "null", "string"])
def test_deadman_unusable_config_falls_back_to_env_table(tmp_path, monkeypatch, text):
    """The dead-man exists for when the actuator's config is broken (the actuator exits 2
    on an unreadable or malformed file), so a config that is there but not a JSON object
    must still fail open through EGRESS_TABLE."""
    D = _load_deadman()
    monkeypatch.setenv("EGRESS_TABLE", "envtable")
    p = tmp_path / "c.json"
    p.write_text(text)
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return R(rc=2, err="RTNETLINK answers: No such process")

    assert D.main(["--config", str(p)], runner=run) == 0
    assert calls == [["ip", "route", "del", "default", "metric", "100", "table", "envtable"]]


def test_deadman_fib_table_not_exist_same_as_no_such_process(tmp_path, capsys):
    D = _load_deadman()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "egress"}))

    def run(argv, **kw):
        return R(rc=1, err="RTNETLINK answers: No such address\nFIB table does not exist")

    assert D.main(["--config", str(p)], runner=run) == 0
    out, err = capsys.readouterr()
    # Should treat "FIB table does not exist" same as "No such process"
    assert "removed 0 preferred default" in out


def test_deadman_stderr_not_no_such_process_exits_1_with_summary(tmp_path, capsys):
    D = _load_deadman()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "egress"}))

    def run(argv, **kw):
        return R(rc=1, err="RTNETLINK answers: Permission denied")

    assert D.main(["--config", str(p)], runner=run) == 1
    out, err = capsys.readouterr()
    # Must print summary line before exiting
    assert "removed 0 preferred default" in out
    assert "Permission denied" in out


def test_deadman_real_error_after_deletes_reports_the_true_count(tmp_path, capsys):
    """Two deletes work, then one fails for a reason other than "nothing left": the
    summary must say 2 were removed, not 0, before it says why it stopped."""
    D = _load_deadman()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "egress"}))
    calls, left = [], [2]

    def run(argv, **kw):
        calls.append(argv)
        if left[0]:
            left[0] -= 1
            return R()
        return R(rc=2, err="RTNETLINK answers: Operation not permitted\n")

    with _deadman_stderr() as err:
        assert D.main(["--config", str(p)], runner=run) == 1
    assert len(calls) == 3
    out = capsys.readouterr().out
    assert out == ("relay-egress-deadman: removed 2 preferred default(s) from table egress "
                   "(fail open)\n"
                   "relay-egress-deadman: ip route del failed: "
                   "RTNETLINK answers: Operation not permitted\n")
    assert err.getvalue() == ""


def test_example_config_validates():
    raw = json.loads((_ROOT / "config/relay-egress.example.json").read_text())
    c = M.validate_config(raw)
    assert c["client"]["default_mode"] == "relay_direct"
    assert set(c["mode_upstreams"]) == {"relay_vpn", "relay_backbone"}
    assert c["dry_run"] is True


def test_units_wire_the_deadman_and_the_paths():
    unit = (_DIR / "systemd/relay-egress-watchdog.service").read_text()
    assert "OnFailure=relay-egress-deadman.service" in unit
    assert "ExecStart=/usr/local/sbin/relay-egress-watchdog --config " in unit
    assert "Type=oneshot" in unit
    assert "TimeoutStartSec=25" in unit
    assert "RuntimeDirectoryPreserve=yes" in unit
    timer = (_DIR / "systemd/relay-egress-watchdog.timer").read_text()
    assert "OnUnitActiveSec=10" in timer
    dead = (_DIR / "systemd/relay-egress-deadman.service").read_text()
    assert "ExecStart=/usr/local/sbin/relay-egress-deadman --config " in dead
    assert "Type=oneshot" in dead
