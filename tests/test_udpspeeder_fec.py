import json
import os
import tempfile
import urllib.request, urllib.error

import pytest
import udpspeeder_fec as U


def _state(sessions):
    fd, p = tempfile.mkstemp(suffix=".json")
    os.write(fd, json.dumps({"sessions": sessions}).encode())
    os.close(fd)
    return p


def test_worst_loss_among_up_sessions():
    p = _state({"a": {"session_id": 1, "state": "UP", "loss_pct": 1.0},
                "b": {"session_id": 2, "state": "UP", "loss_pct": 4.0}})
    assert U.read_worst_loss(p) == (4.0, 2)


def test_down_sessions_ignored():
    p = _state({"a": {"session_id": 1, "state": "UP", "loss_pct": 1.0},
                "b": {"session_id": 2, "state": "DOWN", "loss_pct": 90.0}})
    assert U.read_worst_loss(p) == (1.0, 1)


def test_missing_file_returns_none():
    assert U.read_worst_loss("/nonexistent/state.json") == (None, 0)


def test_no_up_sessions_returns_zero_loss():
    p = _state({"a": {"session_id": 1, "state": "DOWN", "loss_pct": 9.0}})
    assert U.read_worst_loss(p) == (0.0, 0)


# ---------------------------------------------------------------------------
# Task 4.2 tests
# ---------------------------------------------------------------------------
import fec_control


def test_run_once_writes_startup_ratio(tmp_path):
    import os as _os
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"sessions": {"a": {"session_id": 1, "state": "UP", "loss_pct": 3.0}}}))
    fifo = tmp_path / "srv.fifo"
    _os.mkfifo(str(fifo))
    rfd = _os.open(str(fifo), _os.O_RDONLY | _os.O_NONBLOCK)
    cfg = {"fifo": str(fifo), "sbfd_state": str(state), "poll_interval_s": 0.01,
           "loss_table": fec_control.DEFAULT_LOSS_TABLE, "ramp_up_ticks": 1, "ramp_down_hold_s": 0}
    try:
        rt = fec_control.FecRuntime(0, 0, 0.0)
        rt, ratio = U.run_once(cfg, rt, current_ratio=None)
        assert ratio == "8:4"   # 3.0% loss -> level 2 -> 8:4
        assert _os.read(rfd, 64).decode() == "fec 8:4\n"
    finally:
        _os.close(rfd)


def test_run_once_disabled_forces_off_tier(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"sessions": {"a": {"session_id": 1, "state": "UP", "loss_pct": 9.0}}}))
    fifo = tmp_path / "srv.fifo"
    os.mkfifo(str(fifo))
    rfd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    cfg = {"fifo": str(fifo), "sbfd_state": str(state), "poll_interval_s": 0.01,
           "loss_table": fec_control.DEFAULT_LOSS_TABLE, "ramp_up_ticks": 1, "ramp_down_hold_s": 0}
    try:
        rt = fec_control.FecRuntime(4, 0, 0.0)  # pretend a high ratio is active
        rt, ratio = U.run_once(cfg, rt, current_ratio="8:8", enabled=False)
        assert ratio == "8:0"
        assert os.read(rfd, 64).decode() == "fec 8:0\n"
    finally:
        os.close(rfd)


def test_run_once_fixed_mode_writes_fixed_ratio(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"sessions": {"a": {"session_id": 1, "state": "UP", "loss_pct": 0.0}}}))
    fifo = tmp_path / "srv.fifo"
    os.mkfifo(str(fifo))
    rfd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    cfg = {"fifo": str(fifo), "sbfd_state": str(state), "poll_interval_s": 0.01,
           "loss_table": fec_control.DEFAULT_LOSS_TABLE, "ramp_up_ticks": 1, "ramp_down_hold_s": 0}
    try:
        rt = fec_control.FecRuntime(0, 0, 0.0)
        rt, ratio = U.run_once(cfg, rt, current_ratio=None,
                               mode=fec_control.MODE_FIXED, fixed_ratio="20:1")
        assert ratio == "20:1"
        assert os.read(rfd, 64).decode() == "fec 20:1\n"
    finally:
        os.close(rfd)


def test_run_once_min_adaptive_lifts_idle_to_floor(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"sessions": {"a": {"session_id": 1, "state": "UP", "loss_pct": 0.0}}}))
    fifo = tmp_path / "srv.fifo"
    os.mkfifo(str(fifo))
    rfd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    cfg = {"fifo": str(fifo), "sbfd_state": str(state), "poll_interval_s": 0.01,
           "loss_table": fec_control.DEFAULT_LOSS_TABLE, "ramp_up_ticks": 1,
           "ramp_down_hold_s": 0, "floor_ratio": "20:1"}
    try:
        rt = fec_control.FecRuntime(0, 0, 0.0)
        rt, ratio = U.run_once(cfg, rt, current_ratio=None,
                               mode=fec_control.MODE_MIN_ADAPTIVE)
        assert ratio == "20:1"     # idle 8:0 lifted to the floor
        assert os.read(rfd, 64).decode() == "fec 20:1\n"
    finally:
        os.close(rfd)


def test_run_once_enabled_default_still_adapts(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"sessions": {"a": {"session_id": 1, "state": "UP", "loss_pct": 3.0}}}))
    fifo = tmp_path / "srv.fifo"
    os.mkfifo(str(fifo))
    rfd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    cfg = {"fifo": str(fifo), "sbfd_state": str(state), "poll_interval_s": 0.01,
           "loss_table": fec_control.DEFAULT_LOSS_TABLE, "ramp_up_ticks": 1, "ramp_down_hold_s": 0}
    try:
        rt = fec_control.FecRuntime(0, 0, 0.0)
        rt, ratio = U.run_once(cfg, rt, current_ratio=None)  # no enabled arg → default True
        assert ratio == "8:4"
    finally:
        os.close(rfd)


def test_fec_state_set_and_snapshot():
    st = U.FecState(enabled=True)
    assert st.get_enabled() is True
    st.set_enabled(False)
    assert st.get_enabled() is False
    st.publish(ratio="8:4", level=2, driving_loss_pct=3.1, since=100.0)
    snap = st.snapshot()
    assert snap["ratio"] == "8:4"
    assert snap["level"] == 2
    assert snap["driving_loss_pct"] == 3.1
    # snapshot() must return a copy, not the live dict
    snap["ratio"] = "MUT"
    assert st.snapshot()["ratio"] == "8:4"


def test_fec_http_get_returns_snapshot():
    st = U.FecState(enabled=True)
    st.publish(ratio="8:2", level=1, driving_loss_pct=1.2, since=5.0)
    httpd = U.start_fec_http("127.0.0.1:0", st)
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/fec", timeout=2) as r:
            body = json.loads(r.read())
        assert body["ratio"] == "8:2"
        assert body["enabled"] is True
    finally:
        httpd.shutdown()


def test_fec_http_post_sets_enabled():
    st = U.FecState(enabled=True)
    httpd = U.start_fec_http("127.0.0.1:0", st)
    try:
        port = httpd.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/fec",
            data=json.dumps({"enabled": False}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 200
        assert st.get_enabled() is False
    finally:
        httpd.shutdown()


def test_fec_http_post_rejects_non_bool():
    st = U.FecState(enabled=True)
    httpd = U.start_fec_http("127.0.0.1:0", st)
    try:
        port = httpd.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/fec",
            data=json.dumps({"enabled": "yes"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=2)
        assert ei.value.code == 400
    finally:
        httpd.shutdown()


def test_fec_http_post_rejects_oversized_payload():
    st = U.FecState(enabled=True)
    httpd = U.start_fec_http("127.0.0.1:0", st)
    try:
        port = httpd.server_address[1]
        big = b'{"enabled": false, "pad": "' + b'x' * 300 + b'"}'
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/fec",
            data=big, headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=2)
        assert ei.value.code == 413
        assert st.get_enabled() is True  # state unchanged by a rejected payload
    finally:
        httpd.shutdown()


def test_run_publishes_wire_from_tracker(tmp_path):
    import threading as _t, time as _time
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"sessions": {"a": {"session_id": 1, "state": "UP", "loss_pct": 0.0}}}))
    fifo = tmp_path / "srv.fifo"
    os.mkfifo(str(fifo))
    rfd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    cfg = {"fifo": str(fifo), "sbfd_state": str(state_path), "poll_interval_s": 0.01,
           "loss_table": fec_control.DEFAULT_LOSS_TABLE, "ramp_up_ticks": 1, "ramp_down_hold_s": 0}

    class FakeTracker:
        def snapshot(self, now):
            return {"tx_mbps": 3.8, "overhead_pct": 9.0, "sample_age_s": 4.0, "stale": False}

    st = U.FecState(enabled=True)
    stop = _t.Event()
    th = _t.Thread(target=lambda: U.run(cfg, stop, st, wire_tracker=FakeTracker()), daemon=True)
    th.start()
    _time.sleep(0.2)
    try:
        stop.set()
        th.join(timeout=2)
    finally:
        os.close(rfd)
    assert st.snapshot()["wire"] == {"tx_mbps": 3.8, "overhead_pct": 9.0, "sample_age_s": 4.0, "stale": False}


def test_fec_state_wire_defaults_none():
    assert U.FecState(enabled=True).snapshot()["wire"] is None
