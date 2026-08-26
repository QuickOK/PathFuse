import json as _json
import logging
import os
import socket
import threading
from pathlib import Path

import pytest
import fec_control as FC
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


def test_apply_station_label_survives_concurrent_writers(tmp_path):
    """station_labels.json is a read-modify-write on a threaded UI server,
    exactly like the zone file, and it was neither locked nor written through
    the unique-tmp helper.

    Two effects, and the quiet one is the dangerous one. Unlocked, the later
    read wins and labels are simply lost. Worse, every writer shares one
    `<name>.tmp` inode: B is still writing into it when A's replace moves it
    to the live path, so B's remaining bytes land in the LIVE file -- and a
    torn station_labels.json reads back as no labels at all, blanking every
    named station on the map at once."""
    lp = str(tmp_path / "station_labels.json")
    writers, per_writer = 8, 50
    failures, torn = [], []
    done = threading.Event()

    def writer(n):
        for i in range(per_writer):
            try:
                M.apply_station_label(lp, "s%d-%d" % (n, i), "L%d-%d" % (n, i))
            except Exception as e:                       # noqa: BLE001
                failures.append(repr(e))

    def reader():
        # The live path must never be observed as anything but a complete
        # file: that is what a temp file plus os.replace buys.
        while not done.is_set():
            try:
                text = Path(lp).read_text()
            except FileNotFoundError:
                continue
            except OSError as e:
                torn.append(repr(e))
                continue
            try:
                _json.loads(text)
            except ValueError as e:
                torn.append("%s: %r" % (e, text[:80]))

    watcher = threading.Thread(target=reader, daemon=True)
    watcher.start()
    threads = [threading.Thread(target=writer, args=(n,))
               for n in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    done.set()
    watcher.join(timeout=5)

    assert failures == []
    assert torn == []
    labels = _json.loads(Path(lp).read_text())
    assert len(labels) == writers * per_writer
    assert labels["s0-0"] == "L0-0"
    assert labels["s7-49"] == "L7-49"


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
    # The level keys ride along even with no FEC config to describe; the page
    # reads them unconditionally.
    assert out["location_fec"]["tiles"] == []
    assert out["location_fec"]["zones"] == []


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
    assert out["location_fec"]["tiles"] == []
    assert out["location_fec"]["zones"] == []


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


def _one_tile_store(path, tile, ewma_loss=6.0):
    path.write_text(_json.dumps({"version": 1, "tiles": {
        tile: {"wan1": {"passes": 4, "ewma_loss": ewma_loss,
                        "last_seen": 1000.0}}}}))


def _count_from_dict(monkeypatch):
    """Wrap TileStore.from_dict with a call counter."""
    calls = []
    real = T.TileStore.from_dict

    def counting(raw, **kw):
        calls.append(1)
        return real(raw, **kw)

    monkeypatch.setattr(T.TileStore, "from_dict", staticmethod(counting))
    M._STORE_MEMO.update({"path": None, "stat": None, "store": None})
    return calls


def test_map_location_layer_parses_the_store_once_per_change(tmp_path, monkeypatch):
    """/api/map polls every 3s; re-parsing an unchanged store on every poll is
    wasted work and (for a malformed store) a warning per poll."""
    calls = _count_from_dict(monkeypatch)
    store = tmp_path / "store.json"
    tile = T.encode(41.1, -73.5, 7)
    _one_tile_store(store, tile)
    absent = str(tmp_path / "absent.json")

    first = M.map_location_layer(str(store), absent, None, max_tiles=10)
    second = M.map_location_layer(str(store), absent, None, max_tiles=10)
    assert first["tiles"][0]["id"] == tile
    assert second["tiles"][0]["id"] == tile
    assert len(calls) == 1

    # A rewrite must be picked up. Force a distinct mtime_ns so the test does
    # not depend on the filesystem's clock resolution.
    _one_tile_store(store, tile, ewma_loss=9.0)
    st = os.stat(str(store))
    os.utime(str(store), ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    third = M.map_location_layer(str(store), absent, None, max_tiles=10)
    assert len(calls) == 2
    assert third["tiles"][0]["wans"]["wan1"]["ewma_loss"] == 9.0


def test_map_location_layer_survives_the_store_vanishing(tmp_path, monkeypatch):
    _count_from_dict(monkeypatch)
    store = tmp_path / "store.json"
    _one_tile_store(store, T.encode(41.1, -73.5, 7))
    absent = str(tmp_path / "absent.json")
    assert M.map_location_layer(str(store), absent, None, max_tiles=10)["tiles"]
    store.unlink()
    assert M.map_location_layer(str(store), absent, None,
                                max_tiles=10) == {"tiles": [], "zones": []}


def test_map_location_layer_warns_once_for_a_malformed_store(tmp_path, caplog):
    M._STORE_MEMO.update({"path": None, "stat": None, "store": None})
    store = tmp_path / "store.json"
    tile = T.encode(41.1, -73.5, 7)
    store.write_text(_json.dumps({"version": 1, "tiles": {
        tile: {"wan1": {"passes": "lots", "ewma_loss": 6.0,
                        "last_seen": 1000.0}}}}))
    absent = str(tmp_path / "absent.json")
    with caplog.at_level(logging.WARNING, logger="tile_store"):
        for _ in range(3):
            M.map_location_layer(str(store), absent, None, max_tiles=10)
    warns = [r for r in caplog.records
             if r.name == "tile_store" and "malformed" in r.getMessage()]
    assert len(warns) == 1


def test_map_location_layer_survives_a_nul_in_the_store_path(tmp_path):
    """os.stat raises ValueError - not OSError - on an embedded NUL, and the
    endpoint must degrade rather than 500 on any broken source."""
    M._STORE_MEMO.update({"path": None, "stat": None, "store": None})
    bad = str(tmp_path / "sto\x00re.json")
    out = M.map_location_layer(bad, str(tmp_path / "absent.json"), None,
                               max_tiles=10)
    assert out == {"tiles": [], "zones": []}


# -- operator zone editing -----------------------------------------------------

_WANS = {"wan1", "wan2"}


def _zone_payload(**over):
    p = {"lat": 41.1, "lon": -73.5, "radius_m": 300, "level": 3,
         "label": "dock"}
    p.update(over)
    return p


def test_map_defaults_name_the_operator_zone_file():
    m = M.resolve_map_cfg(None)
    assert m["location_zones_path"] == "/var/lib/sbfd-ctl/location_zones.json"


def test_validate_zone_payload_accepts_a_new_zone():
    ok, zone, err = M.validate_zone_payload(_zone_payload(), _WANS, 5)
    assert ok and err is None
    assert zone == {"label": "dock", "lat": 41.1, "lon": -73.5,
                    "radius_m": 300.0, "level": 3, "wans": None,
                    "suppress_learned": False}
    assert "id" not in zone                    # no id means create


def test_validate_zone_payload_keeps_an_id_for_an_update():
    ok, zone, _ = M.validate_zone_payload(_zone_payload(id="z7"), _WANS, 5)
    assert ok and zone["id"] == "z7"


def test_validate_zone_payload_accepts_a_delete():
    ok, zone, err = M.validate_zone_payload({"id": "z2", "delete": True},
                                            _WANS, 5)
    assert ok and err is None
    assert zone == {"id": "z2", "delete": True}


def test_validate_zone_payload_rejects_a_delete_without_a_usable_id():
    for bad in (None, "", "zone1", "z", "../etc", 3, True):
        ok, _z, err = M.validate_zone_payload({"id": bad, "delete": True},
                                              _WANS, 5)
        assert not ok and err


def test_validate_zone_payload_rejects_a_bad_id_format_on_an_update():
    ok, _z, err = M.validate_zone_payload(_zone_payload(id="s1"), _WANS, 5)
    assert not ok and "id" in err


def test_validate_zone_payload_rejects_a_non_object():
    for bad in ([], "zone", 4, None):
        ok, _z, err = M.validate_zone_payload(bad, _WANS, 5)
        assert not ok and "object" in err


def test_validate_zone_payload_rejects_coordinates_out_of_range():
    for over in ({"lat": 91.0}, {"lat": -90.5}, {"lon": 180.5},
                 {"lon": -181.0}):
        ok, _z, err = M.validate_zone_payload(_zone_payload(**over), _WANS, 5)
        assert not ok and err


def test_validate_zone_payload_rejects_a_non_finite_coordinate():
    """json.loads accepts the barewords NaN and Infinity, and this file has
    been bitten twice by a non-finite number reaching something that only
    range-checks. NaN fails every comparison, so the bounds test alone lets it
    straight through."""
    for text in ('{"lat": NaN, "lon": -73.5, "radius_m": 300, "level": 3}',
                 '{"lat": 41.1, "lon": Infinity, "radius_m": 300, "level": 3}',
                 '{"lat": 41.1, "lon": -73.5, "radius_m": NaN, "level": 3}'):
        ok, _z, err = M.validate_zone_payload(_json.loads(text), _WANS, 5)
        assert not ok and err


def test_validate_zone_payload_rejects_a_radius_outside_its_bounds():
    for radius in (0, -10, 50001, "300", None, True):
        ok, _z, err = M.validate_zone_payload(
            _zone_payload(radius_m=radius), _WANS, 5)
        assert not ok and "radius" in err


def test_validate_zone_payload_rejects_a_level_past_the_table():
    ok, _z, err = M.validate_zone_payload(_zone_payload(level=5), _WANS, 5)
    assert not ok and "level" in err
    ok, _z, err = M.validate_zone_payload(_zone_payload(level=-1), _WANS, 5)
    assert not ok and "level" in err
    # The shorter cellular table means the same level is out of range there.
    ok, _z, _err = M.validate_zone_payload(_zone_payload(level=3), _WANS, 5)
    assert ok
    ok, _z, err = M.validate_zone_payload(_zone_payload(level=3), _WANS, 3)
    assert not ok and "level" in err


def test_validate_zone_payload_rejects_a_boolean_level():
    # bool is a subclass of int: int(True) is 1, so an unguarded check would
    # turn `"level": true` into a real floor of level 1.
    ok, _z, err = M.validate_zone_payload(_zone_payload(level=True), _WANS, 5)
    assert not ok and "level" in err


def test_validate_zone_payload_rejects_an_unknown_wan_by_name():
    ok, _z, err = M.validate_zone_payload(
        _zone_payload(wans=["wan1", "wan9"]), _WANS, 5)
    assert not ok and "wan9" in err
    ok, _z, err = M.validate_zone_payload(_zone_payload(wans="wan1"), _WANS, 5)
    assert not ok and "wans" in err


def test_validate_zone_payload_treats_an_absent_wans_as_all_wans():
    for payload in (_zone_payload(), _zone_payload(wans=None),
                    _zone_payload(wans=[])):
        ok, zone, _ = M.validate_zone_payload(payload, _WANS, 5)
        assert ok and zone["wans"] is None


def test_validate_zone_payload_rejects_a_non_bool_suppress_learned():
    for bad in (1, "true", None, []):
        ok, _z, err = M.validate_zone_payload(
            _zone_payload(suppress_learned=bad), _WANS, 5)
        assert not ok and "suppress_learned" in err
    ok, zone, _ = M.validate_zone_payload(
        _zone_payload(suppress_learned=True), _WANS, 5)
    assert ok and zone["suppress_learned"] is True


def test_validate_zone_payload_sanitises_the_label():
    ok, zone, _ = M.validate_zone_payload(
        _zone_payload(label="a\x07b" + "x" * 100), _WANS, 5)
    assert ok and zone["label"].startswith("ab") and len(zone["label"]) == 48
    # An empty (or all-control-character) label still has to name something:
    # the resolver publishes it as the reason a floor was raised.
    for empty in ("", "   ", "\x07\x00"):
        ok, zone, _ = M.validate_zone_payload(
            _zone_payload(label=empty), _WANS, 5)
        assert ok and zone["label"] == "zone"
    ok, _z, err = M.validate_zone_payload(_zone_payload(label=7), _WANS, 5)
    assert not ok and "label" in err


def test_validated_zone_survives_the_daemons_own_validator():
    """The endpoint and location_fec must agree about what a zone is, or the
    map could write a zone the daemon then silently drops."""
    import location_fec as LF
    ok, zone, _ = M.validate_zone_payload(
        _zone_payload(wans=["wan1"], suppress_learned=True), _WANS, 5)
    assert ok
    assert LF.validate_zone(zone, 5) is not None


def test_apply_location_zone_assigns_ids_in_sequence(tmp_path):
    p = str(tmp_path / "location_zones.json")
    ok, zone, _ = M.validate_zone_payload(_zone_payload(), _WANS, 5)
    zones = M.apply_location_zone(p, zone)
    assert [z["id"] for z in zones] == ["z1"]
    ok, zone, _ = M.validate_zone_payload(_zone_payload(label="yard"),
                                          _WANS, 5)
    zones = M.apply_location_zone(p, zone)
    assert [z["id"] for z in zones] == ["z1", "z2"]
    assert [z["label"] for z in zones] == ["dock", "yard"]
    assert _json.loads(Path(p).read_text())["zones"] == zones


def test_apply_location_zone_numbers_a_new_id_above_every_existing_one(tmp_path):
    """max+1, not count+1: with z1 deleted, the next zone must not be handed
    z2 while a live z2 is still in the file."""
    p = str(tmp_path / "location_zones.json")
    for label in ("a", "b"):
        _ok, z, _ = M.validate_zone_payload(_zone_payload(label=label),
                                            _WANS, 5)
        M.apply_location_zone(p, z)
    M.apply_location_zone(p, {"id": "z1", "delete": True})
    _ok, z, _ = M.validate_zone_payload(_zone_payload(label="c"), _WANS, 5)
    zones = M.apply_location_zone(p, z)
    assert [z["id"] for z in zones] == ["z2", "z3"]


def test_apply_location_zone_updates_in_place_and_keeps_the_id(tmp_path):
    p = str(tmp_path / "location_zones.json")
    for label in ("a", "b"):
        _ok, z, _ = M.validate_zone_payload(_zone_payload(label=label),
                                            _WANS, 5)
        M.apply_location_zone(p, z)
    _ok, z, _ = M.validate_zone_payload(
        _zone_payload(id="z1", label="renamed", level=4), _WANS, 5)
    zones = M.apply_location_zone(p, z)
    assert [z["id"] for z in zones] == ["z1", "z2"]          # order kept
    assert zones[0]["label"] == "renamed" and zones[0]["level"] == 4


def test_apply_location_zone_deletes_by_id(tmp_path):
    p = str(tmp_path / "location_zones.json")
    for label in ("a", "b"):
        _ok, z, _ = M.validate_zone_payload(_zone_payload(label=label),
                                            _WANS, 5)
        M.apply_location_zone(p, z)
    zones = M.apply_location_zone(p, {"id": "z1", "delete": True})
    assert [z["id"] for z in zones] == ["z2"]


def test_apply_location_zone_reports_an_unknown_id(tmp_path):
    p = str(tmp_path / "location_zones.json")
    _ok, z, _ = M.validate_zone_payload(_zone_payload(), _WANS, 5)
    M.apply_location_zone(p, z)
    assert M.apply_location_zone(p, {"id": "z9", "delete": True}) is None
    _ok, z, _ = M.validate_zone_payload(_zone_payload(id="z9"), _WANS, 5)
    assert M.apply_location_zone(p, z) is None
    # A refused change must not have touched the file.
    assert [z["id"] for z in
            _json.loads(Path(p).read_text())["zones"]] == ["z1"]


def test_apply_location_zone_leaves_valid_json_with_no_zones_left(tmp_path):
    """The daemon re-reads this file on every change; deleting the last zone
    must leave it parseable, not empty or absent."""
    p = str(tmp_path / "location_zones.json")
    _ok, z, _ = M.validate_zone_payload(_zone_payload(), _WANS, 5)
    M.apply_location_zone(p, z)
    assert M.apply_location_zone(p, {"id": "z1", "delete": True}) == []
    assert _json.loads(Path(p).read_text())["zones"] == []


def test_apply_location_zone_fails_open_on_an_unreadable_file(tmp_path):
    p = tmp_path / "location_zones.json"
    p.write_text("{not json")
    _ok, z, _ = M.validate_zone_payload(_zone_payload(), _WANS, 5)
    zones = M.apply_location_zone(str(p), z)
    assert [z["id"] for z in zones] == ["z1"]


# -- what the page needs to offer a level --------------------------------------


def _levels_cfg(tmp_path, **over):
    m = M.resolve_map_cfg({
        "stations_path": str(tmp_path / "s.json"),
        "labels_path": str(tmp_path / "l.json"),
        "environ_points_path": str(tmp_path / "e.json"),
        "location_store_path": str(tmp_path / "store.json"),
        "location_config_path": str(tmp_path / "lf.json"),
        "location_zones_path": str(tmp_path / "location_zones.json")})
    m.update(over)
    return m


def _fec_cfg(profiles=None):
    return M.FecCfg(enabled=True, fifo="/dev/null",
                    loss_table=FC.DEFAULT_LOSS_TABLE, ramp_up_ticks=1,
                    ramp_down_hold_s=0, full_mode_backoff_fec="8:0",
                    full_min_up_wans=2, floor_ratio="20:1",
                    wan_profiles=profiles or {})


def test_map_location_layer_tags_both_zone_sources(tmp_path):
    """A config zone is not editable from the map and an operator zone is, so
    the page has to be able to tell them apart without guessing."""
    cfgz = tmp_path / "lf.json"
    cfgz.write_text(_json.dumps({"zones": [
        {"label": "yard", "lat": 41.1, "lon": -73.5, "radius_m": 300,
         "level": 2}]}))
    opz = tmp_path / "location_zones.json"
    opz.write_text(_json.dumps({"zones": [
        {"id": "z1", "label": "dock", "lat": 41.2, "lon": -73.6,
         "radius_m": 150, "level": 3, "wans": ["wan1"],
         "suppress_learned": True}]}))
    out = M.map_location_layer(str(tmp_path / "absent.json"), str(cfgz), None,
                               10, operator_zones_path=str(opz))
    assert [z["source"] for z in out["zones"]] == ["config", "operator"]
    assert "id" not in out["zones"][0]
    assert out["zones"][1]["id"] == "z1"
    assert out["zones"][1]["label"] == "dock"
    assert out["zones"][1]["wans"] == ["wan1"]


def test_map_location_layer_without_an_operator_file_is_config_only(tmp_path):
    cfgz = tmp_path / "lf.json"
    cfgz.write_text(_json.dumps({"zones": [
        {"label": "yard", "lat": 41.1, "lon": -73.5, "radius_m": 300,
         "level": 2}]}))
    out = M.map_location_layer(str(tmp_path / "absent.json"), str(cfgz), None,
                               10, operator_zones_path=str(tmp_path / "no.json"))
    assert [z["source"] for z in out["zones"]] == ["config"]


def test_map_payload_levels_describe_the_driving_profiles_table(tmp_path):
    m = _levels_cfg(tmp_path)
    pub = tmp_path / "pub.json"
    pub.write_text(_json.dumps({"fec": {"floor_ratio": "8:4",
                                        "profile": {"driver_wan": "wan1"}},
                                "wan_labels": {"wan1": "Cell",
                                               "wan2": "Satellite"}}))
    out = M.assemble_map_payload(m, str(pub), None, 1000.0,
                                 fec_cfg=_fec_cfg())
    loc = out["location_fec"]
    assert [lv["level"] for lv in loc["levels"]] == [0, 1, 2, 3, 4]
    assert [lv["ratio"] for lv in loc["levels"]] == ["8:0", "8:2", "8:4",
                                                   "8:6", "8:8"]
    assert [lv["overhead_pct"] for lv in loc["levels"]] == [0.0, 25.0, 50.0,
                                                          75.0, 100.0]
    # 8:4 is 50% overhead, which is the third rung of this table.
    assert loc["floor_level"] == 2
    assert loc["wans"] == {"wan1": "Cell", "wan2": "Satellite"}


def test_map_payload_levels_follow_the_driver_onto_a_shorter_table(tmp_path):
    m = _levels_cfg(tmp_path)
    pub = tmp_path / "pub.json"
    pub.write_text(_json.dumps({"fec": {"floor_ratio": "20:1",
                                        "profile": {"driver_wan": "wan1"}}}))
    fec = _fec_cfg({"wan1": M.WanProfileCfg(
        name="wan1", loss_table=FC.DEFAULT_CELL_LOSS_TABLE, ramp_up_ticks=1,
        ramp_down_hold_s=0, floor_ratio="8:0", signal_floor_fec="12:1")})
    loc = M.assemble_map_payload(m, str(pub), None, 1000.0,
                                 fec_cfg=fec)["location_fec"]
    assert [lv["ratio"] for lv in loc["levels"]] == ["8:0", "20:1", "12:1",
                                                   "8:1"]
    assert loc["floor_level"] == 1                 # 20:1 = 5%, the second rung
    assert loc["wans"] == {}                       # no wan_labels published


def test_map_payload_levels_fall_back_to_the_default_table_without_a_driver(tmp_path):
    """A snapshot that names no driver is the state right after a restart;
    the page still has to be able to offer a level."""
    m = _levels_cfg(tmp_path)
    pub = tmp_path / "pub.json"
    pub.write_text(_json.dumps({"fec": {"floor_ratio": "20:1"}}))
    fec = _fec_cfg({"wan1": M.WanProfileCfg(
        name="wan1", loss_table=FC.DEFAULT_CELL_LOSS_TABLE, ramp_up_ticks=1,
        ramp_down_hold_s=0, floor_ratio="8:0", signal_floor_fec="12:1")})
    loc = M.assemble_map_payload(m, str(pub), None, 1000.0,
                                 fec_cfg=fec)["location_fec"]
    assert len(loc["levels"]) == len(FC.DEFAULT_LOSS_TABLE)
    assert loc["floor_level"] == 0                 # 20:1 is below every rung


def test_map_payload_without_a_fec_cfg_degrades_but_never_omits(tmp_path):
    """The map page reads location_fec.levels unconditionally; the keys have
    to be there even on a box with no FEC configured at all."""
    m = _levels_cfg(tmp_path)
    out = M.assemble_map_payload(m, str(tmp_path / "pub.json"), None, 1000.0)
    loc = out["location_fec"]
    assert loc["levels"] == [] and loc["floor_level"] is None
    assert loc["wans"] == {}
    assert loc["tiles"] == [] and loc["zones"] == []
    assert out["fix"] is None and out["stations"] == []


# -- round 2: every live zone visible, ids never reused ------------------------


def test_map_location_layer_shows_an_operator_zone_with_no_id(tmp_path):
    """A zone with no id is still a zone the daemon acts on -- validate_zone
    never asked for one. Hiding it would mean the map omits a circle that is
    steering real parity; show it, and say it cannot be edited from here."""
    opz = tmp_path / "location_zones.json"
    opz.write_text(_json.dumps({"zones": [
        {"id": "z1", "label": "dock", "lat": 41.2, "lon": -73.6,
         "radius_m": 150, "level": 3},
        {"label": "hand written", "lat": 41.3, "lon": -73.7,
         "radius_m": 200, "level": 1},
        {"id": "not-a-zone-id", "label": "also hand written", "lat": 41.4,
         "lon": -73.8, "radius_m": 250, "level": 2}]}))
    out = M.map_location_layer(str(tmp_path / "absent.json"),
                               str(tmp_path / "absent-cfg.json"), None, 10,
                               operator_zones_path=str(opz))
    assert [z["label"] for z in out["zones"]] == [
        "dock", "hand written", "also hand written"]
    assert [z["source"] for z in out["zones"]] == ["operator"] * 3
    assert [z["editable"] for z in out["zones"]] == [True, False, False]
    assert out["zones"][0]["id"] == "z1"
    assert "id" not in out["zones"][1]
    assert "id" not in out["zones"][2]      # an unusable id is no id at all


def test_map_location_layer_still_drops_a_zone_with_unusable_geometry(tmp_path):
    """An id-less row is editable elsewhere, not unusable. A row with no
    position is unusable, and stays dropped."""
    opz = tmp_path / "location_zones.json"
    opz.write_text(_json.dumps({"zones": [
        {"label": "no position", "radius_m": 200, "level": 1},
        {"label": "no radius", "lat": 41.3, "lon": -73.7, "level": 1},
        {"label": "keeps its place", "lat": 41.3, "lon": -73.7,
         "radius_m": 200, "level": 1}]}))
    out = M.map_location_layer(str(tmp_path / "absent.json"),
                               str(tmp_path / "absent-cfg.json"), None, 10,
                               operator_zones_path=str(opz))
    assert [z["label"] for z in out["zones"]] == ["keeps its place"]


def test_config_zones_are_never_editable(tmp_path):
    cfgz = tmp_path / "lf.json"
    cfgz.write_text(_json.dumps({"zones": [
        {"label": "yard", "lat": 41.1, "lon": -73.5, "radius_m": 300,
         "level": 2}]}))
    out = M.map_location_layer(str(tmp_path / "absent.json"), str(cfgz), None,
                               10)
    assert out["zones"][0]["editable"] is False
    assert out["zones"][0]["source"] == "config"


def test_apply_location_zone_never_reuses_an_id_after_a_delete(tmp_path):
    """A stale editor panel still holding z2 must never be able to save over a
    DIFFERENT z2 handed out later. The counter is persisted for that reason."""
    p = str(tmp_path / "location_zones.json")
    for label in ("a", "b"):
        _ok, z, _ = M.validate_zone_payload(_zone_payload(label=label),
                                            _WANS, 5)
        M.apply_location_zone(p, z)
    M.apply_location_zone(p, {"id": "z2", "delete": True})   # the highest
    _ok, z, _ = M.validate_zone_payload(_zone_payload(label="c"), _WANS, 5)
    zones = M.apply_location_zone(p, z)
    assert [z["id"] for z in zones] == ["z1", "z3"]
    assert _json.loads(Path(p).read_text())["next_id"] == 4


def test_apply_location_zone_keeps_the_counter_past_an_empty_file(tmp_path):
    p = str(tmp_path / "location_zones.json")
    _ok, z, _ = M.validate_zone_payload(_zone_payload(), _WANS, 5)
    M.apply_location_zone(p, z)
    assert M.apply_location_zone(p, {"id": "z1", "delete": True}) == []
    body = _json.loads(Path(p).read_text())
    assert body["zones"] == [] and body["next_id"] == 2
    _ok, z, _ = M.validate_zone_payload(_zone_payload(), _WANS, 5)
    assert [z["id"] for z in M.apply_location_zone(p, z)] == ["z2"]


def test_apply_location_zone_allocates_without_a_stored_counter(tmp_path):
    """Files written before the counter existed, and hand-edited ones, still
    have to allocate a free id."""
    p = tmp_path / "location_zones.json"
    p.write_text(_json.dumps({"zones": [
        {"id": "z1", "label": "a", "lat": 41.1, "lon": -73.5,
         "radius_m": 300, "level": 1},
        {"id": "z2", "label": "b", "lat": 41.1, "lon": -73.5,
         "radius_m": 300, "level": 1}]}))
    _ok, z, _ = M.validate_zone_payload(_zone_payload(label="c"), _WANS, 5)
    zones = M.apply_location_zone(str(p), z)
    assert [z["id"] for z in zones] == ["z1", "z2", "z3"]


def test_apply_location_zone_never_collides_with_a_stale_counter(tmp_path):
    """A hand-edited counter below an id already in the file must not hand out
    that id again: the watermark is the max of the two."""
    p = tmp_path / "location_zones.json"
    p.write_text(_json.dumps({"next_id": 2, "zones": [
        {"id": "z5", "label": "a", "lat": 41.1, "lon": -73.5,
         "radius_m": 300, "level": 1}]}))
    _ok, z, _ = M.validate_zone_payload(_zone_payload(label="b"), _WANS, 5)
    zones = M.apply_location_zone(str(p), z)
    assert [z["id"] for z in zones] == ["z5", "z6"]


def test_apply_location_zone_ignores_an_unusable_counter(tmp_path):
    for n, bad in enumerate(("3", True, -1, 0, None, 2.5, [])):
        p = tmp_path / ("counter-%d.json" % n)
        p.write_text(_json.dumps({"next_id": bad, "zones": [
            {"id": "z1", "label": "a", "lat": 41.1, "lon": -73.5,
             "radius_m": 300, "level": 1}]}))
        _ok, z, _ = M.validate_zone_payload(_zone_payload(label="b"),
                                            _WANS, 5)
        zones = M.apply_location_zone(str(p), z)
        assert [z["id"] for z in zones] == ["z1", "z2"], bad


# -- round 3: one bad zone must not cost the page the whole map ---------------


def test_map_location_layer_drops_a_zone_with_a_non_finite_position(tmp_path):
    """json.dumps writes NaN and Infinity as bare tokens and JSON.parse throws
    on them, so ONE poisoned zone costs the page the entire payload -- the
    same class already fixed for a tile's residual."""
    zones = tmp_path / "location_zones.json"
    for bad in ("NaN", "Infinity", "-Infinity"):
        zones.write_text(
            '{"zones": [{"label": "poison", "lat": %s, "lon": -73.5,'
            ' "radius_m": 300, "level": 2},'
            ' {"label": "good", "lat": 41.1, "lon": -73.5,'
            ' "radius_m": 300, "level": 2}]}' % bad)
        out = M.map_location_layer(str(tmp_path / "absent.json"),
                                   str(tmp_path / "absent-cfg.json"), None, 10,
                                   operator_zones_path=str(zones))
        assert [z["label"] for z in out["zones"]] == ["good"], bad
        _json.dumps(out, allow_nan=False)     # what the browser must parse


def test_map_location_layer_drops_a_zone_with_a_non_finite_radius(tmp_path):
    zones = tmp_path / "lf.json"
    zones.write_text(
        '{"zones": [{"label": "poison", "lat": 41.1, "lon": -73.5,'
        ' "radius_m": Infinity, "level": 2},'
        ' {"label": "good", "lat": 41.1, "lon": -73.5,'
        ' "radius_m": 300, "level": 2}]}')
    out = M.map_location_layer(str(tmp_path / "absent.json"), str(zones),
                               None, 10)
    assert [z["label"] for z in out["zones"]] == ["good"]
    _json.dumps(out, allow_nan=False)


def test_map_location_layer_drops_a_zone_whose_numbers_overflow(tmp_path):
    """float() and int() raise OverflowError -- not ValueError -- on an
    integer literal too large for a float and on an infinite level, and
    OverflowError was in neither except tuple.

    The endpoint refuses both, so this is not reachable through the map; the
    CONFIG zone file is hand-edited by design and feeds the same rows, and one
    poisoned character there took out the whole /api/map response."""
    big = "1" + "0" * 400
    good = ('{"label": "good", "lat": 41.1, "lon": -73.5,'
            ' "radius_m": 300, "level": 2}')
    zones = tmp_path / "location-fec.json"
    for key, bad in (("lat", big), ("lon", big), ("radius_m", big),
                     ("level", "Infinity")):
        fields = {"label": '"poison"', "lat": "41.1", "lon": "-73.5",
                  "radius_m": "300", "level": "2"}
        fields[key] = bad
        poison = "{%s}" % ", ".join('"%s": %s' % kv for kv in fields.items())
        zones.write_text('{"zones": [%s, %s]}' % (poison, good))
        out = M.map_location_layer(str(tmp_path / "absent.json"), str(zones),
                                   None, 10)
        assert [z["label"] for z in out["zones"]] == ["good"], key
        _json.dumps(out, allow_nan=False)     # what the browser must parse


def test_map_payload_stays_parseable_with_a_poisoned_zone(tmp_path):
    m = M.resolve_map_cfg({"stations_path": str(tmp_path / "s.json"),
                           "labels_path": str(tmp_path / "l.json"),
                           "environ_points_path": str(tmp_path / "e.json"),
                           "location_store_path": str(tmp_path / "store.json"),
                           "location_config_path": str(tmp_path / "lf.json"),
                           "location_zones_path": str(tmp_path / "op.json")})
    Path(m["location_zones_path"]).write_text(
        '{"zones": [{"id": "z1", "label": "poison", "lat": NaN,'
        ' "lon": -73.5, "radius_m": 300, "level": 2}]}')
    out = M.assemble_map_payload(m, str(tmp_path / "pub.json"), None, 1000.0)
    assert out["location_fec"]["zones"] == []
    _json.dumps(out, allow_nan=False)


# -- round 3: the file cannot grow without bound -------------------------------


def test_apply_location_zone_refuses_a_create_past_the_cap(tmp_path):
    """Every zone is walked once per look-ahead point in the 1 Hz loop and
    rides in every 3 s map payload; the per-request byte cap bounds one POST,
    nothing bounded the file."""
    p = str(tmp_path / "location_zones.json")
    for i in range(M._MAX_OPERATOR_ZONES):
        _ok, z, _ = M.validate_zone_payload(_zone_payload(label="z%d" % i),
                                            _WANS, 5)
        M.apply_location_zone(p, z)
    assert len(_json.loads(Path(p).read_text())["zones"]) == 200

    _ok, z, _ = M.validate_zone_payload(_zone_payload(label="one too many"),
                                        _WANS, 5)
    with pytest.raises(M.ZoneLimitError) as ei:
        M.apply_location_zone(p, z)
    assert "200" in str(ei.value)
    assert len(_json.loads(Path(p).read_text())["zones"]) == 200


def test_apply_location_zone_still_edits_and_deletes_at_the_cap(tmp_path):
    """The cap must never trap the operator: the way back under it is an
    update or a delete, so those stay allowed."""
    p = str(tmp_path / "location_zones.json")
    for i in range(M._MAX_OPERATOR_ZONES):
        _ok, z, _ = M.validate_zone_payload(_zone_payload(label="z%d" % i),
                                            _WANS, 5)
        M.apply_location_zone(p, z)
    _ok, z, _ = M.validate_zone_payload(
        _zone_payload(id="z7", label="renamed at the cap"), _WANS, 5)
    zones = M.apply_location_zone(p, z)
    assert len(zones) == 200
    assert [x for x in zones if x["id"] == "z7"][0]["label"] \
        == "renamed at the cap"
    assert len(M.apply_location_zone(p, {"id": "z7", "delete": True})) == 199


# -- round 3: a level the driving table has lost --------------------------------


def test_validate_zone_payload_keeps_a_level_the_driving_table_has_lost():
    """A zone stored at level 4 opened while a 4-rung cellular profile drives
    must save back at 4. Refusing it would make the zone uneditable; snapping
    it to 3 would quietly rewrite a floor the operator set."""
    ok, zone, err = M.validate_zone_payload(
        _zone_payload(id="z1", level=4), _WANS, 4, keep_level=4)
    assert ok and err is None and zone["level"] == 4
    # It only ever PRESERVES: a different level past the table is still out.
    ok, _z, err = M.validate_zone_payload(
        _zone_payload(id="z1", level=4), _WANS, 4, keep_level=2)
    assert not ok and "level" in err
    ok, _z, err = M.validate_zone_payload(
        _zone_payload(id="z1", level=4), _WANS, 4)
    assert not ok and "level" in err
    # And a level below the table's top is unaffected either way.
    ok, zone, _ = M.validate_zone_payload(
        _zone_payload(id="z1", level=2), _WANS, 4, keep_level=4)
    assert ok and zone["level"] == 2


def test_stored_zone_level_reads_only_a_usable_level(tmp_path):
    p = tmp_path / "location_zones.json"
    p.write_text(_json.dumps({"zones": [
        {"id": "z1", "label": "a", "lat": 41.1, "lon": -73.5,
         "radius_m": 300, "level": 4},
        {"id": "z2", "label": "b", "lat": 41.1, "lon": -73.5,
         "radius_m": 300, "level": True},
        {"id": "z3", "label": "c", "lat": 41.1, "lon": -73.5,
         "radius_m": 300, "level": "4"}]}))
    assert M.stored_zone_level(str(p), "z1") == 4
    assert M.stored_zone_level(str(p), "z2") is None     # bool is not a level
    assert M.stored_zone_level(str(p), "z3") is None
    assert M.stored_zone_level(str(p), "z9") is None
    assert M.stored_zone_level(str(tmp_path / "absent.json"), "z1") is None


# -- round 5: the map and the daemon must be reading one file ------------------


def _record(tmp_path, **over):
    """What location_fec publishes, at a timestamp the reader calls fresh."""
    rec = {"set_ts": 1000.0, "source": "location_fec", "wans": {}}
    rec.update(over)
    (tmp_path / "location_fec.json").write_text(_json.dumps(rec))
    return M.LocationFecCfg(state_path=str(tmp_path / "location_fec.json"),
                            enabled=True, stale_after_s=30.0)


def test_no_zones_path_mismatch_when_both_sides_read_one_file(tmp_path):
    m = _levels_cfg(tmp_path)
    loc = _record(tmp_path, operator_zones_path=m["location_zones_path"])
    assert M.location_zones_path_mismatch(loc, m, 1000.0) is None
    # Spelt differently, still the same file: a mismatch we cannot prove is
    # not a mismatch, and a false one would disable Save on a working box.
    loc = _record(tmp_path,
                  operator_zones_path=str(tmp_path) + "/./location_zones.json")
    assert M.location_zones_path_mismatch(loc, m, 1000.0) is None


def test_zones_path_mismatch_names_the_file_the_daemon_reads(tmp_path):
    """The map saves to its own configured path and the daemon reads its own;
    nothing ties the two settings together. Diverged, the save returns 200 and
    no floor ever moves -- so the payload has to carry the daemon's path, the
    one thing the operator has to go and fix."""
    m = _levels_cfg(tmp_path)
    theirs = str(tmp_path / "somewhere-else" / "zones.json")
    loc = _record(tmp_path, operator_zones_path=theirs)
    assert M.location_zones_path_mismatch(loc, m, 1000.0) == theirs
    out = M.assemble_map_payload(m, str(tmp_path / "pub.json"), None, 1000.0,
                                 location_cfg=loc)
    assert out["location_fec"]["zones_path_mismatch"] == theirs


def test_zones_path_mismatch_is_null_when_it_cannot_be_proved(tmp_path):
    """Absent, stale, unparseable, or simply from a daemon too old to publish
    the key: none of those are evidence of a mismatch, and claiming one would
    disable Save on a box that is working."""
    m = _levels_cfg(tmp_path)
    theirs = str(tmp_path / "somewhere-else" / "zones.json")
    absent = M.LocationFecCfg(state_path=str(tmp_path / "gone.json"),
                              enabled=True, stale_after_s=30.0)
    assert M.location_zones_path_mismatch(absent, m, 1000.0) is None
    assert M.location_zones_path_mismatch(None, m, 1000.0) is None
    stale = _record(tmp_path, set_ts=100.0, operator_zones_path=theirs)
    assert M.location_zones_path_mismatch(stale, m, 1000.0) is None
    old = _record(tmp_path)                       # no such key in the record
    assert M.location_zones_path_mismatch(old, m, 1000.0) is None
    for bad in (7, None, ""):
        rec = _record(tmp_path, operator_zones_path=bad)
        assert M.location_zones_path_mismatch(rec, m, 1000.0) is None
    (tmp_path / "location_fec.json").write_text("{not json")
    assert M.location_zones_path_mismatch(old, m, 1000.0) is None
    # And the payload always carries the key, so the page never has to guess.
    out = M.assemble_map_payload(m, str(tmp_path / "pub.json"), None, 1000.0)
    assert out["location_fec"]["zones_path_mismatch"] is None
