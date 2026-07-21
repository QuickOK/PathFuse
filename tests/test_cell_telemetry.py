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
