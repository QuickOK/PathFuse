"""Tests for the relay-side egress actuator (deploy/relay/egress/).

The deployed scripts have no .py extension and use hyphens, so they are loaded
by path. These tests pin the client<->relay egress vocabulary (drift there
silently pins the relay to its default mode) and the route invariant: at most
one preferred default, and only toward a healthy upstream the mode selects.
"""
import importlib.util
import json
import subprocess
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
    (lambda c: c["client"].update(control_url="ftp://example.com"), "http://"),
    (lambda c: c["client"].update(default_mode=["relay_vpn"]), "must be a string"),
    (lambda c: c["mode_upstreams"].update({123: "vpn"}), "key must be a string"),
    (lambda c: c["mode_upstreams"].update(relay_vpn=["backbone"]), "value must be a string"),
    (lambda c: c["exempt"].update(prefixes=["0.0.0.0/0"]), "prefix length 0"),
])
def test_validate_config_rejects(mutate, match):
    raw = raw_cfg()
    mutate(raw)
    with pytest.raises(M.ConfigError, match=match):
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


def test_exemptions_applied_before_preferred():
    """tick() applies exemption routes before preferred route via FakeIp.calls."""
    c, ip = cfg(), FakeIp(BASE)
    state, rc, _ = run_tick(c, {}, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB})

    # Find indices of exemption and preferred commands in ip.calls
    exempt_calls = [i for i, call in enumerate(ip.calls) if "10.0.0.0/8" in call or "198.51.100.7" in call]
    preferred_calls = [i for i, call in enumerate(ip.calls) if call[1:3] == ["route", "replace"] and call[2] == "default"]

    # All exemptions should come before all preferred
    if exempt_calls and preferred_calls:
        assert max(exempt_calls) < min(preferred_calls), "exemptions must be applied before preferred"


def test_preferred_failure_stops_further_commands():
    """When a preferred route command fails, tick stops and sweeps immediately."""
    c = cfg()
    # Seed two metric-100 defaults
    ip = FakeIp(BASE + [{"dst": "default", "gateway": "10.200.0.2", "dev": "veth-vpn", "metric": 100},
                        {"dst": "default", "dev": "wg-exit", "metric": 100}])
    # Fail on any "replace" command
    ip.fail = {"replace"}

    state, rc, lines = run_tick(c, {}, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB})

    # Should have rc=1 (preferred failed)
    assert rc == 1
    # All dels should have succeeded, but replace should fail
    # After failure, sweep should have deleted the preferred defaults
    replace_calls = [call for call in ip.calls if len(call) > 1 and call[1] == "replace"]
    assert len(replace_calls) >= 1, "should have tried to replace"
    # After failure and sweep, no preferred defaults should remain
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


def test_tick_with_bad_state_file():
    """tick() with a bad state file from with_defaults doesn't crash."""
    c, ip = cfg(), FakeIp(BASE)

    # Bad state
    bad_state = {
        "desired_mode": [],
        "desired_mode_fetched_at": "bad",
        "upstreams": {
            "vpn": {"healthy": "yes"},
            "backbone": {"last_change": []}
        }
    }

    state, rc, _ = run_tick(c, bad_state, 100.0, ip, {"vpn": OK_VPN, "backbone": OK_BB})

    # Should complete successfully despite bad input
    assert rc == 0
    # Should have reset bad values
    assert isinstance(state["desired_mode"], (str, type(None)))
    assert isinstance(state["desired_mode_fetched_at"], (int, float))


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


def test_effective_mode_within_grace_uses_desired():
    assert M.effective_mode_for("relay_backbone", 10.0, 60.0, "relay_direct") == "relay_backbone"


def test_effective_mode_past_grace_falls_back_to_default():
    assert M.effective_mode_for("relay_backbone", 61.0, 60.0, "relay_direct") == "relay_direct"


# --- dead-man switch, units, example config -------------------------------------------

def _load_deadman():
    """Load the dead-man module lazily so a broken script doesn't break test collection."""
    return _load("relay_egress_deadman", "relay-egress-deadman")


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


def test_deadman_timeout_stops_loop_and_exits_1(tmp_path, capsys):
    D = _load_deadman()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "egress"}))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        raise subprocess.TimeoutExpired("ip", 5)

    assert D.main(["--config", str(p)], runner=run) == 1
    # Only one attempt before timeout stops it
    assert len(calls) == 1
    out, err = capsys.readouterr()
    # Should print summary before exiting 1
    assert "removed 0 preferred default" in out


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


def test_deadman_metric_zero_defaults_to_100_keeps_table(tmp_path):
    D = _load_deadman()
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": "mytable", "preferred_metric": 0}))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return R(rc=2)

    assert D.main(["--config", str(p)], runner=run) == 0
    # metric 0 should be rejected, defaults to 100, but table should be kept
    assert calls[0][5] == "100"
    assert calls[0][7] == "mytable"


def test_deadman_bad_table_falls_back_to_env(tmp_path, monkeypatch):
    D = _load_deadman()
    monkeypatch.setenv("EGRESS_TABLE", "envtable")
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": None}))  # Bad table
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return R(rc=2)

    assert D.main(["--config", str(p)], runner=run) == 0
    # Should use env table
    assert calls[0][7] == "envtable"


def test_deadman_bad_table_unset_env_exits_1_with_message(tmp_path, monkeypatch, capsys):
    D = _load_deadman()
    monkeypatch.delenv("EGRESS_TABLE", raising=False)
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"table": 123}))  # Bad table

    assert D.main(["--config", str(p)], runner=lambda *a, **k: R()) == 1
    out, err = capsys.readouterr()
    assert "config has no usable table and EGRESS_TABLE is unset/empty" in err


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
