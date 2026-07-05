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
