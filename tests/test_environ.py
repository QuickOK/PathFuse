import json as _json
import math
from pathlib import Path
import environ_ctl as M


def _make_cfg(tmp_path):
    raw = {
        "poll_interval_s": 60, "lookahead_s": 300, "min_speed_ms": 2.0, "max_stale_s": 600,
        "gpsd": {"host": "127.0.0.1", "port": 2947},
        "auto_override": {"path": str(tmp_path / "auto_override.json")},
        "signals": {"precip": {"enabled": True, "url": "http://fc",
                               "current_field": "precipitation", "on_thresh": 0.5,
                               "off_thresh": 0.1, "wet_confirm": 1, "dry_confirm": 2,
                               "reason": "precip ahead"}},
    }
    p = tmp_path / "env.json"
    p.write_text(_json.dumps(raw))
    return str(p)


def test_project_1000m_due_north():
    lat2, lon2 = M.project(0.0, 0.0, bearing_deg=0.0, dist_m=1000.0)
    assert abs(lat2 - 0.00899) < 1e-4
    assert abs(lon2 - 0.0) < 1e-6


def test_build_points_current_only_when_slow():
    pts = M.build_points((10.0, 20.0, 0.5, 90.0), lookahead_s=300, min_speed_ms=2.0)
    assert pts == [(10.0, 20.0)]


def test_build_points_adds_lookahead_when_moving():
    pts = M.build_points((10.0, 20.0, 10.0, 90.0), lookahead_s=300, min_speed_ms=2.0)
    assert len(pts) == 2
    assert pts[0] == (10.0, 20.0)
    assert pts[1][1] > 20.0
    assert abs(pts[1][0] - 10.0) < 0.1


def test_build_points_no_lookahead_when_track_none():
    pts = M.build_points((10.0, 20.0, 10.0, None), lookahead_s=300, min_speed_ms=2.0)
    assert pts == [(10.0, 20.0)]


def test_signal_controller_enters_hazard_on_first_wet():
    sc = M.SignalController("precip", on_thresh=0.5, off_thresh=0.1,
                            wet_confirm=1, dry_confirm=2, reason="precip ahead")
    assert sc.update(0.8) is True
    assert sc.hazard is True


def test_signal_controller_needs_two_dry_to_leave():
    sc = M.SignalController("precip", on_thresh=0.5, off_thresh=0.1,
                            wet_confirm=1, dry_confirm=2, reason="precip ahead")
    sc.update(0.8)
    assert sc.update(0.0) is True
    assert sc.update(0.0) is False
    assert sc.hazard is False


def test_signal_controller_band_resets_streak():
    sc = M.SignalController("precip", on_thresh=0.5, off_thresh=0.1,
                            wet_confirm=2, dry_confirm=2, reason="precip ahead")
    assert sc.update(0.8) is False
    assert sc.update(0.3) is False
    assert sc.update(0.8) is False
    assert sc.update(0.8) is True


def test_combine_hazard_or_with_reasons():
    a = M.SignalController("precip", 0.5, 0.1, 1, 2, "precip ahead")
    b = M.SignalController("smoke", 55.0, 35.0, 1, 2, "smoke")
    a.update(0.8)
    b.update(10.0)
    force_full, reason = M.combine_hazard([a, b])
    assert force_full is True
    assert reason == "precip ahead"

    b.update(80.0)
    force_full, reason = M.combine_hazard([a, b])
    assert force_full is True
    assert reason == "precip ahead; smoke"


def test_combine_hazard_none_active():
    a = M.SignalController("precip", 0.5, 0.1, 1, 2, "precip ahead")
    force_full, reason = M.combine_hazard([a])
    assert force_full is False
    assert reason == ""


def test_build_override_record_shape():
    rec = M.build_override_record(force_full=True, reason="smoke", now=1234.0)
    assert rec == {"force_full": True, "source": "environ_ctl",
                   "reason": "smoke", "set_ts": 1234.0}


def test_signal_controller_band_while_hazard_holds_on():
    sc = M.SignalController("precip", 0.5, 0.1, 1, 2, "precip ahead")
    sc.update(0.8)              # hazard on
    assert sc.update(0.3) is True   # in band -> stays on
    assert sc.update(0.3) is True   # still on; dry_streak never accumulates
    assert sc.hazard is True


def test_parse_open_meteo_single_object():
    data = {"current": {"precipitation": 0.7}}
    assert M.parse_open_meteo(data, "precipitation") == [0.7]


def test_parse_open_meteo_array():
    data = [{"current": {"pm2_5": 12.0}}, {"current": {"pm2_5": 80.0}}]
    assert M.parse_open_meteo(data, "pm2_5") == [12.0, 80.0]


def test_parse_open_meteo_missing_field_is_zero():
    assert M.parse_open_meteo({"current": {}}, "precipitation") == [0.0]


def test_write_override_atomic_roundtrip(tmp_path):
    p = tmp_path / "auto_override.json"
    rec = M.build_override_record(True, "smoke", now=1000.0)
    M.write_override(str(p), rec)
    back = _json.loads(p.read_text())
    assert back == rec
    assert not (tmp_path / "auto_override.json.tmp").exists()


def test_load_env_config_parses_signals(tmp_path):
    raw = {
        "poll_interval_s": 60, "lookahead_s": 300, "min_speed_ms": 2.0,
        "max_stale_s": 600,
        "gpsd": {"host": "127.0.0.1", "port": 2947},
        "auto_override": {"path": str(tmp_path / "auto_override.json")},
        "signals": {
            "precip": {"enabled": True, "url": "http://fc", "current_field": "precipitation",
                       "on_thresh": 0.5, "off_thresh": 0.1, "wet_confirm": 1, "dry_confirm": 2,
                       "reason": "precip ahead"},
            "smoke": {"enabled": False, "url": "http://aq", "current_field": "pm2_5",
                      "on_thresh": 55.0, "off_thresh": 35.0, "wet_confirm": 1, "dry_confirm": 2,
                      "reason": "smoke"},
        },
    }
    p = tmp_path / "env.json"
    p.write_text(_json.dumps(raw))
    cfg = M.load_env_config(str(p))
    assert cfg.poll_interval_s == 60
    assert cfg.auto_override_path == str(tmp_path / "auto_override.json")
    names = [s.controller.name for s in cfg.signals]
    assert names == ["precip"]
    assert cfg.signals[0].url == "http://fc"
    assert cfg.signals[0].current_field == "precipitation"


def test_poll_once_writes_override_on_success(tmp_path, monkeypatch):
    cfg = M.load_env_config(_make_cfg(tmp_path))
    monkeypatch.setattr(M, "get_fix", lambda h, p, **k: (10.0, 20.0, 10.0, 90.0))
    monkeypatch.setattr(M, "fetch_open_meteo", lambda pts, url, field, **k: [0.9])
    M.poll_once(cfg, last_good_mono=100.0, now_mono=100.0)
    rec = _json.loads(Path(cfg.auto_override_path).read_text())
    assert rec["force_full"] is True
    assert rec["reason"] == "precip ahead"


def test_poll_once_failsafe_writes_false_when_stale(tmp_path, monkeypatch):
    cfg = M.load_env_config(_make_cfg(tmp_path))
    monkeypatch.setattr(M, "get_fix", lambda h, p, **k: None)
    M.poll_once(cfg, last_good_mono=0.0, now_mono=1000.0)
    rec = _json.loads(Path(cfg.auto_override_path).read_text())
    assert rec["force_full"] is False
    assert "stale" in rec["reason"]


def test_poll_once_no_write_before_stale_window(tmp_path, monkeypatch):
    cfg = M.load_env_config(_make_cfg(tmp_path))
    monkeypatch.setattr(M, "get_fix", lambda h, p, **k: None)
    M.poll_once(cfg, last_good_mono=900.0, now_mono=1000.0)
    assert not Path(cfg.auto_override_path).exists()


def test_poll_once_partial_fetch_failure_holds_and_writes(tmp_path, monkeypatch):
    import json as _json2
    raw = {
        "poll_interval_s": 60, "lookahead_s": 0, "min_speed_ms": 2.0, "max_stale_s": 600,
        "gpsd": {"host": "127.0.0.1", "port": 2947},
        "auto_override": {"path": str(tmp_path / "auto_override.json")},
        "signals": {
            "precip": {"enabled": True, "url": "http://fc", "current_field": "precipitation",
                       "on_thresh": 0.5, "off_thresh": 0.1, "wet_confirm": 1, "dry_confirm": 2,
                       "reason": "precip ahead"},
            "smoke": {"enabled": True, "url": "http://aq", "current_field": "pm2_5",
                      "on_thresh": 55.0, "off_thresh": 35.0, "wet_confirm": 1, "dry_confirm": 2,
                      "reason": "smoke"},
        },
    }
    p = tmp_path / "env.json"
    p.write_text(_json2.dumps(raw))
    cfg = M.load_env_config(str(p))

    monkeypatch.setattr(M, "get_fix", lambda h, port, **k: (10.0, 20.0, 0.0, None))

    def flaky_fetch(points, url, field, **k):
        if url == "http://aq":
            raise RuntimeError("smoke endpoint down")
        return [0.9]  # precip wet
    monkeypatch.setattr(M, "fetch_open_meteo", flaky_fetch)

    M.poll_once(cfg, last_good_mono=100.0, now_mono=100.0)
    rec = _json.loads(Path(cfg.auto_override_path).read_text())
    # precip succeeded and is wet -> force_full True; smoke held its (clear) default
    assert rec["force_full"] is True
    assert rec["reason"] == "precip ahead"


def test_parse_args_accepts_dash_c():
    ns = M._parse_args(["-c", "/etc/environmental.json"])
    assert ns.config == "/etc/environmental.json"


def test_parse_args_accepts_long_flag():
    ns = M._parse_args(["--config", "/x.json"])
    assert ns.config == "/x.json"
