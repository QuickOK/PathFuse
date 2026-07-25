"""Tests for sbfd.py's optional HTTP /state listener.

The listener publishes liveness state to sbfd-ctl across the management
overlay, which in a real deployment rides the metered WAN links -- so what
goes on the wire is a cost, not just a detail.
"""
import http.client
import json
import threading

import pytest

import sbfd as M


PRETTY_STATE = {
    "timestamp": 1753443512.9,
    "sessions": {
        "wan1": {"session_id": 1, "state": "UP", "state_since": 1753400000.1,
                 "uptime_s": 43512.8, "tx_seq": 87024, "last_rx_seq": 87023,
                 "last_rx_age_s": 0.48, "consecutive_miss": 0,
                 "consecutive_hit": 87023, "rtt_ms": 48.31, "loss_pct": 0.42,
                 "peer": "198.51.100.10:3785", "iface": "enp1s0"},
        "wan2": {"session_id": 2, "state": "UP", "state_since": 1753400001.2,
                 "uptime_s": 43511.7, "tx_seq": 87021, "last_rx_seq": 87020,
                 "last_rx_age_s": 0.51, "consecutive_miss": 0,
                 "consecutive_hit": 87020, "rtt_ms": 51.02, "loss_pct": 0.0,
                 "peer": "198.51.100.10:3786", "iface": "wwan0"},
    },
}


@pytest.fixture
def listener(tmp_path):
    """A bound /state listener plus the path of the state file it serves."""
    state_file = tmp_path / "state.json"
    cfg = M.DaemonConfig(state_file=str(state_file), state_listen="127.0.0.1:0")
    httpd = M.start_state_listener(cfg)
    assert httpd is not None, "listener failed to bind"
    yield httpd, state_file
    httpd.shutdown()
    httpd.server_close()


def _get(httpd, path="/state", conn=None):
    """GET path, returning (status, body_bytes, connection)."""
    own = conn is None
    if own:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1],
                                          timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    status = resp.status
    if own:
        conn.close()
        return status, body, None
    return status, body, conn


def test_state_endpoint_serves_compact_json_not_the_pretty_on_disk_file(listener):
    """The file stays human-readable; the wire payload does not pay for it.

    write_state_file uses indent=2 so operators can read /run/sbfd/state.json
    directly. Serving those bytes verbatim ships ~30% whitespace across a
    metered link once per second.
    """
    httpd, state_file = listener
    state_file.write_text(json.dumps(PRETTY_STATE, indent=2))

    status, body, _ = _get(httpd)

    assert status == 200
    assert json.loads(body) == PRETTY_STATE, "payload must be semantically identical"
    assert b"\n" not in body, f"newlines on the wire: {body[:80]!r}"
    assert b", " not in body, f"separator padding on the wire: {body[:80]!r}"
    assert len(body) < len(state_file.read_bytes()), "wire payload should be smaller"


def test_state_endpoint_serves_unparseable_file_verbatim(listener):
    """Compaction must not turn a malformed state file into a dead response.

    Without the fallback, json.loads raises inside do_GET and the client gets a
    torn connection instead of the bytes plus a chance to report a parse error.
    """
    httpd, state_file = listener
    state_file.write_bytes(b"{ this is not json")

    status, body, _ = _get(httpd)

    assert status == 200
    assert body == b"{ this is not json"


def test_state_endpoint_503s_before_the_first_state_write(listener):
    httpd, _ = listener

    status, _, _ = _get(httpd)

    assert status == 503
