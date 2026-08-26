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


def test_run_publishes_ladder_for_the_active_profile(tmp_path):
    """The pip row's rung count must follow the profile actually driving the
    leg — the cellular table is one rung shorter than the base table."""
    import threading as _t, time as _time
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"sessions": {"a": {"session_id": 1, "state": "UP", "loss_pct": 0.0}}}))
    fifo = tmp_path / "srv.fifo"
    os.mkfifo(str(fifo))
    rfd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    cfg = {"fifo": str(fifo), "sbfd_state": str(state_path), "poll_interval_s": 0.01,
           "loss_table": fec_control.DEFAULT_LOSS_TABLE, "ramp_up_ticks": 1,
           "ramp_down_hold_s": 0, "wan_profiles": {"wan2": {}}}
    st = U.FecState(mode=fec_control.MODE_MIN_ADAPTIVE, floor_ratio="20:1",
                    profile_names=frozenset({"wan2"}))
    stop = _t.Event()
    th = _t.Thread(target=lambda: U.run(cfg, stop, st), daemon=True)

    def await_ladder(expected, deadline_s=5.0):
        """Poll rather than sleep a fixed interval: the control thread's cadence
        is not ours to assume, and a loaded box makes any fixed wait a flake."""
        end = _time.monotonic() + deadline_s
        seen = None
        while _time.monotonic() < end:
            seen = st.snapshot()["ladder"]
            if seen == expected:
                return seen
            _time.sleep(0.01)
        return seen

    try:
        th.start()
        # Base table: floor 20:1 (5%) sits under 8:2, so it holds rung 0 and the
        # idle leg lights nothing.
        base = {"levels": 5, "floor_level": 0, "applied_level": 0,
                "below_floor": False}
        assert await_ladder(base) == base
        st.set_pushed_link(profile="wan2", signal_floor=False, ts=time.time())
        # Cellular table: 20:1 IS a rung, so the floor occupies rung 1 and only
        # two rungs remain above it.
        cell = {"levels": 4, "floor_level": 1, "applied_level": 1,
                "below_floor": False}
        assert await_ladder(cell) == cell
    finally:
        stop.set()
        th.join(timeout=2)
        os.close(rfd)


def test_fec_state_ladder_defaults_none():
    # Absent until the first control tick; the UI treats None as "unknown".
    assert U.FecState(enabled=True).snapshot()["ladder"] is None


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


# ---------------------------------------------------------------------------
# Task 11: relay-side per-WAN profile + signal floor
# ---------------------------------------------------------------------------
import fec_control as F


def test_pushed_link_staleness():
    st = U.FecState(profile_names=frozenset({"default", "wan1"}))
    st.set_pushed_link("wan1", True, ts=100.0)
    assert st.get_pushed_link(now=105.0, stale_after_s=90.0) == ("wan1", True, 0)
    assert st.get_pushed_link(now=300.0, stale_after_s=90.0) == (None, False, 0)
    st2 = U.FecState()
    assert st2.get_pushed_link(now=0.0, stale_after_s=90.0) == (None, False, 0)


def test_resolve_relay_profile_default_and_named():
    cfg = {"loss_table": F.DEFAULT_LOSS_TABLE, "ramp_up_ticks": 2,
           "ramp_down_hold_s": 20.0,
           "wan_profiles": {"wan1": {"ramp_up_ticks": 1}}}
    table, hyst, sf = U.resolve_relay_profile(cfg, None)
    assert table is cfg["loss_table"] and hyst.ramp_up_ticks == 2
    table, hyst, sf = U.resolve_relay_profile(cfg, "wan1")
    assert [r["fec"] for r in table] == ["8:0", "20:1", "12:1", "8:1"]
    assert hyst.ramp_up_ticks == 1 and hyst.ramp_down_hold_s == 60.0
    assert sf == "12:1"
    table, _, _ = U.resolve_relay_profile(cfg, "default")
    assert table is cfg["loss_table"]


def test_resolve_relay_profile_coerces_garbage_ramp_fields(monkeypatch):
    # C10: the relay config is hand-editable JSON and this runs per-tick
    # inside run_once (which has no broad except) — a bad ramp_up_ticks must
    # fall back to the cellular default (1) rather than raising and killing
    # the daemon.
    warnings = []
    monkeypatch.setattr(U.logging, "warning", lambda *a, **k: warnings.append((a, k)))
    U._warned_profile_values.clear()
    cfg = {"loss_table": F.DEFAULT_LOSS_TABLE, "ramp_up_ticks": 2,
           "ramp_down_hold_s": 20.0,
           "wan_profiles": {"wan1": {"ramp_up_ticks": "garbage"}}}
    table, hyst, sf = U.resolve_relay_profile(cfg, "wan1")
    assert hyst.ramp_up_ticks == 1          # fell back to the cellular default
    assert hyst.ramp_down_hold_s == 60.0    # untouched field still defaults normally
    assert len(warnings) == 1
    # A second call with the SAME bad value must not warn again.
    U.resolve_relay_profile(cfg, "wan1")
    assert len(warnings) == 1


def test_run_once_uses_pushed_profile_and_signal_floor(tmp_path):
    fifo = tmp_path / "fifo"; os.mkfifo(fifo)
    fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)   # give the fifo a reader
    try:
        cfg = {"loss_table": F.DEFAULT_LOSS_TABLE, "ramp_up_ticks": 1,
               "ramp_down_hold_s": 20.0, "fifo": str(fifo),
               "sbfd_state": str(tmp_path / "none.json"),
               "wan_profiles": {"wan1": {}}}
        rt = F.FecRuntime(0, 0, 0.0)
        # zero pushed loss but signal floor on -> 12:1 from the cell table
        rt, ratio = U.run_once(cfg, rt, None, mode="min_adaptive",
                               fixed_ratio="20:1", floor_ratio="8:0",
                               pushed_loss=0.0, pushed_profile="wan1",
                               pushed_signal_floor=True)
        assert ratio == "12:1"
    finally:
        os.close(fd)


def test_post_unknown_wan_profile_rejected():
    st = U.FecState(profile_names=frozenset({"default", "wan1"}))
    httpd = U.start_fec_http("127.0.0.1:0", st)
    assert httpd is not None
    try:
        host, port = httpd.server_address[:2]
        body = json.dumps({"wan_profile": "nope"}).encode()
        req = urllib.request.Request(
            f"http://{host}:{port}/fec", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected HTTP 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400
        # rejection must leave the pushed state untouched
        assert st.get_pushed_link(now=time.time(), stale_after_s=90.0) == (None, False, 0)
        # and a VALID profile round-trips
        ok_body = json.dumps({"wan_profile": "wan1", "signal_floor": True}).encode()
        ok_req = urllib.request.Request(
            f"http://{host}:{port}/fec", data=ok_body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(ok_req, timeout=5) as resp:
            assert resp.status == 200
        assert st.get_pushed_link(now=time.time(), stale_after_s=90.0) == ("wan1", True, 0)
    finally:
        httpd.shutdown()


def test_lone_signal_floor_accepted_but_inert_by_design():
    # C7 (ratified design decision, do NOT change the code): signal_floor is
    # part of the profile contract, not a standalone toggle — pushing it
    # without a wan_profile EVER pushed for this link is a no-op on the base
    # table anyway, so it 200s but get_pushed_link still reports (None,
    # False, 0). This pins that behavior against an accidental "fix" later.
    st = U.FecState(profile_names=frozenset({"default", "wan1"}))
    httpd = U.start_fec_http("127.0.0.1:0", st)
    assert httpd is not None
    try:
        host, port = httpd.server_address[:2]
        body = json.dumps({"signal_floor": True}).encode()
        req = urllib.request.Request(
            f"http://{host}:{port}/fec", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
        assert st.get_pushed_link(now=time.time(), stale_after_s=90.0) == (None, False, 0)
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# Location floor on the relay leg: the client pushes the level it resolved for
# the place the vehicle is standing in, and the relay lifts ITS leg the same
# raise-only way. It rides the pushed link, so it expires with the profile.
# ---------------------------------------------------------------------------

def test_pushed_link_carries_the_location_level_and_expires_with_it():
    st = U.FecState(profile_names=frozenset({"default", "wan1"}))
    st.set_pushed_link("wan1", True, ts=100.0, location_level=3)
    assert st.get_pushed_link(now=105.0, stale_after_s=90.0) == ("wan1", True, 3)
    # Stale must drop the LEVEL too, not just the table: a client that stopped
    # talking is no longer vouching for the place it last reported.
    assert st.get_pushed_link(now=300.0, stale_after_s=90.0) == (None, False, 0)
    st2 = U.FecState()
    assert st2.get_pushed_link(now=0.0, stale_after_s=90.0) == (None, False, 0)


def test_post_location_level_validation():
    st = U.FecState(profile_names=frozenset({"default", "wan1"}))
    httpd = U.start_fec_http("127.0.0.1:0", st)
    assert httpd is not None
    host, port = httpd.server_address[:2]

    def post(obj):
        body = json.dumps(obj).encode()
        req = urllib.request.Request(
            f"http://{host}:{port}/fec", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status

    def post_expect_400(obj):
        try:
            post(obj)
        except urllib.error.HTTPError as e:
            return e.code
        raise AssertionError(f"expected HTTP 400 for {obj!r}")

    try:
        assert post({"location_level": 2}) == 200
        assert st._pushed_location_level == 2
        # bool is an int subclass — it must not sneak through as level 1.
        assert post_expect_400({"location_level": True}) == 400
        assert post_expect_400({"location_level": -1}) == 400
        assert post_expect_400({"location_level": "2"}) == 400
        # A rejection leaves the accepted value standing, untouched.
        assert st._pushed_location_level == 2
        # 0 is the release, not junk: accepted, and inert downstream.
        assert post({"location_level": 0}) == 200
        assert st._pushed_location_level == 0
        # Alongside a profile it round-trips through the pushed link.
        assert post({"wan_profile": "wan1", "location_level": 2}) == 200
        assert st.get_pushed_link(now=time.time(), stale_after_s=90.0) \
            == ("wan1", False, 2)
    finally:
        httpd.shutdown()


def _run_once_ratio(tmp_path, **kwargs):
    """run_once against a real fifo with a reader; returns the ratio written."""
    fifo = tmp_path / "loc.fifo"
    os.mkfifo(str(fifo))
    fd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    try:
        cfg = {"loss_table": F.DEFAULT_LOSS_TABLE, "ramp_up_ticks": 1,
               "ramp_down_hold_s": 0.0, "fifo": str(fifo),
               "sbfd_state": str(tmp_path / "none.json"),
               "wan_profiles": {"wan1": {}}}
        rt = F.FecRuntime(0, 0, 0.0)
        _rt, ratio = U.run_once(cfg, rt, None, **kwargs)
        return ratio
    finally:
        os.close(fd)


def test_run_once_lifts_to_the_pushed_location_level(tmp_path):
    # Zero loss maps to level 0; the pushed level is the only thing lifting it.
    ratio = _run_once_ratio(tmp_path, pushed_loss=0.0, pushed_location_level=3)
    assert ratio == F.level_to_ratio(3, F.DEFAULT_LOSS_TABLE)


def test_run_once_lifts_to_the_location_level_without_a_loss_sample(tmp_path):
    # sbfd_state does not exist -> no loss sample; the floor must still hold,
    # exactly as the signal floor does on this branch.
    ratio = _run_once_ratio(tmp_path, pushed_loss=None, pushed_location_level=3)
    assert ratio == F.level_to_ratio(3, F.DEFAULT_LOSS_TABLE)


def test_run_once_location_level_never_lowers(tmp_path):
    # 12% loss -> level 4 on the base table; a pushed level 1 must not pull the
    # leg back down to it.
    ratio = _run_once_ratio(tmp_path, pushed_loss=12.0, pushed_location_level=1)
    assert ratio == F.level_to_ratio(4, F.DEFAULT_LOSS_TABLE)


def test_run_once_location_level_is_clamped_to_the_pushed_profile_table(tmp_path):
    # The client learned the level against ITS table; the cellular table this
    # push selects is a rung shorter, so level 4 must land on its top row
    # (8:1) rather than index past it.
    ratio = _run_once_ratio(tmp_path, pushed_loss=0.0, pushed_profile="wan1",
                            pushed_location_level=4)
    assert ratio == F.level_to_ratio(3, F.DEFAULT_CELL_LOSS_TABLE) == "8:1"


def test_run_publishes_the_pushed_location_level(tmp_path):
    """GET /fec must show the level the relay leg is holding, so the operator
    can see the two legs agree."""
    import threading as _t, time as _time
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(
        {"sessions": {"a": {"session_id": 1, "state": "UP", "loss_pct": 0.0}}}))
    fifo = tmp_path / "srv.fifo"
    os.mkfifo(str(fifo))
    rfd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    cfg = {"fifo": str(fifo), "sbfd_state": str(state_path),
           "poll_interval_s": 0.01, "loss_table": F.DEFAULT_LOSS_TABLE,
           "ramp_up_ticks": 1, "ramp_down_hold_s": 0,
           "wan_profiles": {"wan1": {}}}
    st = U.FecState(mode=F.MODE_ADAPTIVE,
                    profile_names=frozenset({"default", "wan1"}))
    httpd = U.start_fec_http("127.0.0.1:0", st)
    assert httpd is not None
    stop = _t.Event()
    th = _t.Thread(target=lambda: U.run(cfg, stop, st), daemon=True)

    def await_field(name, expected, deadline_s=5.0):
        end = _time.monotonic() + deadline_s
        seen = None
        while _time.monotonic() < end:
            seen = st.snapshot().get(name)
            if seen == expected:
                return seen
            _time.sleep(0.01)
        return seen

    try:
        th.start()
        assert await_field("location_level", 0) == 0
        host, port = httpd.server_address[:2]
        body = json.dumps({"wan_profile": "wan1", "location_level": 2}).encode()
        req = urllib.request.Request(
            f"http://{host}:{port}/fec", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
        assert await_field("location_level", 2) == 2
        with urllib.request.urlopen(f"http://{host}:{port}/fec", timeout=5) as r:
            assert json.loads(r.read())["location_level"] == 2
    finally:
        stop.set()
        th.join(timeout=2)
        httpd.shutdown()
        os.close(rfd)
