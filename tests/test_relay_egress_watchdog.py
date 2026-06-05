"""Tests for the reference relay-side egress actuator.

The deployed script has no .py extension and uses hyphens, so it is loaded by
path via importlib rather than a plain `import`. These tests pin the client<->
relay egress vocabulary contract that, when it drifts, silently pins the relay to
the upstream-VPN egress (so relay_direct never takes effect). See
deploy/relay/egress/README.md.
"""
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy/relay/egress/relay-egress-watchdog"


def _load():
    # The deployed file has no .py extension, so an explicit source loader is needed.
    loader = SourceFileLoader("relay_egress_watchdog", str(_SCRIPT))
    spec = importlib.util.spec_from_loader("relay_egress_watchdog", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


M = _load()


# --- vocabulary: canonical names accepted, aliases normalized ---

def test_canonical_modes_passthrough():
    assert M.normalize_mode("relay_vpn") == "relay_vpn"
    assert M.normalize_mode("relay_direct") == "relay_direct"
    assert M.normalize_mode("local_direct") == "local_direct"


def test_alias_names_normalized_to_canonical():
    assert M.normalize_mode("upstream_vpn") == "relay_vpn"
    assert M.normalize_mode("relay_wan") == "relay_direct"
    assert M.normalize_mode("local") == "local_direct"


def test_unknown_and_none_passthrough():
    # Unknown/None must pass through unchanged so the VALID check still rejects it.
    assert M.normalize_mode("banana") == "banana"
    assert M.normalize_mode(None) is None


def test_normalized_modes_are_valid():
    for m in ("relay_vpn", "relay_direct", "local_direct", "upstream_vpn", "relay_wan", "local"):
        assert M.normalize_mode(m) in M.VALID_DESIRED_MODES


# --- the route decision these names drive ---

def test_only_relay_vpn_keeps_the_upstream_route():
    assert "relay_vpn" in M.UPSTREAM_MODES
    assert "relay_direct" not in M.UPSTREAM_MODES
    assert "local_direct" not in M.UPSTREAM_MODES


def test_effective_mode_within_grace_uses_desired():
    assert M.effective_mode_for("relay_direct", 10.0, 60.0, "relay_vpn") == "relay_direct"


def test_effective_mode_past_grace_falls_back_to_default():
    assert M.effective_mode_for("relay_direct", 61.0, 60.0, "relay_vpn") == "relay_vpn"


# --- end-to-end: the real fetch path against a stub client endpoint ---

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


def test_fetch_accepts_relay_direct():
    srv = _serve_once({"mode": "relay_direct", "master_wan": "wan2", "ts": 1.0})
    port = srv.server_address[1]
    assert M.fetch_desired_mode(f"http://127.0.0.1:{port}/x", 2.0) == ("relay_direct", "wan2", None)


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


# --- the state-machine integration that the vocabulary feeds ---

def test_advance_withdraws_route_when_mode_not_upstream():
    calls = {"add": 0, "remove": 0}
    state = dict(M._DEFAULT_STATE, healthy=True, first_pass_seen=True, consecutive_pass=5)
    new = M.advance(
        state, ok=True, probe_value="200", now=100.0,
        fail_threshold=3, pass_threshold=2,
        add_route=lambda: calls.__setitem__("add", calls["add"] + 1),
        remove_route=lambda: calls.__setitem__("remove", calls["remove"] + 1),
        desired_mode="relay_direct", desired_age_s=0.0, grace_s=60.0, default_mode="relay_vpn",
    )
    assert new["healthy"] is False and calls["remove"] == 1


def test_advance_keeps_route_when_relay_vpn_and_healthy():
    calls = {"add": 0, "remove": 0}
    state = dict(M._DEFAULT_STATE, healthy=False, first_pass_seen=False)
    new = M.advance(
        state, ok=True, probe_value="200", now=100.0,
        fail_threshold=3, pass_threshold=2,
        add_route=lambda: calls.__setitem__("add", calls["add"] + 1),
        remove_route=lambda: calls.__setitem__("remove", calls["remove"] + 1),
        desired_mode="relay_vpn", desired_age_s=0.0, grace_s=60.0, default_mode="relay_vpn",
    )
    assert new["healthy"] is True and calls["add"] == 1
