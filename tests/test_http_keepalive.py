"""Connection reuse on PathFuse's three HTTP listeners.

All three answer pollers that hit them on a fixed cadence -- the browser UI at
1-2 Hz, sbfd-ctl fetching the relay at 1 Hz across the management overlay. On
HTTP/1.0 every one of those polls pays a fresh TCP handshake and teardown,
which over a metered WAN link is most of the bytes and an extra RTT of
staleness. These tests pin the connection down.

They also pin the safety property keep-alive depends on: any request that is
answered WITHOUT its body being read must close the connection, or the unread
body is parsed as the next request on a reused socket.
"""
import http.client
import json
import threading
import time
from pathlib import Path

import pytest

import fec_control
import sbfd
import sbfd_ctl
import udpspeeder_fec


def probe_reuse(port, path, method="GET", body=None):
    """Issue two requests on one connection object.

    Returns (local_ports, first_response_version, first_will_close).
    A server that closes after each response forces http.client to reconnect,
    so the two local ports differ -- that is the observable signature of a
    handshake per poll.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    ports = []
    version = will_close = None
    try:
        for i in range(2):
            conn.request(method, path, body=body)
            ports.append(conn.sock.getsockname()[1])
            resp = conn.getresponse()
            if i == 0:
                version, will_close = resp.version, resp.will_close
            resp.read()
    finally:
        conn.close()
    return ports, version, will_close


# -- fixtures ----------------------------------------------------------------

@pytest.fixture
def sbfd_state_listener(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"timestamp": 1.0, "sessions": {}}))
    cfg = sbfd.DaemonConfig(state_file=str(state_file), state_listen="127.0.0.1:0")
    httpd = sbfd.start_state_listener(cfg)
    assert httpd is not None
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def ui_server(tmp_path: Path):
    cfg = sbfd_ctl.Config(
        wans={"wan1": sbfd_ctl.WanCfg("wan1", 1, "Cellular")},
        relay=sbfd_ctl.RelayCfg("http://x"),
        engarde=sbfd_ctl.EngardeCfg("198.51.100.10", 59402),
        nft=sbfd_ctl.NftCfg(),
        policy=sbfd_ctl.PolicyCfg(),
        ui_listen="127.0.0.1:0",
        sbfd_local_state=str(tmp_path / "sbfd-state.json"),
        runtime_state=str(tmp_path / "runtime.json"),
        persist_state=str(tmp_path / "persist.json"),
        published_state=str(tmp_path / "published.json"),
    )
    Path(cfg.published_state).write_text(json.dumps({"ts": 1.0, "mode": "full"}))
    stop = threading.Event()
    httpd = sbfd_ctl.start_ui_server(cfg, stop)
    yield httpd
    stop.set()
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def fec_server():
    state = udpspeeder_fec.FecState(mode=fec_control.MODE_ADAPTIVE)
    httpd = udpspeeder_fec.start_fec_http("127.0.0.1:0", state)
    assert httpd is not None
    yield httpd
    httpd.shutdown()
    httpd.server_close()


# -- connection reuse --------------------------------------------------------

def test_sbfd_state_listener_reuses_one_connection(sbfd_state_listener):
    port = sbfd_state_listener.server_address[1]

    ports, version, will_close = probe_reuse(port, "/state")

    assert version == 11, "must answer HTTP/1.1 for keep-alive to be possible"
    assert will_close is False, "server signalled close; poller pays a handshake"
    assert ports[0] == ports[1], f"reconnected between polls: {ports}"


def test_ui_server_reuses_one_connection(ui_server):
    port = ui_server.server_address[1]

    ports, version, will_close = probe_reuse(port, "/api/state")

    assert version == 11
    assert will_close is False
    assert ports[0] == ports[1], f"reconnected between polls: {ports}"


def test_fec_server_reuses_one_connection(fec_server):
    port = fec_server.server_address[1]

    ports, version, will_close = probe_reuse(port, "/fec")

    assert version == 11
    assert will_close is False
    assert ports[0] == ports[1], f"reconnected between polls: {ports}"


# -- idle-connection reaping -------------------------------------------------

@pytest.mark.parametrize("fixture_name", ["sbfd_state_listener", "ui_server", "fec_server"])
def test_servers_set_an_idle_timeout(fixture_name, request):
    """Keep-alive without a timeout leaks a thread per abandoned connection.

    ThreadingHTTPServer holds one thread for a connection's whole life, and
    BaseHTTPRequestHandler.timeout defaults to None -- an unbounded blocking
    read. handle_one_request catches TimeoutError and closes, so setting the
    timeout is what makes a dead client's thread recoverable.
    """
    httpd = request.getfixturevalue(fixture_name)

    assert httpd.RequestHandlerClass.timeout is not None


# -- Nagle / delayed-ACK -----------------------------------------------------

@pytest.mark.parametrize("fixture_name,path", [
    ("sbfd_state_listener", "/state"),
    ("ui_server", "/api/state"),
    ("fec_server", "/fec"),
])
def test_keepalive_responses_are_not_delayed_by_nagle(fixture_name, path, request):
    """Keep-alive must not trade a handshake for a 40ms stall per response.

    BaseHTTPRequestHandler writes headers and body as two separate small
    writes. socketserver leaves Nagle enabled by default, so the second write
    waits for an ACK while the client -- which does set TCP_NODELAY -- sits on
    its 40ms delayed-ACK timer. On HTTP/1.0 the close flushed it; on a
    persistent connection every response pays it, making keep-alive slower than
    what it replaced.
    """
    httpd = request.getfixturevalue(fixture_name)
    port = httpd.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    n = 20
    try:
        conn.request("GET", path)  # warm; excluded from the timing
        conn.getresponse().read()
        start = time.monotonic()
        for _ in range(n):
            conn.request("GET", path)
            conn.getresponse().read()
        elapsed = time.monotonic() - start
    finally:
        conn.close()

    per_request_ms = 1000 * elapsed / n
    assert per_request_ms < 20, (
        f"{per_request_ms:.1f}ms per loopback request -- looks like a Nagle stall")


# -- desync safety -----------------------------------------------------------

def test_ui_server_closes_connection_when_rejecting_an_unread_body(ui_server):
    """POST /api/runtime 413s on an oversized body without reading it.

    On a reused socket that unread body would be parsed as the next request.
    send_error emits `Connection: close`, which is what prevents it -- assert
    that, so a future switch to a hand-rolled error path cannot quietly
    reintroduce the desync.
    """
    port = ui_server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", "/api/runtime", body=json.dumps({"mode": "full",
                                                              "pad": "x" * 5000}))
        resp = conn.getresponse()
        resp.read()
    finally:
        conn.close()

    assert resp.status == 413
    assert resp.will_close is True, "unread body would desync the next request"


def test_fec_server_closes_connection_when_rejecting_an_unknown_post_path(fec_server):
    """POST to a non-/fec path returns 404 before reading the body."""
    port = fec_server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", "/nope", body=json.dumps({"mode": "off"}))
        resp = conn.getresponse()
        resp.read()
    finally:
        conn.close()

    assert resp.status == 404
    assert resp.will_close is True
