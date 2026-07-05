import json as _json
from pathlib import Path

import sbfd_ctl as M


def test_resolve_map_cfg_defaults():
    m = M.resolve_map_cfg(None)
    assert m["stations_path"] == "/var/lib/sbfd-ctl/stations.json"
    assert m["labels_path"] == "/var/lib/sbfd-ctl/station_labels.json"
    assert m["gpsd"] == {"host": "127.0.0.1", "port": 2947}
    assert m["tile_cache"]["max_mb"] == 512
    assert m["tile_cache"]["max_zoom"] == 17


def test_resolve_map_cfg_overrides_merge():
    m = M.resolve_map_cfg({"gpsd": {"host": "192.0.2.9"},
                           "tile_cache": {"max_mb": 64}})
    assert m["gpsd"] == {"host": "192.0.2.9", "port": 2947}
    assert m["tile_cache"]["max_mb"] == 64
    assert m["tile_cache"]["max_zoom"] == 17


def test_validate_label_happy_path():
    ok, sid, label, err = M.validate_label({"id": "s3", "label": "Depot 7"})
    assert ok and sid == "s3" and label == "Depot 7"


def test_validate_label_strips_control_and_caps_length():
    ok, sid, label, _ = M.validate_label({"id": "s1", "label": "a\x07b" + "x" * 100})
    assert ok and label.startswith("ab") and len(label) <= 48


def test_validate_label_rejects_bad_id():
    for bad in ("../etc", "s", "x1", 5, None):
        ok, *_rest, err = M.validate_label({"id": bad, "label": "hi"})
        assert not ok and err


def test_validate_label_empty_means_delete():
    ok, sid, label, _ = M.validate_label({"id": "s2", "label": ""})
    assert ok and label == ""


def test_predict_from_stations_matches_tracker_rule():
    data = {"stations": {"s1": {"lat": 1, "lon": 1, "last_visit": 10},
                         "s2": {"lat": 2, "lon": 2, "last_visit": 30},
                         "s3": {"lat": 3, "lon": 3, "last_visit": 20}},
            "transitions": {"s1": {"s2": 3, "s3": 3}},
            "last_station": "s1"}
    # tie on count -> later last_visit wins
    assert M.predict_from_stations(data) == ["s2", "s3"]
    assert M.predict_from_stations({"stations": {}, "transitions": {},
                                    "last_station": None}) == []


def test_tile_route_regex_accepts_only_ints():
    assert M._TILE_RE.match("/tiles/12/931/1622.png")
    assert not M._TILE_RE.match("/tiles/12/../1622.png")
    assert not M._TILE_RE.match("/tiles/12/931/1622.jpg")
    assert not M._TILE_RE.match("/tiles/123/1/1.png")      # z > 2 digits


def test_tile_valid_bounds():
    assert M.tile_valid(3, 7, 7, max_zoom=17)
    assert not M.tile_valid(3, 8, 0, max_zoom=17)          # x >= 2**z
    assert not M.tile_valid(18, 0, 0, max_zoom=17)         # beyond max zoom


def test_tile_cache_roundtrip_and_eviction(tmp_path):
    import os
    cache = str(tmp_path / "tiles")
    blob = b"x" * 200_000                                  # ~0.2 MB per tile
    for i in range(8):
        M.store_tile(cache, 10, i, 1, blob, max_mb=1)      # 1 MB budget
        # rapid writes tie on the kernel's coarse file-timestamp clock;
        # pin explicit mtimes so LRU order is deterministic under test
        os.utime(M.tile_cache_file(cache, 10, i, 1), ns=(i * 1000, i * 1000))
    p = M.tile_cache_file(cache, 10, 7, 1)
    assert p.exists() and p.read_bytes() == blob
    M.evict_tiles(cache, max_mb=1)
    total = sum(f.stat().st_size for f in Path(cache).rglob("*.png"))
    assert total <= 1024 * 1024
    assert M.tile_cache_file(cache, 10, 7, 1).exists()     # newest survives


def test_evict_tiles_noop_under_budget(tmp_path):
    cache = str(tmp_path / "tiles")
    M.store_tile(cache, 5, 1, 1, b"tiny", max_mb=512)
    assert M.evict_tiles(cache, max_mb=512) == 0


def _mcfg(tmp_path, **over):
    m = M.resolve_map_cfg({
        "stations_path": str(tmp_path / "stations.json"),
        "labels_path": str(tmp_path / "labels.json"),
        "environ_points_path": str(tmp_path / "points.json"),
    })
    m.update(over)
    return m


def test_assemble_map_payload_all_sources(tmp_path):
    m = _mcfg(tmp_path)
    Path(m["stations_path"]).write_text(_json.dumps({
        "stations": {"s1": {"lat": 35.0, "lon": -97.0, "n_fixes": 4,
                             "visits": 2, "last_visit": 50.0},
                     "s2": {"lat": 35.1, "lon": -97.0, "n_fixes": 1,
                             "visits": 1, "last_visit": 60.0}},
        "transitions": {"s1": {"s2": 2}}, "last_station": "s1"}))
    Path(m["labels_path"]).write_text(_json.dumps({"s1": "Depot"}))
    Path(m["environ_points_path"]).write_text(_json.dumps(
        {"ts": 9.0, "force_full": True, "reason": "precip ahead",
         "points": [{"lat": 35.0, "lon": -97.0, "values": {"precip": 3.0}}]}))
    st = tmp_path / "state.json"
    st.write_text(_json.dumps({"mode": "master_backup", "active_wans": ["wan2"]}))
    fix = (35.05, -97.01, 4.2, 90.0, 100.0)
    out = M.assemble_map_payload(m, str(st), fix, now=101.5)
    assert out["fix"] == {"lat": 35.05, "lon": -97.01, "speed": 4.2,
                          "track": 90.0, "age_s": 1.5}
    s1 = [s for s in out["stations"] if s["id"] == "s1"][0]
    assert s1["label"] == "Depot"
    s2 = [s for s in out["stations"] if s["id"] == "s2"][0]
    assert s2["label"] is None
    assert out["predictions"] == ["s2"]
    assert out["environ"]["force_full"] is True
    assert out["mode"] == "master_backup" and out["active"] == ["wan2"]


def test_assemble_map_payload_degrades_when_everything_missing(tmp_path):
    m = _mcfg(tmp_path)
    out = M.assemble_map_payload(m, str(tmp_path / "absent.json"), None, now=1.0)
    assert out["fix"] is None and out["stations"] == []
    assert out["predictions"] == [] and out["environ"] is None
    assert out["mode"] is None and out["active"] is None


def test_apply_station_label_set_and_delete(tmp_path):
    lp = str(tmp_path / "labels.json")
    out = M.apply_station_label(lp, "s1", "Depot")
    assert out == {"s1": "Depot"}
    out = M.apply_station_label(lp, "s2", "Yard")
    assert out == {"s1": "Depot", "s2": "Yard"}
    out = M.apply_station_label(lp, "s1", "")
    assert out == {"s2": "Yard"}
    assert _json.loads(Path(lp).read_text()) == {"s2": "Yard"}
