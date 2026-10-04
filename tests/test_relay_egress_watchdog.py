"""Tests for the relay-side egress actuator (deploy/relay/egress/).

The deployed scripts have no .py extension and use hyphens, so they are loaded
by path. These tests pin the client<->relay egress vocabulary (drift there
silently pins the relay to its default mode) and the route invariant: at most
one preferred default, and only toward a healthy upstream the mode selects.
"""
import importlib.util
import json
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
    lines = []

    def fetch(url, timeout):
        return (None, None, fetch_err) if fetch_err else (mode, "wan2", None)

    new, rc = M.tick(c, state, now, ip=ip, probe=lambda up: probes[up["name"]],
                     fetch=fetch, log=lines.append)
    return new, rc, lines


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
])
def test_validate_config_rejects(mutate, match):
    raw = raw_cfg()
    mutate(raw)
    with pytest.raises(M.ConfigError, match=match):
        M.validate_config(raw)


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
    assert M.run_probe(cfg()["upstreams"]["vpn"], _runner()) == (True, "vpn=on")


def test_run_probe_link_down_skips_curl():
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return R(out=json.dumps([{"flags": ["BROADCAST"]}]))

    ok, detail = M.run_probe(cfg()["upstreams"]["vpn"], run)
    assert not ok and detail == "link veth-vpn down" and len(calls) == 1


def test_run_probe_curl_failure_and_unexpected_body():
    up = cfg()["upstreams"]["vpn"]
    ok, detail = M.run_probe(up, _runner(rc=28))
    assert not ok and detail.startswith("curl rc=28")
    assert M.run_probe(up, _runner(body="vpn=off\n")) == (False, "unexpected body")


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
    state = {}
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


def test_exemption_failure_is_logged_but_keeps_the_preferred_route():
    c, ip = cfg(), FakeIp(BASE, fail={"10.0.0.0/8"})
    _new, rc, lines = run_tick(c, {}, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB})
    assert rc == 0 and ip.preferred()[0]["dev"] == "wg-exit"
    assert any("ERROR: ip route replace 10.0.0.0/8" in line for line in lines)


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


def test_main_returns_2_on_bad_config(tmp_path, capsys):
    p = tmp_path / "c.json"
    p.write_text("{}")
    assert M.main(["--config", str(p)]) == 2
    assert "CONFIG ERROR" in capsys.readouterr().err


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
    assert M.fetch_desired_mode(f"http://127.0.0.1:{port}/x", 2.0) == (mode, "wan2", None)


def test_fetch_accepts_alias_and_normalizes():
    srv = _serve_once({"mode": "relay_wan", "master_wan": "wan2", "ts": 1.0})
    port = srv.server_address[1]
    mode, _master, err = M.fetch_desired_mode(f"http://127.0.0.1:{port}/x", 2.0)
    assert mode == "relay_direct" and err is None


def test_fetch_rejects_unknown_mode():
    srv = _serve_once({"mode": "banana"})
    port = srv.server_address[1]
    mode, _master, err = M.fetch_desired_mode(f"http://127.0.0.1:{port}/x", 2.0)
    assert mode is None and "invalid mode" in err


def test_effective_mode_within_grace_uses_desired():
    assert M.effective_mode_for("relay_backbone", 10.0, 60.0, "relay_direct") == "relay_backbone"


def test_effective_mode_past_grace_falls_back_to_default():
    assert M.effective_mode_for("relay_backbone", 61.0, 60.0, "relay_direct") == "relay_direct"
