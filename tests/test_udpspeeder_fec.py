import json
import time
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
        def rx_snapshot(self, now):
            return None

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


# ---------------------------------------------------------------------------
# Client-pushed loss (2026-07-08): the relay's TX leg (relay->client) repairs
# loss measured at the CLIENT; relay-local sbfd loss is the opposite
# (client->relay) direction and is only a fallback for older clients.
# ---------------------------------------------------------------------------

def test_pushed_loss_fresh_returned():
    st = U.FecState(enabled=True)
    st.set_pushed_loss(1.47, ts=100.0)
    assert st.get_pushed_loss(now=150.0, stale_after_s=90.0) == 1.47


def test_pushed_loss_stale_returns_none():
    st = U.FecState(enabled=True)
    st.set_pushed_loss(1.47, ts=100.0)
    assert st.get_pushed_loss(now=200.0, stale_after_s=90.0) is None


def test_pushed_loss_absent_returns_none():
    st = U.FecState(enabled=True)
    assert st.get_pushed_loss(now=100.0, stale_after_s=90.0) is None


def test_run_once_pushed_loss_overrides_local(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"sessions": {"a": {"session_id": 1, "state": "UP", "loss_pct": 0.0}}}))
    fifo = tmp_path / "srv.fifo"
    os.mkfifo(str(fifo))
    rfd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    cfg = {"fifo": str(fifo), "sbfd_state": str(state), "poll_interval_s": 0.01,
           "loss_table": fec_control.DEFAULT_LOSS_TABLE, "ramp_up_ticks": 1, "ramp_down_hold_s": 0}
    try:
        rt = fec_control.FecRuntime(0, 0, 0.0)
        rt, ratio = U.run_once(cfg, rt, current_ratio=None, pushed_loss=6.0)
        assert ratio == "8:6"   # client-measured 6.0% -> level 3, despite local 0.0
        assert os.read(rfd, 64).decode() == "fec 8:6\n"
    finally:
        os.close(rfd)


def test_run_once_pushed_loss_works_without_local_state(tmp_path):
    fifo = tmp_path / "srv.fifo"
    os.mkfifo(str(fifo))
    rfd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    cfg = {"fifo": str(fifo), "sbfd_state": "/nonexistent/state.json", "poll_interval_s": 0.01,
           "loss_table": fec_control.DEFAULT_LOSS_TABLE, "ramp_up_ticks": 1, "ramp_down_hold_s": 0}
    try:
        rt = fec_control.FecRuntime(0, 0, 0.0)
        rt, ratio = U.run_once(cfg, rt, current_ratio=None, pushed_loss=1.0)
        assert ratio == "8:2"   # 1.0% -> level 1 even with no local sbfd state
    finally:
        os.close(rfd)


def test_fec_http_post_stores_client_loss():
    st = U.FecState(enabled=True)
    httpd = U.start_fec_http("127.0.0.1:0", st)
    try:
        port = httpd.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/fec",
            data=json.dumps({"mode": "adaptive", "client_loss_pct": 2.5}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 200
        assert st.get_pushed_loss(now=time.time(), stale_after_s=90.0) == 2.5
    finally:
        httpd.shutdown()


def test_fec_http_post_accepts_loss_only_payload():
    st = U.FecState(enabled=True)
    httpd = U.start_fec_http("127.0.0.1:0", st)
    try:
        port = httpd.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/fec",
            data=json.dumps({"client_loss_pct": 0.0}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 200
        assert st.get_pushed_loss(now=time.time(), stale_after_s=90.0) == 0.0
    finally:
        httpd.shutdown()


def test_fec_http_post_rejects_bad_client_loss():
    st = U.FecState(enabled=True)
    httpd = U.start_fec_http("127.0.0.1:0", st)
    try:
        port = httpd.server_address[1]
        for bad in ("2.5", -1.0, 101.0, True):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/fec",
                data=json.dumps({"mode": "adaptive", "client_loss_pct": bad}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(req, timeout=2)
            assert ei.value.code == 400
        assert st.get_pushed_loss(now=time.time(), stale_after_s=90.0) is None
    finally:
        httpd.shutdown()


def _post_fec(st, payload):
    """POST to a freshly bound relay /fec. Returns (status, body_dict)."""
    httpd = U.start_fec_http("127.0.0.1:0", st)
    try:
        port = httpd.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/fec",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_fec_state_holds_and_returns_floor():
    st = U.FecState(mode="min_adaptive", fixed_ratio="8:2", floor_ratio="8:1")
    assert st.get_desired() == ("min_adaptive", "8:2", "8:1")
    st.set_floor_ratio("20:1")
    assert st.get_floor_ratio() == "20:1"


def test_fec_state_defaults_floor_to_module_default():
    st = U.FecState(mode="min_adaptive")
    assert st.get_floor_ratio() == fec_control.DEFAULT_FLOOR_RATIO


def test_fec_state_snapshot_exposes_floor():
    st = U.FecState(mode="min_adaptive", floor_ratio="8:1")
    assert st.snapshot()["floor_ratio"] == "8:1"


def test_run_once_uses_state_floor_not_config(tmp_path):
    # min_adaptive with zero loss idles the adaptive engine at 8:0, which
    # apply_mode lifts to the floor. The floor passed in must win over the
    # config value, or a pushed floor would be silently ignored.
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"sessions": {"a": {"session_id": 1,
                                                   "state": "UP",
                                                   "loss_pct": 0.0}}}))
    fifo = tmp_path / "srv.fifo"
    os.mkfifo(str(fifo))
    rfd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    cfg = {"fifo": str(fifo), "sbfd_state": str(state), "poll_interval_s": 0.01,
           "loss_table": fec_control.DEFAULT_LOSS_TABLE, "ramp_up_ticks": 1,
           "ramp_down_hold_s": 0, "floor_ratio": "20:1"}   # config says 20:1
    try:
        rt = fec_control.FecRuntime(0, 0, 0.0)
        rt, ratio = U.run_once(cfg, rt, current_ratio=None,
                               mode=fec_control.MODE_MIN_ADAPTIVE,
                               floor_ratio="8:1")           # caller says 8:1
        assert ratio == "8:1"
        assert os.read(rfd, 64).decode() == "fec 8:1\n"
    finally:
        os.close(rfd)


def test_run_once_falls_back_to_config_floor_when_none(tmp_path):
    # floor_ratio=None must preserve today's behavior: the config value applies.
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"sessions": {"a": {"session_id": 1,
                                                   "state": "UP",
                                                   "loss_pct": 0.0}}}))
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
        assert ratio == "20:1"
    finally:
        os.close(rfd)


def test_post_fec_accepts_floor_ratio_as_percent():
    st = U.FecState(mode="min_adaptive")
    code, body = _post_fec(st, {"mode": "min_adaptive", "floor_ratio": "25%"})
    assert code == 200
    assert body["floor_ratio"] == "8:2"
    assert st.get_floor_ratio() == "8:2"


def test_post_fec_rejects_bad_floor_ratio():
    st = U.FecState(mode="min_adaptive", floor_ratio="20:1")
    code, body = _post_fec(st, {"mode": "min_adaptive", "floor_ratio": "abc"})
    assert code == 400
    assert "floor_ratio" in body["error"]
    assert st.get_floor_ratio() == "20:1"   # a rejected entry changes nothing


def test_post_fec_without_floor_leaves_it_unchanged():
    st = U.FecState(mode="min_adaptive", floor_ratio="8:1")
    code, _ = _post_fec(st, {"mode": "adaptive"})
    assert code == 200
    assert st.get_floor_ratio() == "8:1"


def test_post_fec_accepts_floor_only_payload():
    st = U.FecState(mode="min_adaptive", floor_ratio="20:1")
    code, _ = _post_fec(st, {"floor_ratio": "8:1"})
    assert code == 200
    assert st.get_floor_ratio() == "8:1"


def test_post_fec_invalid_loss_leaves_ratios_unchanged():
    # A payload carrying a good floor and a bad client_loss_pct must be
    # rejected whole: no half-applied mutation behind the 400.
    st = U.FecState(mode="min_adaptive", fixed_ratio="8:2", floor_ratio="20:1")
    code, body = _post_fec(st, {"floor_ratio": "8:1", "fixed_ratio": "8:4",
                                "client_loss_pct": 999})
    assert code == 400
    assert "client_loss_pct" in body["error"]
    assert st.get_floor_ratio() == "20:1"
    assert st.get_fixed_ratio() == "8:2"


def test_set_desired_applies_the_whole_triple_atomically():
    st = U.FecState(mode="adaptive", fixed_ratio="8:2", floor_ratio="20:1")
    st.set_desired(mode="min_adaptive", fixed_ratio="8:4", floor_ratio="8:1")
    assert st.get_desired() == ("min_adaptive", "8:4", "8:1")


def test_set_desired_leaves_none_fields_untouched():
    st = U.FecState(mode="min_adaptive", fixed_ratio="8:2", floor_ratio="20:1")
    st.set_desired(floor_ratio="8:1")
    assert st.get_desired() == ("min_adaptive", "8:2", "8:1")


def test_snapshot_reflects_a_post_before_the_next_publish():
    # GET /fec must not report a stale floor for up to a full control tick.
    st = U.FecState(mode="min_adaptive", floor_ratio="20:1")
    code, _ = _post_fec(st, {"floor_ratio": "8:1"})
    assert code == 200
    assert st.snapshot()["floor_ratio"] == "8:1"


def test_run_once_applies_a_raised_floor_without_a_loss_sample(tmp_path):
    # No readable sbfd state: a floor the operator just raised must still take
    # effect, rather than waiting for a loss sample that may never arrive.
    fifo = tmp_path / "srv.fifo"
    os.mkfifo(str(fifo))
    rfd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    cfg = {"fifo": str(fifo), "sbfd_state": "/nonexistent/state.json",
           "poll_interval_s": 0.01, "loss_table": fec_control.DEFAULT_LOSS_TABLE,
           "ramp_up_ticks": 1, "ramp_down_hold_s": 0, "floor_ratio": "20:1"}
    try:
        rt = fec_control.FecRuntime(0, 0, 0.0)
        rt, ratio = U.run_once(cfg, rt, current_ratio="20:1",
                               mode=fec_control.MODE_MIN_ADAPTIVE,
                               floor_ratio="8:4")
        assert ratio == "8:4"
        assert os.read(rfd, 64).decode() == "fec 8:4\n"
    finally:
        os.close(rfd)


def test_run_once_coerces_a_hand_edited_config_floor(tmp_path):
    fifo = tmp_path / "srv.fifo"
    os.mkfifo(str(fifo))
    rfd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    cfg = {"fifo": str(fifo), "sbfd_state": "/nonexistent/state.json",
           "poll_interval_s": 0.01, "loss_table": fec_control.DEFAULT_LOSS_TABLE,
           "ramp_up_ticks": 1, "ramp_down_hold_s": 0, "floor_ratio": "garbage"}
    try:
        rt = fec_control.FecRuntime(0, 0, 0.0)
        rt, ratio = U.run_once(cfg, rt, current_ratio=None,
                               mode=fec_control.MODE_MIN_ADAPTIVE)
        assert ratio == fec_control.DEFAULT_FLOOR_RATIO
    finally:
        os.close(rfd)


def test_set_desired_returns_the_applied_triple():
    st = U.FecState(mode="adaptive", fixed_ratio="8:2", floor_ratio="20:1")
    assert st.set_desired(mode="min_adaptive", floor_ratio="8:1") == \
        ("min_adaptive", "8:2", "8:1")


def test_fec_state_coerces_a_percent_seeded_from_config():
    st = U.FecState(mode="min_adaptive", fixed_ratio="50%", floor_ratio="25%")
    assert st.get_desired() == ("min_adaptive", "8:4", "8:2")


def test_fec_state_coerces_garbage_seeded_from_config():
    st = U.FecState(mode="min_adaptive", fixed_ratio="junk", floor_ratio="junk")
    assert st.get_fixed_ratio() == fec_control.DEFAULT_FIXED_RATIO
    assert st.get_floor_ratio() == fec_control.DEFAULT_FLOOR_RATIO


def test_fec_state_from_cfg_seeds_and_coerces():
    st = U.fec_state_from_cfg({"mode": "min_adaptive", "fixed_ratio": "50%",
                               "floor_ratio": "8:1"})
    assert st.get_desired() == ("min_adaptive", "8:4", "8:1")


def test_fec_state_from_cfg_defaults_on_empty_cfg():
    st = U.fec_state_from_cfg({})
    assert st.get_desired() == (fec_control.DEFAULT_MODE,
                                fec_control.DEFAULT_FIXED_RATIO,
                                fec_control.DEFAULT_FLOOR_RATIO)


def test_fec_state_snapshot_seeds_rx_none():
    s = U.FecState()
    assert s.snapshot()["rx"] is None


def test_publish_rx_appears_in_snapshot():
    s = U.FecState()
    s.publish(rx={"delivered_per_s": 10.0})
    assert s.snapshot()["rx"] == {"delivered_per_s": 10.0}
