"""sbfd-ctl's control-plane connection to the relay.

The controller loop GETs the relay's /state and /fec once per second across the
management overlay, which in a real deployment rides the metered WAN links. A
fresh TCP connection per poll costs more bytes than the payloads and an extra
RTT of staleness in a failover input, so the connection is pooled.

Pooling brings its own failure mode: a socket the peer has already closed --
reaped by an idle timeout, or dropped when the relay daemon restarted -- still
looks usable from this side. These tests cover the reuse and that recovery,
against real servers rather than a mocked transport.
"""
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import sbfd_ctl as M


SBFD_BODY = {
    "timestamp": 1753443512.9,
    "sessions": {"wan1": {"session_id": 1, "state": "UP", "rtt_ms": 48.3,
                          "loss_pct": 0.4}},
}
FEC_BODY = {"enabled": True, "mode": "min_adaptive", "ratio": "8:2", "level": 1}
SIDS = {1: "wan1"}


class CountingServer(ThreadingHTTPServer):
    """Counts accepted TCP connections, so reuse is observable."""

    daemon_threads = True

    def __init__(self, *a, **k):
        self.conn_count = 0
        super().__init__(*a, **k)

    def get_request(self):
        # Called only from the single-threaded accept loop.
        self.conn_count += 1
        return super().get_request()


def make_handler(close_every_response=False, hang_s=0.0):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 10

        def log_message(self, *a, **k):
            pass

        def _respond(self, obj):
            if hang_s:
                time.sleep(hang_s)
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if close_every_response:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/state":
                self._respond(SBFD_BODY)
            elif self.path == "/fec":
                self._respond(FEC_BODY)
            else:
                self.send_error(404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            self.rfile.read(length)
            self._respond({"ok": True, "mode": "min_adaptive"})

    return H


@pytest.fixture(autouse=True)
def clean_pool():
    """Each test starts with an empty pool and leaves none behind.

    The pool is process-global, and tests bind fresh ephemeral ports, so a
    connection left pooled from a previous test could otherwise be handed to a
    server that no longer exists.
    """
    M.close_relay_conns()
    yield
    M.close_relay_conns()


@pytest.fixture
def server():
    def _start(**kw):
        httpd = CountingServer(("127.0.0.1", 0), make_handler(**kw))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, httpd.server_address[1]
    started = []

    def start(**kw):
        httpd, port = _start(**kw)
        started.append(httpd)
        return httpd, port

    yield start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


# -- reuse -------------------------------------------------------------------

def test_repeated_state_fetches_reuse_one_tcp_connection(server):
    httpd, port = server()
    url = f"http://127.0.0.1:{port}/state"

    for _ in range(3):
        snap = M.fetch_remote_sbfd_state(url, 2.0, SIDS)
        assert snap.ok is True, snap.error

    assert httpd.conn_count == 1, (
        f"{httpd.conn_count} handshakes for 3 polls")


def test_state_and_fec_polls_to_one_relay_share_a_connection(server):
    """The loop hits /state and /fec on the same host each tick."""
    httpd, port = server()

    snap = M.fetch_remote_sbfd_state(f"http://127.0.0.1:{port}/state", 2.0, SIDS)
    fec = M.fetch_relay_fec(f"http://127.0.0.1:{port}/fec", 2.0)
    posted = M.post_relay_fec(f"http://127.0.0.1:{port}/fec",
                              "min_adaptive", "8:2", "20:1", 2.0)

    assert snap.ok is True and fec["ok"] is True and posted is True
    assert httpd.conn_count == 1, (
        f"{httpd.conn_count} handshakes for 3 same-host requests")


# -- recovery ----------------------------------------------------------------

def test_fetch_recovers_when_the_pooled_socket_was_closed_by_the_peer(server):
    """A relay restart or idle reap leaves a dead socket that still looks live.

    Closing the pooled socket underneath reproduces exactly that: the write
    goes to a closed fd, so the request must be retried on a fresh connection
    instead of failing the poll.
    """
    httpd, port = server()
    url = f"http://127.0.0.1:{port}/state"
    assert M.fetch_remote_sbfd_state(url, 2.0, SIDS).ok is True

    pooled = list(M._relay_conns.values())
    assert len(pooled) == 1 and pooled[0].sock is not None
    pooled[0].sock.close()  # dead fd, but conn.sock stays non-None

    snap = M.fetch_remote_sbfd_state(url, 2.0, SIDS)

    assert snap.ok is True, f"did not recover: {snap.error}"
    assert httpd.conn_count == 2


def test_fetch_succeeds_when_the_relay_declines_keepalive(server):
    """An older relay, or one behind a proxy, may close after every response."""
    httpd, port = server(close_every_response=True)
    url = f"http://127.0.0.1:{port}/state"

    for _ in range(3):
        assert M.fetch_remote_sbfd_state(url, 2.0, SIDS).ok is True

    assert httpd.conn_count == 3


def test_polling_an_http10_relay_still_works_and_pools_nothing():
    """The rolling-upgrade path: client updated, relay not yet.

    An un-upgraded relay answers HTTP/1.0, so there is no keep-alive to reuse.
    Every poll must still succeed at the old per-request cost, and nothing may
    be left in the pool -- a pooled connection the peer already closed would
    burn the next poll's first attempt.
    """
    class Old(BaseHTTPRequestHandler):  # stdlib default protocol is HTTP/1.0
        def log_message(self, *a, **k):
            pass

        def do_GET(self):
            body = json.dumps(SBFD_BODY).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = CountingServer(("127.0.0.1", 0), Old)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/state"
        for _ in range(3):
            assert M.fetch_remote_sbfd_state(url, 2.0, SIDS).ok is True
        assert httpd.conn_count == 3, "HTTP/1.0 cannot be reused; expect one each"
        assert not M._relay_conns, "must not pool a connection the peer closed"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_down_relay_is_not_retried_into_a_doubled_timeout(server):
    """Retry is for a stale pooled socket only, never for a fresh failure.

    The controller ticks every 0.5s with a 2s fetch timeout. Retrying a
    connection that was new -- i.e. the relay is simply unreachable -- would
    silently double that budget and stall the loop.
    """
    httpd, port = server(hang_s=2.0)
    url = f"http://127.0.0.1:{port}/state"

    t0 = time.monotonic()
    snap = M.fetch_remote_sbfd_state(url, 0.3, SIDS)
    elapsed = time.monotonic() - t0

    assert snap.ok is False
    assert "timed out" in snap.error.lower() or "timeout" in snap.error.lower()
    assert elapsed < 0.55, f"took {elapsed:.2f}s -- looks like a second attempt"


def test_close_relay_conns_empties_the_pool(server):
    _httpd, port = server()
    M.fetch_remote_sbfd_state(f"http://127.0.0.1:{port}/state", 2.0, SIDS)
    assert M._relay_conns

    M.close_relay_conns()

    assert not M._relay_conns
