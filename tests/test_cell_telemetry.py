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
