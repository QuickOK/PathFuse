import json
import os
import time
from pathlib import Path

import pytest
import sbfd_ctl as M


SBFD_FAKE = {
    "timestamp": 1745889012.34,
    "sessions": {
        "wan1_tmo":      {"session_id": 1, "state": "UP",   "rtt_ms": 51.2, "loss_pct": 0.0},
        "wan2_starlink": {"session_id": 2, "state": "DOWN", "rtt_ms": None, "loss_pct": 100.0},
    }
}


def write(path: Path, obj):
    path.write_text(json.dumps(obj))


def test_read_state_happy_path(tmp_path: Path):
    p = tmp_path / "state.json"
    write(p, SBFD_FAKE)
    snap = M.read_local_sbfd_state(str(p), session_id_to_wan={1:"wan1", 2:"wan2"})
    assert snap.ok is True
    assert snap.per_wan["wan1"].state == "UP"
    assert snap.per_wan["wan2"].state == "DOWN"
    assert snap.per_wan["wan1"].rtt_ms == 51.2
    assert snap.per_wan["wan2"].loss_pct == 100.0
    assert snap.stale_s < 5


def test_read_state_missing_file(tmp_path: Path):
    snap = M.read_local_sbfd_state(str(tmp_path / "nope.json"),
                                   session_id_to_wan={1:"wan1", 2:"wan2"})
    assert snap.ok is False
    assert "missing" in snap.error.lower() or "no such" in snap.error.lower()


def test_read_state_malformed_json(tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text("{ not json")
    snap = M.read_local_sbfd_state(str(p), session_id_to_wan={1:"wan1", 2:"wan2"})
    assert snap.ok is False


def test_read_state_stale_marked_but_returned(tmp_path: Path):
    p = tmp_path / "state.json"
    write(p, SBFD_FAKE)
    old = time.time() - 12
    os.utime(p, (old, old))
    snap = M.read_local_sbfd_state(str(p), session_id_to_wan={1:"wan1", 2:"wan2"},
                                   stale_threshold_s=10.0)
    assert snap.ok is True
    assert snap.stale is True
    assert snap.stale_s >= 10


def test_read_state_no_matching_session_id_returns_unknown(tmp_path: Path):
    fake = {
        "timestamp": time.time(),
        "sessions": {"weird": {"session_id": 99, "state": "UP", "rtt_ms": 10.0, "loss_pct": 0.0}},
    }
    p = tmp_path / "state.json"
    write(p, fake)
    snap = M.read_local_sbfd_state(str(p), session_id_to_wan={1:"wan1", 2:"wan2"})
    assert snap.ok is True
    assert snap.per_wan["wan1"].state == "UNKNOWN"
    assert snap.per_wan["wan2"].state == "UNKNOWN"
