import json as _json
import logging
import socket
from pathlib import Path

import sbfd_ctl as M
import tile_store as T


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


def test_map_location_layer_reads_tiles_and_zones(tmp_path):
    tile = T.encode(41.1, -73.5, 7)
    store = tmp_path / "store.json"
    store.write_text(_json.dumps({"version": 1, "tiles": {
        tile: {"wan1": {"passes": 4, "ewma_loss": 6.0, "last_seen": 1000.0}}},
        "residual": {tile: {"ewma": 1.5, "last_seen": 1000.0}}}))
    zones = tmp_path / "location-fec.json"
    zones.write_text(_json.dumps({"zones": [
        {"label": "yard", "lat": 41.1, "lon": -73.5, "radius_m": 300, "level": 2}]}))
    out = M.map_location_layer(str(store), str(zones), (41.1, -73.5), max_tiles=100)
    assert out["tiles"][0]["id"] == tile
    assert out["tiles"][0]["bbox"] == list(T.bbox(tile))
    assert out["tiles"][0]["wans"]["wan1"]["passes"] == 4
    assert out["tiles"][0]["residual"] == 1.5
    assert out["zones"][0]["label"] == "yard"


def test_map_location_layer_keeps_the_nearest_tiles(tmp_path):
    near, far = T.encode(41.1, -73.5, 7), T.encode(42.0, -73.5, 7)
    store = tmp_path / "store.json"
    store.write_text(_json.dumps({"version": 1, "tiles": {
        far: {"wan1": {"passes": 3, "ewma_loss": 6.0, "last_seen": 1.0}},
        near: {"wan1": {"passes": 3, "ewma_loss": 6.0, "last_seen": 1.0}}}}))
    out = M.map_location_layer(str(store), str(tmp_path / "absent.json"),
                               (41.1, -73.5), max_tiles=1)
    assert [t["id"] for t in out["tiles"]] == [near]


def test_map_location_layer_degrades_to_empty(tmp_path):
    (tmp_path / "store.json").write_text("{junk")
    out = M.map_location_layer(str(tmp_path / "store.json"),
                               str(tmp_path / "absent.json"), None, max_tiles=10)
    assert out == {"tiles": [], "zones": []}


def test_assemble_map_payload_carries_location_fec(tmp_path):
    m = M.resolve_map_cfg({"stations_path": str(tmp_path / "s.json"),
                           "labels_path": str(tmp_path / "l.json"),
                           "environ_points_path": str(tmp_path / "e.json"),
                           "location_store_path": str(tmp_path / "store.json"),
                           "location_config_path": str(tmp_path / "lf.json")})
    out = M.assemble_map_payload(m, str(tmp_path / "pub.json"), None, 1000.0)
    assert out["location_fec"] == {"tiles": [], "zones": []}


def test_map_location_layer_tolerates_a_malformed_residual(tmp_path):
    tile = T.encode(41.1, -73.5, 7)
    wan = {"wan1": {"passes": 4, "ewma_loss": 6.0, "last_seen": 1000.0}}
    store = tmp_path / "store.json"

    store.write_text(_json.dumps({"tiles": {tile: wan}, "residual": "garbage"}))
    out = M.map_location_layer(str(store), str(tmp_path / "absent.json"), None, max_tiles=10)
    assert out["tiles"][0]["id"] == tile
    assert out["tiles"][0]["residual"] is None

    store.write_text(_json.dumps({"tiles": {tile: wan}, "residual": {tile: 5}}))
    out = M.map_location_layer(str(store), str(tmp_path / "absent.json"), None, max_tiles=10)
    assert out["tiles"][0]["residual"] is None

    store.write_text(_json.dumps({"tiles": {tile: wan}, "residual": {tile: {"ewma": "x"}}}))
    out = M.map_location_layer(str(store), str(tmp_path / "absent.json"), None, max_tiles=10)
    assert out["tiles"][0]["residual"] is None

    # bool is a subclass of int: without the explicit bool guard `true` would
    # sail through the numeric check and reach the map as a residual of 1.
    store.write_text(_json.dumps({"tiles": {tile: wan}, "residual": {tile: {"ewma": True}}}))
    out = M.map_location_layer(str(store), str(tmp_path / "absent.json"), None, max_tiles=10)
    assert out["tiles"][0]["residual"] is None


def test_map_location_layer_tolerates_a_non_dict_tiles(tmp_path):
    store = tmp_path / "store.json"
    store.write_text(_json.dumps({"tiles": "nope"}))
    out = M.map_location_layer(str(store), str(tmp_path / "absent.json"), None, max_tiles=10)
    assert out == {"tiles": [], "zones": []}


def test_map_location_layer_skips_a_tile_id_that_is_not_a_geohash(tmp_path):
    good = T.encode(41.1, -73.5, 7)
    wan = {"wan1": {"passes": 4, "ewma_loss": 6.0, "last_seen": 1000.0}}
    store = tmp_path / "store.json"
    store.write_text(_json.dumps({"tiles": {"not a geohash!": wan, good: wan}}))
    out = M.map_location_layer(str(store), str(tmp_path / "absent.json"), None, max_tiles=10)
    assert [t["id"] for t in out["tiles"]] == [good]


def test_map_location_layer_tolerates_non_list_zones_and_a_zone_missing_a_key(tmp_path):
    store = tmp_path / "absent.json"
    zones = tmp_path / "zones.json"

    zones.write_text(_json.dumps({"zones": "nope"}))
    out = M.map_location_layer(str(store), str(zones), None, max_tiles=10)
    assert out["zones"] == []

    zones.write_text(_json.dumps({"zones": [
        {"label": "x"},
        {"label": "ok", "lat": 41.1, "lon": -73.5, "radius_m": 10, "level": 1}]}))
    out = M.map_location_layer(str(store), str(zones), None, max_tiles=10)
    assert len(out["zones"]) == 1
    assert out["zones"][0]["label"] == "ok"


def test_map_location_layer_drops_a_per_wan_entry_with_a_bad_type(tmp_path):
    """The map page does `(v.ewma_loss || 0).toFixed(1)`, which THROWS on a
    string -- and drawLocation runs inside the same tick that moves the vehicle
    marker. The server must not hand the page a value of a type it does not
    validate; TileStore.from_dict is the validator that already exists."""
    tile = T.encode(41.1, -73.5, 7)
    store = tmp_path / "store.json"

    store.write_text(_json.dumps({"tiles": {tile: {
        "wan1": {"passes": 3, "ewma_loss": "bad", "last_seen": 1.0}}}}))
    out = M.map_location_layer(str(store), str(tmp_path / "absent.json"), None, max_tiles=10)
    assert out["tiles"] == []           # the only WAN was junk: no tile at all

    store.write_text(_json.dumps({"tiles": {tile: {
        "wan1": {"passes": 3, "ewma_loss": "bad", "last_seen": 1.0},
        "wan2": {"passes": 4, "ewma_loss": 6.0, "last_seen": 1.0}}}}))
    out = M.map_location_layer(str(store), str(tmp_path / "absent.json"), None, max_tiles=10)
    assert list(out["tiles"][0]["wans"]) == ["wan2"]
    assert out["tiles"][0]["wans"]["wan2"]["ewma_loss"] == 6.0


def test_map_location_layer_emits_zone_wans_only_as_a_list_of_names(tmp_path):
    """`z.wans.join(", ")` throws on a string, and the config is hand-edited."""
    store = tmp_path / "absent.json"
    zones = tmp_path / "zones.json"
    zones.write_text(_json.dumps({"zones": [
        {"label": "a", "lat": 41.1, "lon": -73.5, "radius_m": 10, "level": 1,
         "wans": "wan1"},
        {"label": "b", "lat": 41.1, "lon": -73.5, "radius_m": 10, "level": 1,
         "wans": ["wan1", 7]},
        {"label": "c", "lat": 41.1, "lon": -73.5, "radius_m": 10, "level": 1,
         "wans": ["wan1", "wan2"]}]}))
    out = M.map_location_layer(str(store), str(zones), None, max_tiles=10)
    assert [z["wans"] for z in out["zones"]] == [None, None, ["wan1", "wan2"]]


def test_assemble_map_payload_tolerates_a_bad_max_location_tiles(tmp_path):
    m = M.resolve_map_cfg({"stations_path": str(tmp_path / "s.json"),
                           "labels_path": str(tmp_path / "l.json"),
                           "environ_points_path": str(tmp_path / "e.json"),
                           "location_store_path": str(tmp_path / "store.json"),
                           "location_config_path": str(tmp_path / "lf.json"),
                           "max_location_tiles": "lots"})
    out = M.assemble_map_payload(m, str(tmp_path / "pub.json"), None, 1000.0)
    assert out["location_fec"] == {"tiles": [], "zones": []}


def test_map_location_layer_drops_a_non_finite_residual(tmp_path):
    # NaN and inf are floats, so they pass the numeric guard, and json.dumps
    # writes them as bare NaN/Infinity tokens: JSON.parse throws and the page
    # loses the WHOLE payload, not just one tile's residual.
    tile = T.encode(41.1, -73.5, 7)
    wan = {"wan1": {"passes": 4, "ewma_loss": 6.0, "last_seen": 1000.0}}
    store = tmp_path / "store.json"
    for bad in ("NaN", "Infinity", "-Infinity"):
        store.write_text(
            '{"tiles": %s, "residual": {"%s": {"ewma": %s, "last_seen": 1000.0}}}'
            % (_json.dumps({tile: wan}), tile, bad))
        out = M.map_location_layer(str(store), str(tmp_path / "absent.json"),
                                   None, max_tiles=10)
        assert out["tiles"][0]["residual"] is None
        _json.dumps(out, allow_nan=False)      # what the browser must parse


def test_map_payload_stays_parseable_with_a_non_finite_loss_in_the_store(tmp_path):
    # _send_json's json.dumps would emit a bare NaN token for this, and the
    # browser's JSON.parse rejects the whole body — not just the bad tile.
    tile = T.encode(41.1, -73.5, 7)
    store = tmp_path / "store.json"
    store.write_text('{"tiles": {"%s": {"wan1": {"passes": 4, '
                     '"ewma_loss": NaN, "last_seen": 1000.0}}}}' % tile)
    m = M.resolve_map_cfg({"stations_path": str(tmp_path / "s.json"),
                           "labels_path": str(tmp_path / "l.json"),
                           "environ_points_path": str(tmp_path / "e.json"),
                           "location_store_path": str(store),
                           "location_config_path": str(tmp_path / "lf.json")})
    out = M.assemble_map_payload(m, str(tmp_path / "pub.json"), None, 1000.0)
    _json.dumps(out, allow_nan=False)
    assert out["location_fec"]["tiles"] == []


def test_assemble_map_payload_clamps_a_negative_max_location_tiles(tmp_path):
    # rows[:-5] is a NEGATIVE slice: it drops the five NEAREST tiles and keeps
    # the rest, which is the exact opposite of a cap.
    entry = {"wan1": {"passes": 4, "ewma_loss": 6.0, "last_seen": 1000.0}}
    tiles = {T.encode(41.1 + 0.01 * i, -73.5, 7): entry for i in range(8)}
    store = tmp_path / "store.json"
    store.write_text(_json.dumps({"tiles": tiles}))
    m = M.resolve_map_cfg({"stations_path": str(tmp_path / "s.json"),
                           "labels_path": str(tmp_path / "l.json"),
                           "environ_points_path": str(tmp_path / "e.json"),
                           "location_store_path": str(store),
                           "location_config_path": str(tmp_path / "lf.json"),
                           "max_location_tiles": -5})
    out = M.assemble_map_payload(m, str(tmp_path / "pub.json"), None, 1000.0)
    assert out["location_fec"]["tiles"] == []


def test_get_map_fix_does_not_warn_when_gpsd_is_down(caplog):
    """The map polls every 3 s; environ_ctl.get_fix warns on every failed
    connect. A dead gpsd plus one open map page would fill the journal."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                      # nothing is listening there now
    M._GPS_MEMO["ts"] = 0.0        # defeat the 2 s memo
    M._GPS_MEMO["fix"] = None
    with caplog.at_level(logging.DEBUG):
        assert M.get_map_fix("127.0.0.1", port) is None
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
