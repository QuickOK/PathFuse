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
