import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import sbfd_ctl as M


def start_server(handler_factory):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


SBFD_PAYLOAD = {
    "timestamp": 1745889012.34,
    "sessions": {
        "wan1":      {"session_id": 1, "state": "UP",   "rtt_ms": 51.2, "loss_pct": 0.0},
        "wan2": {"session_id": 2, "state": "UP",   "rtt_ms": 49.9, "loss_pct": 0.0},
    }
}


@pytest.fixture
def ok_server():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a, **k): pass
        def do_GET(self):
            if self.path != "/state":
                self.send_error(404); return
            body = json.dumps(SBFD_PAYLOAD).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    httpd = start_server(H)
    yield httpd
    httpd.shutdown()


@pytest.fixture
def err_server():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a, **k): pass
        def do_GET(self):
            self.send_error(503, "down")
    httpd = start_server(H)
    yield httpd
    httpd.shutdown()


def test_fetch_remote_happy_path(ok_server):
    port = ok_server.server_address[1]
    snap = M.fetch_remote_sbfd_state(f"http://127.0.0.1:{port}/state",
                                     timeout_s=2.0,
                                     session_id_to_wan={1:"wan1", 2:"wan2"})
    assert snap.ok is True
    assert snap.per_wan["wan1"].state == "UP"
    assert snap.per_wan["wan2"].rtt_ms == 49.9


def test_fetch_remote_5xx_returns_error_snapshot(err_server):
    port = err_server.server_address[1]
    snap = M.fetch_remote_sbfd_state(f"http://127.0.0.1:{port}/state",
                                     timeout_s=2.0,
                                     session_id_to_wan={1:"wan1", 2:"wan2"})
    assert snap.ok is False
    assert "503" in snap.error or "Service" in snap.error or "down" in snap.error.lower()


def test_fetch_remote_connection_refused():
    snap = M.fetch_remote_sbfd_state("http://127.0.0.1:1/state",
                                     timeout_s=1.0,
                                     session_id_to_wan={1:"wan1", 2:"wan2"})
    assert snap.ok is False
    assert "refused" in snap.error.lower() or "connection" in snap.error.lower()


def test_fetch_remote_timeout():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a, **k): pass
        def do_GET(self):
            import time as _t
            _t.sleep(2.0)
            self.send_response(200); self.end_headers()
    httpd = start_server(H)
    try:
        port = httpd.server_address[1]
        snap = M.fetch_remote_sbfd_state(f"http://127.0.0.1:{port}/state",
                                         timeout_s=0.3,
                                         session_id_to_wan={1:"wan1", 2:"wan2"})
        assert snap.ok is False
        assert "timed out" in snap.error.lower() or "timeout" in snap.error.lower()
    finally:
        httpd.shutdown()


def test_merge_effective_down_dominates():
    L = M.StateSnapshot(ok=True, per_wan={"wan1": M.WanSample("UP", 50.0, 0.0),
                                          "wan2": M.WanSample("UP", 50.0, 0.0)})
    R = M.StateSnapshot(ok=True, per_wan={"wan1": M.WanSample("DOWN", None, 100.0),
                                          "wan2": M.WanSample("UP", 50.0, 0.0)})
    assert M.merge_effective(L, R) == {"wan1": "DOWN", "wan2": "UP"}


def test_merge_effective_remote_unavailable_uses_local():
    L = M.StateSnapshot(ok=True, per_wan={"wan1": M.WanSample("UP", 50.0, 0.0),
                                          "wan2": M.WanSample("DOWN", None, 100.0)})
    R = M.StateSnapshot(ok=False, per_wan={}, error="conn refused")
    assert M.merge_effective(L, R) == {"wan1": "UP", "wan2": "DOWN"}


def test_merge_effective_both_unavailable_returns_empty():
    L = M.StateSnapshot(ok=False, per_wan={}, error="missing")
    R = M.StateSnapshot(ok=False, per_wan={}, error="conn refused")
    assert M.merge_effective(L, R) == {}


def test_merge_effective_stale_local_down_does_not_suppress_fresh_remote_up():
    # CR #13: a stale local snapshot (sbfd daemon frozen) must not let its
    # last-seen DOWN dominate fresh remote data, or a recovered WAN stays
    # suppressed forever.
    L = M.StateSnapshot(ok=True, stale=True,
                        per_wan={"wan1": M.WanSample("UP", 50.0, 0.0),
                                 "wan2": M.WanSample("DOWN", None, 100.0)})
    R = M.StateSnapshot(ok=True, stale=False,
                        per_wan={"wan1": M.WanSample("UP", 50.0, 0.0),
                                 "wan2": M.WanSample("UP", 50.0, 0.0)})
    assert M.merge_effective(L, R) == {"wan1": "UP", "wan2": "UP"}


def test_merge_effective_both_stale_keeps_last_known():
    # When NEITHER side is fresh (total blindness), keep last-known states
    # rather than nuking everything to UNKNOWN -- doing the latter would route
    # into the "both DOWN safety fallback" path. With both stale, DOWN still
    # dominates the last-known data (conservative; no new fallback trigger).
    L = M.StateSnapshot(ok=True, stale=True,
                        per_wan={"wan1": M.WanSample("DOWN", None, 100.0)})
    R = M.StateSnapshot(ok=True, stale=True,
                        per_wan={"wan1": M.WanSample("UP", 50.0, 0.0)})
    assert M.merge_effective(L, R) == {"wan1": "DOWN"}


def test_merge_effective_stale_only_source_kept_when_nothing_fresher():
    # A stale local with no fresh counterpart is still our best info: keep it
    # rather than going blind.
    L = M.StateSnapshot(ok=True, stale=True,
                        per_wan={"wan1": M.WanSample("UP", 50.0, 0.0),
                                 "wan2": M.WanSample("DOWN", None, 100.0)})
    R = M.StateSnapshot(ok=False, per_wan={}, error="conn refused")
    assert M.merge_effective(L, R) == {"wan1": "UP", "wan2": "DOWN"}
