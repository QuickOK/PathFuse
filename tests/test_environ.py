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


def test_classify_codes_hazard_and_clear():
    codes = {80, 81, 82, 95, 96, 99}
    assert M.classify_codes([95.0], codes) == [1.0]      # thunderstorm
    assert M.classify_codes([81.0], codes) == [1.0]      # moderate rain showers
    assert M.classify_codes([45.0], codes) == [0.0]      # fog: not a hazard
    assert M.classify_codes([0.0], codes) == [0.0]       # clear sky


def test_classify_codes_max_picks_hazard_across_points():
    codes = {95, 96, 99}
    # current point clear (3 = overcast), look-ahead point a thunderstorm (95)
    vals = M.classify_codes([3.0, 95.0], codes)
    assert max(vals) == 1.0


def test_classify_codes_junk_value_is_clear():
    assert M.classify_codes([None, "x"], {95}) == [0.0, 0.0]


def test_weather_code_signal_thresholds_fire_on_first_storm():
    # how the "weather" signal is wired: binary classifier + on=1.0/off=0.0
    sc = M.SignalController("weather", on_thresh=1.0, off_thresh=0.0,
                            wet_confirm=1, dry_confirm=3, reason="thunderstorm ahead")
    assert sc.update(1.0) is True       # storm code -> hazard immediately
    assert sc.update(0.0) is True       # one clear poll: dry_confirm=3 holds it
    assert sc.update(0.0) is True
    assert sc.update(0.0) is False      # third clear poll clears it


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


def test_load_env_config_parses_hazard_codes(tmp_path):
    raw = {
        "poll_interval_s": 60, "lookahead_s": 300, "min_speed_ms": 2.0, "max_stale_s": 600,
        "gpsd": {"host": "127.0.0.1", "port": 2947},
        "auto_override": {"path": str(tmp_path / "auto_override.json")},
        "signals": {
            "precip": {"enabled": True, "url": "http://fc", "current_field": "precipitation",
                       "on_thresh": 0.5, "off_thresh": 0.1, "reason": "precip ahead"},
            "weather": {"enabled": True, "url": "http://fc", "current_field": "weather_code",
                        "hazard_codes": [80, 95, 96, 99], "on_thresh": 1.0, "off_thresh": 0.0,
                        "reason": "thunderstorm ahead"},
        },
    }
    p = tmp_path / "env.json"
    p.write_text(_json.dumps(raw))
    cfg = M.load_env_config(str(p))
    by_name = {s.controller.name: s for s in cfg.signals}
    assert by_name["precip"].hazard_codes is None
    assert by_name["weather"].hazard_codes == {80, 95, 96, 99}
    assert by_name["weather"].current_field == "weather_code"


def test_poll_once_weather_code_storm_triggers_full(tmp_path, monkeypatch):
    raw = {
        "poll_interval_s": 60, "lookahead_s": 0, "min_speed_ms": 2.0, "max_stale_s": 600,
        "gpsd": {"host": "127.0.0.1", "port": 2947},
        "auto_override": {"path": str(tmp_path / "auto_override.json")},
        "signals": {
            "weather": {"enabled": True, "url": "http://fc", "current_field": "weather_code",
                        "hazard_codes": [80, 81, 82, 95, 96, 99], "on_thresh": 1.0,
                        "off_thresh": 0.0, "wet_confirm": 1, "dry_confirm": 3,
                        "reason": "thunderstorm ahead"},
        },
    }
    p = tmp_path / "env.json"
    p.write_text(_json.dumps(raw))
    cfg = M.load_env_config(str(p))
    monkeypatch.setattr(M, "get_fix", lambda h, port, **k: (10.0, 20.0, 0.0, None))
    monkeypatch.setattr(M, "fetch_signal", lambda pts, spec, **k:
                        M.parse_signal({"current": {"weather_code": 95.0}}, spec))
    M.poll_once(cfg, last_good_mono=100.0, now_mono=100.0)
    rec = _json.loads(Path(cfg.auto_override_path).read_text())
    assert rec["force_full"] is True
    assert rec["reason"] == "thunderstorm ahead"


def test_poll_once_weather_code_fog_does_not_trigger(tmp_path, monkeypatch):
    # regression guard: a high non-storm code (fog=45) must not be read as a hazard
    raw = {
        "poll_interval_s": 60, "lookahead_s": 0, "min_speed_ms": 2.0, "max_stale_s": 600,
        "gpsd": {"host": "127.0.0.1", "port": 2947},
        "auto_override": {"path": str(tmp_path / "auto_override.json")},
        "signals": {
            "weather": {"enabled": True, "url": "http://fc", "current_field": "weather_code",
                        "hazard_codes": [80, 81, 82, 95, 96, 99], "on_thresh": 1.0,
                        "off_thresh": 0.0, "wet_confirm": 1, "dry_confirm": 3,
                        "reason": "thunderstorm ahead"},
        },
    }
    p = tmp_path / "env.json"
    p.write_text(_json.dumps(raw))
    cfg = M.load_env_config(str(p))
    monkeypatch.setattr(M, "get_fix", lambda h, port, **k: (10.0, 20.0, 0.0, None))
    monkeypatch.setattr(M, "fetch_signal", lambda pts, spec, **k:
                        M.parse_signal({"current": {"weather_code": 45.0}}, spec))
    M.poll_once(cfg, last_good_mono=100.0, now_mono=100.0)
    rec = _json.loads(Path(cfg.auto_override_path).read_text())
    assert rec["force_full"] is False


def test_poll_once_writes_override_on_success(tmp_path, monkeypatch):
    cfg = M.load_env_config(_make_cfg(tmp_path))
    monkeypatch.setattr(M, "get_fix", lambda h, p, **k: (10.0, 20.0, 10.0, 90.0))
    monkeypatch.setattr(M, "fetch_signal", lambda pts, spec, **k: [0.9])
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

    def flaky_fetch(points, spec, **k):
        if spec.url == "http://aq":
            raise RuntimeError("smoke endpoint down")
        return [0.9]  # precip wet
    monkeypatch.setattr(M, "fetch_signal", flaky_fetch)

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


def test_tpv_epoch_parses_zulu():
    import datetime
    e = M.tpv_epoch("2026-07-05T11:31:54.000Z")
    expect = datetime.datetime(2026, 7, 5, 11, 31, 54,
                               tzinfo=datetime.timezone.utc).timestamp()
    assert e == expect


def test_tpv_epoch_junk_is_none():
    assert M.tpv_epoch(None) is None
    assert M.tpv_epoch("not-a-time") is None


def test_build_points_accepts_five_tuple():
    pts = M.build_points((10.0, 20.0, 10.0, 90.0, 12345.0),
                         lookahead_s=300, min_speed_ms=2.0)
    assert len(pts) == 2


def test_config_forecast_and_stations_parsing(tmp_path):
    raw = {
        "poll_interval_s": 60, "lookahead_s": 300, "min_speed_ms": 2.0,
        "max_stale_s": 600,
        "gpsd": {"host": "127.0.0.1", "port": 2947, "max_fix_age_s": 45},
        "auto_override": {"path": str(tmp_path / "ao.json")},
        "stations": {"enabled": True, "path": str(tmp_path / "st.json"),
                     "radius_m": 100, "predict_n": 3},
        "signals": {"precip": {"url": "http://fc", "current_field": "precipitation",
                               "on_thresh": 2.5, "off_thresh": 1.0,
                               "forecast": {"variable": "precipitation",
                                            "window_s": 1800}}},
    }
    p = tmp_path / "env2.json"
    p.write_text(_json.dumps(raw))
    cfg = M.load_env_config(str(p))
    assert cfg.max_fix_age_s == 45.0
    assert cfg.stations["radius_m"] == 100.0
    assert cfg.stations["predict_n"] == 3
    assert cfg.stations["dwell_min_s"] == 600.0          # default filled
    spec = cfg.signals[0]
    assert spec.forecast_variable == "precipitation"
    assert spec.forecast_steps == 2                       # ceil(1800/900)
    assert spec.forecast_scale == 4.0                     # 3600/900


def test_config_without_new_keys_backcompat(tmp_path):
    cfg = M.load_env_config(_make_cfg(tmp_path))
    assert cfg.max_fix_age_s == 30.0
    assert cfg.stations is None
    assert cfg.signals[0].forecast_variable is None


def _fspec(**kw):
    ctl = M.SignalController("precip", 2.5, 1.0, 1, 2, "precip ahead")
    args = dict(controller=ctl, url="http://fc", current_field="precipitation",
                forecast_variable="precipitation", forecast_steps=2)
    args.update(kw)
    return M.SignalSpec(**args)


def test_signal_value_forecast_scales_to_hourly_rate():
    spec = _fspec()
    # 0.8 mm in a 15-min step = 3.2 mm/h equivalent > current 0.1
    assert M.signal_value_per_point(0.1, [0.0, 0.8], spec) == 3.2


def test_signal_value_current_dominates_when_larger():
    spec = _fspec()
    assert M.signal_value_per_point(5.0, [0.2], spec) == 5.0


def test_signal_value_ignores_none_steps():
    spec = _fspec()
    assert M.signal_value_per_point(0.0, [None, 0.5, None], spec) == 2.0


def test_signal_value_no_forecast_config():
    spec = _fspec(forecast_variable=None, forecast_steps=0)
    assert M.signal_value_per_point(0.7, [9.9], spec) == 0.7   # forecast ignored


def test_signal_value_categorical_codes_check_window_too():
    ctl = M.SignalController("wx", 1.0, 0.0, 1, 2, "storm")
    spec = M.SignalSpec(controller=ctl, url="u", current_field="weather_code",
                        hazard_codes={95, 96}, forecast_variable="weather_code",
                        forecast_steps=2)
    assert M.signal_value_per_point(1.0, [95], spec) == 1.0    # code in window
    assert M.signal_value_per_point(1.0, [45], spec) == 0.0    # benign everywhere


def test_parse_signal_multi_point_with_forecast():
    spec = _fspec()
    data = [
        {"current": {"precipitation": 0.0},
         "minutely_15": {"precipitation": [0.0, 0.9]}},
        {"current": {"precipitation": 3.0},
         "minutely_15": {"precipitation": [0.0, 0.0]}},
    ]
    assert M.parse_signal(data, spec) == [3.6, 3.0]


def test_parse_signal_missing_forecast_falls_back_to_current():
    spec = _fspec()
    data = {"current": {"precipitation": 1.5}}
    assert M.parse_signal(data, spec) == [1.5]


import station_tracker as ST


def _tracker_with_two_stations():
    t = ST.StationTracker(radius_m=150.0, dwell_speed_ms=1.0, dwell_min_s=60.0,
                          hold_s=900.0, max_stations=16, predict_n=2)
    now = 1000.0
    for i in range(3):
        t.update((35.0, -97.0, 0.0), now + i * 40.0)     # station A
    t.update((35.05, -97.0, 15.0), now + 200.0)          # drive
    for i in range(3):
        t.update((35.1, -97.0, 0.0), now + 300.0 + i * 40.0)  # station B (A->B)
    return t


def test_assemble_points_parked_snaps_and_predicts(tmp_path):
    cfg = M.load_env_config(_make_cfg(tmp_path))
    t = _tracker_with_two_stations()
    # drive back to A and dwell so current station = A, prediction = B
    t.update((35.05, -97.0, 15.0), 2000.0)
    for i in range(3):
        t.update((35.0, -97.0, 0.0), 2100.0 + i * 40.0)
    fix = (35.0002, -97.0002, 0.0, None, 2200.0)
    pts = M.assemble_points(cfg, t, fix, True, 2200.0)
    assert len(pts) == 2                                  # snapped A + predicted B
    assert abs(pts[0][0] - 35.0) < 0.01
    assert abs(pts[1][0] - 35.1) < 0.001


def test_assemble_points_no_fix_uses_held_station(tmp_path):
    cfg = M.load_env_config(_make_cfg(tmp_path))
    t = _tracker_with_two_stations()
    pts = M.assemble_points(cfg, t, None, False, 1500.0)  # fix lost right after B
    assert len(pts) >= 1
    assert abs(pts[0][0] - 35.1) < 0.001                  # held at B


def test_assemble_points_without_tracker_matches_old_behavior(tmp_path):
    cfg = M.load_env_config(_make_cfg(tmp_path))
    fix = (10.0, 20.0, 10.0, 90.0, 111.0)
    pts = M.assemble_points(cfg, None, fix, True, 111.0)
    assert len(pts) == 2                                  # current + projection


def test_dedup_points_merges_near_duplicates():
    pts = M.dedup_points([(35.0, -97.0), (35.0001, -97.0001), (35.1, -97.0)])
    assert len(pts) == 2


def test_poll_once_rejects_stale_fix(tmp_path, monkeypatch):
    cfg = M.load_env_config(_make_cfg(tmp_path))
    calls = []
    monkeypatch.setattr(M, "get_fix",
                        lambda h, p, **k: (10.0, 20.0, 0.0, None, 100.0))  # ancient
    monkeypatch.setattr(M, "fetch_signal",
                        lambda pts, spec, **k: calls.append(pts) or [0.0])
    M.poll_once(cfg, last_good_mono=100.0, now_mono=100.0)
    assert calls == []                                    # no points -> no fetch


def test_poll_once_fresh_fix_still_evaluates(tmp_path, monkeypatch):
    import time as _time
    cfg = M.load_env_config(_make_cfg(tmp_path))
    monkeypatch.setattr(M, "get_fix",
                        lambda h, p, **k: (10.0, 20.0, 0.0, None, _time.time()))
    monkeypatch.setattr(M, "fetch_signal", lambda pts, spec, **k: [0.2])
    out = M.poll_once(cfg, last_good_mono=100.0, now_mono=200.0)
    assert out == 200.0                                   # evaluated -> last_good updated


def test_build_points_record_shape():
    rec = M.build_points_record(
        points=[(35.0, -97.0), (35.1, -97.0)],
        per_signal_vals={"precip": [0.4, 3.2], "smoke": [1.0, 2.0]},
        force_full=True, reason="precip ahead", ts=123.0)
    assert rec["ts"] == 123.0 and rec["force_full"] is True
    assert rec["reason"] == "precip ahead"
    assert rec["points"] == [
        {"lat": 35.0, "lon": -97.0, "values": {"precip": 0.4, "smoke": 1.0}},
        {"lat": 35.1, "lon": -97.0, "values": {"precip": 3.2, "smoke": 2.0}},
    ]


def test_build_points_record_tolerates_short_value_lists():
    rec = M.build_points_record([(1.0, 2.0), (3.0, 4.0)], {"precip": [0.5]},
                                False, "", 1.0)
    assert rec["points"][1]["values"] == {}


def test_poll_once_publishes_points_file(tmp_path, monkeypatch):
    import time as _time
    cfg = M.load_env_config(_make_cfg(tmp_path))
    cfg.points_path = str(tmp_path / "pts.json")
    monkeypatch.setattr(M, "get_fix",
                        lambda h, p, **k: (10.0, 20.0, 0.0, None, _time.time()))
    monkeypatch.setattr(M, "fetch_signal", lambda pts, spec, **k: [0.9])
    M.poll_once(cfg, last_good_mono=100.0, now_mono=200.0)
    rec = _json.loads(Path(cfg.points_path).read_text())
    assert rec["points"][0]["lat"] == 10.0
    assert rec["points"][0]["values"]["precip"] == 0.9


def _closed_port():
    """A port nothing is listening on: bind it, read it back, close it."""
    import socket as _socket
    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_get_fix_warns_on_a_failed_connect_by_default(caplog):
    with caplog.at_level("WARNING", logger="environ_ctl"):
        assert M.get_fix("127.0.0.1", _closed_port(), timeout=0.2) is None
    assert any("gpsd connect failed" in r.message for r in caplog.records)


def test_get_fix_quiet_returns_none_without_logging(caplog):
    """location_fec polls at 1 Hz, not at environ_ctl's minute cadence: one
    warning per failed connect is one per second for as long as gpsd is down.
    The quiet caller reports the outage once itself."""
    with caplog.at_level("DEBUG", logger="environ_ctl"):
        assert M.get_fix("127.0.0.1", _closed_port(), timeout=0.2,
                         quiet=True) is None
    assert caplog.records == []
