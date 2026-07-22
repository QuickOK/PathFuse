import json
import cell_telemetry as CT


MODEL = {
    "wwan": {"signalStrength": {"rsrp": -98, "rsrq": "-11", "sinr": 13.5}},
    "wwanadv": {"cellId": 1234567, "curBand": "LTE B2"},
}


def test_extract_signal_happy_path():
    r = CT.extract_signal(MODEL)
    assert r == {"rsrp": -98.0, "rsrq": -11.0, "sinr": 13.5,
                 "cell_id": "1234567", "band": "LTE B2"}


def test_extract_signal_tolerates_units_and_junk():
    m = {"wwanadv": {"rsrp": "-105dBm", "rsrq": "bars", "sinr": None}}
    r = CT.extract_signal(m)
    assert r["rsrp"] == -105.0
    assert r["rsrq"] is None          # non-numeric -> None, never raises
    assert r["cell_id"] is None and r["band"] is None


def test_extract_signal_handles_non_dict():
    assert CT.extract_signal(None) == {"rsrp": None, "rsrq": None, "sinr": None,
                                       "cell_id": None, "band": None}


def test_extract_signal_rejects_malformed_identity_fields():
    # cellId/curBand nested as dict/list (wrong-shape or unauthenticated
    # model.json) must not be str()'d into garbage, and must not count as
    # "signal present" for read_signal's login-fallback trigger.
    m = {"wwanadv": {"cellId": {"a": 1}, "curBand": ["x"]}}
    r = CT.extract_signal(m)
    assert r == {"rsrp": None, "rsrq": None, "sinr": None,
                 "cell_id": None, "band": None}
    assert CT.read_signal(FakeClient([m])) is None


def test_atomic_write_json(tmp_path):
    p = tmp_path / "out.json"
    CT.atomic_write_json(str(p), {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}
    assert list(tmp_path.iterdir()) == [p]   # no tmp litter


def test_load_config_defaults(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"admin_url": "http://192.0.2.1"}))
    cfg = CT.load_config(str(p))
    assert cfg.admin_url == "http://192.0.2.1"
    assert cfg.iface == "wan1"
    assert cfg.poll_interval_s == 2.0
    assert cfg.state_path == "/run/sbfd-ctl/cell_telemetry.json"
    assert cfg.secret_path is None
    assert cfg.login_backoff_s == 60.0


class FakeClient:
    def __init__(self, models, login_ok=True):
        self.models = list(models)      # one per fetch_model() call
        self.login_ok = login_ok
        self.login_calls = 0

    def fetch_model(self):
        return self.models.pop(0) if self.models else None

    def login(self, password):
        self.login_calls += 1
        return self.login_ok


def _cfg(tmp_path, **kw):
    kw.setdefault("admin_url", "http://192.0.2.1")
    kw.setdefault("state_path", str(tmp_path / "cell.json"))
    return CT.CtCfg(**kw)


def test_read_signal_none_when_all_metrics_absent():
    assert CT.read_signal(FakeClient([{"session": {}}])) is None
    assert CT.read_signal(FakeClient([None])) is None


def test_poll_once_writes_state_with_set_ts(tmp_path):
    cfg = _cfg(tmp_path)
    reading, ts = CT.poll_once(FakeClient([MODEL]), cfg, now=1000.0,
                               last_login_ts=None)
    assert reading["rsrp"] == -98.0
    on_disk = json.loads((tmp_path / "cell.json").read_text())
    assert on_disk["set_ts"] == 1000.0 and on_disk["rsrq"] == -11.0
    assert ts is None                    # no login was needed


def test_poll_once_failure_leaves_state_untouched(tmp_path):
    cfg = _cfg(tmp_path)
    CT.poll_once(FakeClient([MODEL]), cfg, now=1000.0, last_login_ts=None)
    CT.poll_once(FakeClient([None]), cfg, now=1010.0, last_login_ts=None)
    assert json.loads((tmp_path / "cell.json").read_text())["set_ts"] == 1000.0


def test_poll_once_login_fallback(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("hunter2\n")
    cfg = _cfg(tmp_path, secret_path=str(secret))
    # unauthenticated model lacks signal; post-login model has it
    client = FakeClient([{"session": {}}, MODEL])
    reading, ts = CT.poll_once(client, cfg, now=1000.0, last_login_ts=None)
    assert client.login_calls == 1
    assert reading["rsrp"] == -98.0 and ts == 1000.0


def test_poll_once_login_backoff(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("hunter2")
    cfg = _cfg(tmp_path, secret_path=str(secret))
    client = FakeClient([{}, {}], login_ok=False)
    _, ts = CT.poll_once(client, cfg, now=1000.0, last_login_ts=990.0)
    assert client.login_calls == 0       # inside backoff window
    assert ts == 990.0


def test_poll_once_state_write_failure_preserves_login_backoff(tmp_path):
    """A failed state-file persist (e.g. state dir missing) must not look
    like a failed poll: the reading is still returned and last_login_ts
    still advances, so the caller's backoff bookkeeping survives and the
    next poll doesn't hammer the modem's admin login endpoint again."""
    secret = tmp_path / "secret"
    secret.write_text("hunter2")
    unwritable_state_path = str(tmp_path / "nosuchdir" / "cell.json")
    cfg = _cfg(tmp_path, secret_path=str(secret), state_path=unwritable_state_path)
    client = FakeClient([{"session": {}}, MODEL])
    reading, ts = CT.poll_once(client, cfg, now=1000.0, last_login_ts=None)
    assert reading["rsrp"] == -98.0
    assert ts == 1000.0
    assert client.login_calls == 1


def _det(tmp_path, **kw):
    kw.setdefault("admin_url", "http://192.0.2.1")
    kw.setdefault("state_path", str(tmp_path / "cell.json"))
    kw.setdefault("handoff_path", str(tmp_path / "handoff.json"))
    cfg = CT.CtCfg(**kw)
    return cfg, CT.HandoffDetector(cfg)


def _reading(cell_id="100", rsrq=-9.0):
    return {"rsrp": -90.0, "rsrq": rsrq, "sinr": 10.0,
            "cell_id": cell_id, "band": "LTE B2"}


def test_handoff_cell_change_fires_once_and_rate_limits(tmp_path):
    cfg, det = _det(tmp_path)
    assert det.update(_reading("100"), None, now=1000.0) is None   # first sample: no pair
    assert det.update(_reading("100"), None, now=1002.0) is None   # same cell
    r = det.update(_reading("200"), None, now=1004.0)
    assert r == "cell_change:100->200"
    # inside the open window AND inside min_interval: ignored, not extended
    assert det.update(_reading("300"), None, now=1006.0) is None
    # after the window but still inside min_interval (15 s from open): still
    # ignored — keep the 2 s poll cadence so every pair stays fresh
    for t in (1008.0, 1010.0, 1012.0, 1014.0, 1016.0, 1018.0):
        assert det.update(_reading("400"), None, now=t) is None
    # past min_interval (1020 - 1004 >= 15) with a fresh pair: a NEW change fires
    assert det.update(_reading("500"), None, now=1020.0) == "cell_change:400->500"


def test_handoff_rsrq_drop_needs_consecutive_fresh_pair(tmp_path):
    cfg, det = _det(tmp_path)
    det.update(_reading(rsrq=-8.0), None, now=1000.0)
    r = det.update(_reading(rsrq=-12.5), None, now=1002.0)   # 4.5 dB drop
    assert r == "rsrq_drop:-8.0->-12.5"
    det2 = CT.HandoffDetector(cfg)
    det2.update(_reading(rsrq=-8.0), None, now=1000.0)
    # 4.5 dB drop but the pair spans a 20 s gap (> 2 * poll_interval_s): invalid
    assert det2.update(_reading(rsrq=-12.5), None, now=1020.0) is None


def test_handoff_gap_does_not_fire_cell_change(tmp_path):
    # hotspot reboot: samples stop, then resume with a new cell id -> must NOT fire
    cfg, det = _det(tmp_path)
    det.update(_reading("100"), None, now=1000.0)
    assert det.update(_reading("999"), None, now=1060.0) is None


def test_handoff_loss_spike_reactive_fallback(tmp_path):
    cfg, det = _det(tmp_path)
    det.update(_reading(), 0.0, now=1000.0)
    r = det.update(None, 3.5, now=1002.0)      # modem unreadable, loss spiking
    assert r == "loss_spike:3.5"
    assert det.update(None, 0.1, now=1020.0) is None   # below threshold


def test_handoff_disabled_never_fires(tmp_path):
    cfg, det = _det(tmp_path, handoff_enabled=False)
    det.update(_reading("100"), None, now=1000.0)
    assert det.update(_reading("200"), 5.0, now=1002.0) is None


def _sbfd_session(**kw):
    """A session entry shaped like sbfd.py's write_state_file actually emits
    it (sessions keyed by session NAME, no "wan" field — see sbfd.py
    write_state_file ~line 311)."""
    base = {"session_id": 1, "state": "UP", "state_since": 900.0,
            "uptime_s": 100.0, "tx_seq": 5, "last_rx_seq": 5,
            "last_rx_age_s": 0.2, "consecutive_miss": 0, "consecutive_hit": 5,
            "rtt_ms": 30.0, "loss_pct": 1.5, "peer": "203.0.113.1:5000",
            "iface": "wan1"}
    base.update(kw)
    return base


def test_read_wan_loss_fail_open(tmp_path):
    assert CT.read_wan_loss(str(tmp_path / "missing.json")) is None
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"timestamp": 1000.0,
                             "sessions": {"wan1": _sbfd_session(loss_pct=1.5)}}))
    assert CT.read_wan_loss(str(p)) == 1.5


def test_read_wan_loss_matches_via_iface_when_name_differs(tmp_path):
    # Session named after the tunnel/peer, not the wan id -- must still match
    # by its "iface" field.
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"timestamp": 1000.0, "sessions": {
        "primary-cell": _sbfd_session(loss_pct=2.5, iface="wan1")}}))
    assert CT.read_wan_loss(str(p), wan="wan1") == 2.5


def test_read_wan_loss_malformed_root_fails_open(tmp_path):
    # A non-dict JSON root (list, string, number, null...) must fail open,
    # not raise AttributeError/TypeError out to the poll loop's
    # log.exception spam.
    p = tmp_path / "bad.json"
    p.write_text(json.dumps([1, 2, 3]))
    assert CT.read_wan_loss(str(p)) is None


def test_poll_once_writes_handoff_file(tmp_path):
    cfg, det = _det(tmp_path)
    client = FakeClient([MODEL, dict(MODEL, wwanadv={"cellId": 777,
                                                     "curBand": "LTE B2"})])
    CT.poll_once(client, cfg, now=1000.0, last_login_ts=None, detector=det,
                 wan_loss=None)
    CT.poll_once(client, cfg, now=1002.0, last_login_ts=None, detector=det,
                 wan_loss=None)
    h = json.loads((tmp_path / "handoff.json").read_text())
    assert h["set_ts"] == 1002.0 and h["until_ts"] == 1006.0
    assert h["reason"].startswith("cell_change:")


def test_handoff_config_parse_and_validation(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"admin_url": "http://192.0.2.1",
                             "handoff": {"window_s": 5, "min_interval_s": 30}}))
    cfg = CT.load_config(str(p))
    assert cfg.handoff_window_s == 5.0 and cfg.handoff_min_interval_s == 30.0
    assert cfg.handoff_enabled is True
    p.write_text(json.dumps({"admin_url": "http://192.0.2.1",
                             "handoff": {"window_s": 20, "min_interval_s": 15}}))
    import pytest
    with pytest.raises(ValueError):
        CT.load_config(str(p))


def test_handoff_config_rejects_non_positive_rsrq_drop_db(tmp_path):
    # A zero/negative rsrq_drop_db makes every fresh pair a trigger -- a
    # permanent duplication duty cycle silently burning the data cap.
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"admin_url": "http://192.0.2.1",
                             "handoff": {"rsrq_drop_db": 0}}))
    import pytest
    with pytest.raises(ValueError):
        CT.load_config(str(p))


def test_handoff_config_rejects_non_positive_loss_spike_pct(tmp_path):
    # Same failure mode as rsrq_drop_db, via the loss-spike fallback trigger.
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"admin_url": "http://192.0.2.1",
                             "handoff": {"loss_spike_pct": -1}}))
    import pytest
    with pytest.raises(ValueError):
        CT.load_config(str(p))
