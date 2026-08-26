import json as _json
import logging as _logging
from pathlib import Path

import fec_control as F
import tile_store as T
import location_fec as M

ROOT = Path(__file__).resolve().parent.parent


def _learned(tile, wan="wan1", loss=9.0, passes=3):
    s = T.TileStore()
    for i in range(passes):
        s.observe(tile, {wan: loss}, None, now_mono=100.0 * i, now_wall=1000.0 + i)
        s.close_pass(now_wall=1000.0 + i)
    return s


def test_candidate_points_is_current_only_when_stopped():
    pts = M.candidate_points((41.1, -73.5, 0.4, 90.0), lookahead_s=25.0,
                             min_speed_ms=2.0, sample_step_m=75.0)
    assert pts == [(41.1, -73.5)]


def test_candidate_points_is_current_only_without_a_track():
    pts = M.candidate_points((41.1, -73.5, 25.0, None), lookahead_s=25.0,
                             min_speed_ms=2.0, sample_step_m=75.0)
    assert pts == [(41.1, -73.5)]


def test_candidate_points_samples_the_whole_lookahead():
    # 25 m/s for 25 s = 625 m, sampled every 75 m -> current + 8 projections.
    pts = M.candidate_points((41.1, -73.5, 25.0, 90.0), lookahead_s=25.0,
                             min_speed_ms=2.0, sample_step_m=75.0)
    assert len(pts) == 9
    assert pts[0] == (41.1, -73.5)
    assert all(p[1] > -73.5 for p in pts[1:])          # due east
    assert all(abs(p[0] - 41.1) < 0.01 for p in pts[1:])


def test_candidate_points_still_probes_a_horizon_shorter_than_one_step():
    pts = M.candidate_points((41.1, -73.5, 3.0, 90.0), lookahead_s=5.0,
                             min_speed_ms=2.0, sample_step_m=75.0)
    assert len(pts) == 2


def test_candidate_tiles_dedupes_and_keeps_order():
    pts = [(41.100000, -73.500000), (41.100050, -73.500050), (41.104000, -73.500000)]
    tiles = M.candidate_tiles(pts, precision=7)
    assert len(tiles) == 2
    assert tiles[0] == T.encode(41.1, -73.5, 7)


def test_learned_term_takes_the_worst_tile_ahead():
    tile = T.encode(41.104, -73.5, 7)
    store = _learned(tile)
    tiles = [T.encode(41.1, -73.5, 7), tile]
    levels, sources = M.learned_terms(store, tiles, ["wan1"], F.DEFAULT_LOSS_TABLE)
    assert levels["wan1"] == 3
    assert tile in sources["wan1"]


def test_learned_term_is_zero_for_an_unconfirmed_tile():
    tile = T.encode(41.1, -73.5, 7)
    store = _learned(tile, passes=2)
    levels, _ = M.learned_terms(store, [tile], ["wan1"], F.DEFAULT_LOSS_TABLE)
    assert levels["wan1"] == 0


def test_zone_matches_the_current_position():
    zones = [{"label": "yard", "lat": 41.1, "lon": -73.5, "radius_m": 300,
              "level": 2, "wans": None, "suppress_learned": False}]
    levels, labels, suppressed = M.zone_terms(zones, [(41.1, -73.5)], ["wan1"])
    assert levels["wan1"] == 2
    assert labels["wan1"] == "yard"
    assert suppressed == {}          # {wan: {tile, ...}}, empty: nothing suppressed


def test_zone_matches_a_projected_point_before_arrival():
    zones = [{"label": "underpass", "lat": 41.104, "lon": -73.5, "radius_m": 150,
              "level": 4, "wans": None, "suppress_learned": False}]
    pts = M.candidate_points((41.1, -73.5, 25.0, 0.0), lookahead_s=25.0,
                             min_speed_ms=2.0, sample_step_m=75.0)
    levels, _, _ = M.zone_terms(zones, pts, ["wan1"])
    assert levels["wan1"] == 4


def test_zone_outside_its_radius_does_not_match():
    zones = [{"label": "yard", "lat": 41.2, "lon": -73.5, "radius_m": 300,
              "level": 2, "wans": None, "suppress_learned": False}]
    levels, _, _ = M.zone_terms(zones, [(41.1, -73.5)], ["wan1"])
    assert levels["wan1"] == 0


def test_zone_applies_only_to_its_listed_wans():
    zones = [{"label": "yard", "lat": 41.1, "lon": -73.5, "radius_m": 300,
              "level": 2, "wans": ["wan1"], "suppress_learned": False}]
    levels, _, _ = M.zone_terms(zones, [(41.1, -73.5)], ["wan1", "wan2"])
    assert levels == {"wan1": 2, "wan2": 0}


def test_manual_and_learned_combine_by_max_learned_higher():
    tile = T.encode(41.1, -73.5, 7)
    store = _learned(tile, loss=9.0)                       # level 3
    zones = [{"label": "yard", "lat": 41.1, "lon": -73.5, "radius_m": 300,
              "level": 1, "wans": None, "suppress_learned": False}]
    out = M.resolve(store, (41.1, -73.5, 0.0, None), zones, ["wan1"],
                    F.DEFAULT_LOSS_TABLE, precision=7, lookahead_s=25.0,
                    min_speed_ms=2.0, sample_step_m=75.0)
    assert out["wan1"]["level"] == 3


def test_manual_and_learned_combine_by_max_manual_higher():
    tile = T.encode(41.1, -73.5, 7)
    store = _learned(tile, loss=1.0)                       # level 1
    zones = [{"label": "yard", "lat": 41.1, "lon": -73.5, "radius_m": 300,
              "level": 3, "wans": None, "suppress_learned": False}]
    out = M.resolve(store, (41.1, -73.5, 0.0, None), zones, ["wan1"],
                    F.DEFAULT_LOSS_TABLE, precision=7, lookahead_s=25.0,
                    min_speed_ms=2.0, sample_step_m=75.0)
    assert out["wan1"]["level"] == 3
    assert "yard" in out["wan1"]["reason"]


def test_suppress_learned_leaves_only_the_manual_level():
    tile = T.encode(41.1, -73.5, 7)
    store = _learned(tile, loss=9.0)                       # level 3
    zones = [{"label": "yard", "lat": 41.1, "lon": -73.5, "radius_m": 300,
              "level": 1, "wans": None, "suppress_learned": True}]
    out = M.resolve(store, (41.1, -73.5, 0.0, None), zones, ["wan1"],
                    F.DEFAULT_LOSS_TABLE, precision=7, lookahead_s=25.0,
                    min_speed_ms=2.0, sample_step_m=75.0)
    assert out["wan1"]["level"] == 1


def test_suppress_learned_is_scoped_to_the_zone_tiles():
    """A suppress zone must not blind the whole look-ahead.

    Approaching a zone at speed, the look-ahead reaches it and the zone
    matches — but a confirmed bad tile 400 m short of the zone is nowhere near
    it, and the operator only overruled the learner INSIDE the circle."""
    tile = T.encode(41.1, -73.5, 7)
    store = _learned(tile, loss=9.0)                       # level 3
    zones = [{"label": "underpass", "lat": 41.104, "lon": -73.5,
              "radius_m": 150, "level": 1, "wans": None,
              "suppress_learned": True}]
    out = M.resolve(store, (41.1, -73.5, 25.0, 0.0), zones, ["wan1"],
                    F.DEFAULT_LOSS_TABLE, precision=7, lookahead_s=25.0,
                    min_speed_ms=2.0, sample_step_m=75.0)
    assert out["wan1"]["level"] == 3
    assert out["wan1"]["reason"].startswith("learned ")


def test_zone_terms_suppresses_only_the_tiles_inside_the_circle():
    inside = T.encode(41.104, -73.5, 7)
    outside = T.encode(41.1, -73.5, 7)
    zones = [{"label": "underpass", "lat": 41.104, "lon": -73.5,
              "radius_m": 150, "level": 1, "wans": None,
              "suppress_learned": True}]
    _, _, suppressed = M.zone_terms(zones, [(41.1, -73.5), (41.104, -73.5)],
                                    ["wan1"], [outside, inside])
    assert suppressed == {"wan1": {inside}}


def test_learned_terms_skips_only_the_suppressed_tile():
    near, far = T.encode(41.1, -73.5, 7), T.encode(41.104, -73.5, 7)
    store = _learned(far, loss=9.0)                        # level 3
    levels, _ = M.learned_terms(store, [near, far], ["wan1"],
                                F.DEFAULT_LOSS_TABLE, {"wan1": {far}})
    assert levels["wan1"] == 0
    levels, _ = M.learned_terms(store, [near, far], ["wan1"],
                                F.DEFAULT_LOSS_TABLE, {"wan1": {near}})
    assert levels["wan1"] == 3


def test_resolve_clamps_a_level_to_the_active_table():
    tile = T.encode(41.1, -73.5, 7)
    store = _learned(tile, loss=9.0)
    out = M.resolve(store, (41.1, -73.5, 0.0, None), [], ["wan1"],
                    F.DEFAULT_CELL_LOSS_TABLE, precision=7, lookahead_s=25.0,
                    min_speed_ms=2.0, sample_step_m=75.0)
    assert out["wan1"]["level"] <= len(F.DEFAULT_CELL_LOSS_TABLE) - 1


def test_exit_hold_adopts_a_rise_immediately():
    h = M.ExitHold(hold_s=20.0)
    assert h.update({"wan1": 0}, now_mono=0.0) == {"wan1": 0}
    assert h.update({"wan1": 3}, now_mono=1.0) == {"wan1": 3}


def test_exit_hold_delays_a_drop_then_releases():
    h = M.ExitHold(hold_s=20.0)
    h.update({"wan1": 3}, now_mono=0.0)
    assert h.update({"wan1": 0}, now_mono=5.0) == {"wan1": 3}
    assert h.update({"wan1": 0}, now_mono=24.0) == {"wan1": 3}
    assert h.update({"wan1": 0}, now_mono=26.0) == {"wan1": 0}


def test_exit_hold_runs_from_the_drop_not_from_adoption():
    # A tile occupied for longer than hold_s still gets the full hold on exit.
    h = M.ExitHold(hold_s=20.0)
    h.update({"wan1": 3}, now_mono=0.0)
    assert h.update({"wan1": 3}, now_mono=100.0) == {"wan1": 3}
    assert h.update({"wan1": 0}, now_mono=100.0) == {"wan1": 3}
    assert h.update({"wan1": 0}, now_mono=119.0) == {"wan1": 3}
    assert h.update({"wan1": 0}, now_mono=121.0) == {"wan1": 0}


def test_exit_hold_abandons_the_hold_when_the_level_climbs_again():
    h = M.ExitHold(hold_s=20.0)
    h.update({"wan1": 3}, now_mono=0.0)
    h.update({"wan1": 0}, now_mono=5.0)
    assert h.update({"wan1": 4}, now_mono=6.0) == {"wan1": 4}
    assert h.update({"wan1": 0}, now_mono=7.0) == {"wan1": 4}


def _cfg_raw(tmp_path, **over):
    raw = {
        "gpsd": {"host": "127.0.0.1", "port": 2947},
        "wans": ["wan1", "wan2"],
        "state_path": str(tmp_path / "state.json"),
        "store_path": str(tmp_path / "store.json"),
        "output_path": str(tmp_path / "location_fec.json"),
        "poll_interval_s": 1.0,
        "tile": {"precision": 7},
        "learning": {"min_passes": 3, "alpha": 0.35, "pass_gap_s": 30,
                     "max_tiles": 20000, "max_age_days": 14,
                     "clean_drop_days": 7, "save_interval_s": 60},
        "withdraw": {"max_stale_s": 600},
        "lookahead": {"seconds": 25, "min_speed_ms": 2.0,
                      "sample_step_m": 75, "exit_hold_s": 20},
        "zones": [{"label": "yard", "lat": 41.1, "lon": -73.5,
                   "radius_m": 300, "level": 2}],
    }
    raw.update(over)
    p = tmp_path / "location-fec.json"
    p.write_text(_json.dumps(raw))
    return str(p)


def test_load_config_reads_zones_and_defaults(tmp_path):
    cfg = M.load_location_config(_cfg_raw(tmp_path))
    assert cfg.precision == 7
    assert cfg.lookahead_s == 25
    assert cfg.exit_hold_s == 20
    assert cfg.wans == ["wan1", "wan2"]
    assert cfg.zones[0]["label"] == "yard"
    assert cfg.zones[0]["wans"] is None
    assert cfg.zones[0]["suppress_learned"] is False


def test_an_invalid_zone_is_skipped_not_fatal(tmp_path):
    path = _cfg_raw(tmp_path, zones=[
        {"label": "good", "lat": 41.1, "lon": -73.5, "radius_m": 300, "level": 2},
        {"label": "no level", "lat": 41.1, "lon": -73.5, "radius_m": 300},
        {"label": "level past the table", "lat": 41.1, "lon": -73.5,
         "radius_m": 300, "level": 99},
        {"label": "no position", "radius_m": 300, "level": 2},
    ])
    cfg = M.load_location_config(path)
    assert [z["label"] for z in cfg.zones] == ["good"]


def test_read_state_returns_per_wan_loss_and_residual(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(_json.dumps({
        "ts": 1000.0,
        "client_local": {"wan1": {"loss_pct": 4.0}, "wan2": {"loss_pct": None}},
        "fec": {"directions": {"client_to_relay": {
            "rx": {"lost_pkts_est_per_s": 3.5}}}},
    }))
    loss, residual = M.read_state(str(p), now_wall=1005.0, max_age_s=10.0)
    assert loss == {"wan1": 4.0}
    assert residual == 3.5


def test_read_state_rejects_a_stale_snapshot(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(_json.dumps({"ts": 1000.0, "client_local": {"wan1": {"loss_pct": 4.0}}}))
    assert M.read_state(str(p), now_wall=1100.0, max_age_s=10.0) is None


def test_read_state_fails_open_on_junk(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    assert M.read_state(str(p), now_wall=1000.0, max_age_s=10.0) is None
    assert M.read_state(str(tmp_path / "absent.json"), 1000.0, 10.0) is None


def test_read_state_survives_a_non_dict_client_local(tmp_path):
    # `or {}` only rescues a FALSY value: a non-empty string reaches .items().
    p = tmp_path / "state.json"
    p.write_text(_json.dumps({"ts": 1000.0, "client_local": "wan1"}))
    assert M.read_state(str(p), now_wall=1000.0, max_age_s=10.0) is None


def test_read_state_skips_a_malformed_wan_and_keeps_the_good_one(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(_json.dumps({"ts": 1000.0,
                              "client_local": {"wan1": "up", "wan2": {"loss_pct": 4.0}}}))
    loss, residual = M.read_state(str(p), now_wall=1000.0, max_age_s=10.0)
    assert loss == {"wan2": 4.0} and residual is None


def test_read_state_treats_a_non_dict_rx_as_no_residual(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(_json.dumps({
        "ts": 1000.0, "client_local": {"wan1": {"loss_pct": 4.0}},
        "fec": {"directions": {"client_to_relay": {"rx": "none"}}}}))
    loss, residual = M.read_state(str(p), now_wall=1000.0, max_age_s=10.0)
    assert loss == {"wan1": 4.0} and residual is None


def test_reload_reapplies_the_hold_and_the_learning_parameters(tmp_path):
    # SIGHUP is the documented way to change zones without losing the open
    # pass. An operator who edits the hold or the learning parameters in the
    # same file must not have to guess that those alone need a restart.
    cfg = M.load_location_config(_cfg_raw(
        tmp_path, **{"lookahead": {"seconds": 25, "min_speed_ms": 2.0,
                                   "sample_step_m": 75, "exit_hold_s": 45},
                     "learning": {"min_passes": 5, "alpha": 0.2,
                                  "pass_gap_s": 90, "max_tiles": 100,
                                  "max_age_days": 3, "clean_drop_days": 2}}))
    store = T.TileStore()
    hold = M.ExitHold(20.0)
    applied = M.apply_reload(cfg, store, hold, cfg.store_path)
    assert hold.hold_s == 45.0
    assert (store.min_passes, store.alpha, store.pass_gap_s) == (5, 0.2, 90.0)
    assert (store.max_tiles, store.max_age_days, store.clean_drop_days) == (100, 3.0, 2.0)
    assert "exit_hold_s" in applied and "alpha" in applied


def test_reload_ignores_an_unusable_learning_value(tmp_path):
    cfg = M.load_location_config(_cfg_raw(
        tmp_path, learning={"alpha": "sticky", "min_passes": 4}))
    store = T.TileStore()
    applied = M.apply_reload(cfg, store, M.ExitHold(20.0), cfg.store_path)
    assert store.alpha == 0.35            # unchanged, not crashed on
    assert store.min_passes == 4
    assert "alpha" not in applied


def test_reload_does_not_swap_the_store_out_from_under_the_open_pass(tmp_path):
    # Learned tiles live in memory between saves; rebinding the path mid-run
    # would write them to the new file or lose them outright. Defer to restart.
    cfg = M.load_location_config(_cfg_raw(tmp_path))
    store = T.TileStore()
    applied = M.apply_reload(cfg, store, M.ExitHold(20.0), "/var/lib/elsewhere.json")
    assert "store_path" not in applied


def test_reload_keeps_saving_to_the_store_it_actually_loaded(tmp_path, caplog):
    """Warning about the deferral is not the same as deferring.

    main() saves with `store.save(cfg.store_path)`, and cfg has already been
    rebound to the reloaded config by then — so a changed store_path would take
    effect at the very next save: tiles loaded from the old file written to the
    new one, and the old file frozen. Pin the path back for the life of the
    process, which is what the warning and the docs promise."""
    new_path = str(tmp_path / "somewhere-else.json")
    cfg = M.load_location_config(_cfg_raw(tmp_path, store_path=new_path))
    old_path = str(tmp_path / "store.json")
    with caplog.at_level(_logging.WARNING, logger="location_fec"):
        applied = M.apply_reload(cfg, T.TileStore(), M.ExitHold(20.0), old_path)
    assert cfg.store_path == old_path
    assert "store_path" not in applied
    assert sum("store_path" in r.getMessage() for r in caplog.records) == 1


def test_read_state_drops_a_non_finite_loss_and_keeps_the_good_wan(tmp_path):
    # A NaN loss would blend into that WAN's tile EWMA and stay there: every
    # later average is NaN, so the tile can never confirm again.
    p = tmp_path / "state.json"
    p.write_text('{"ts": 1000.0, "client_local": '
                 '{"wan1": {"loss_pct": NaN}, "wan2": {"loss_pct": 4.0}}}')
    loss, _ = M.read_state(str(p), now_wall=1000.0, max_age_s=10.0)
    assert loss == {"wan2": 4.0}


def test_read_state_drops_a_non_finite_residual(tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"ts": 1000.0, "client_local": {"wan1": {"loss_pct": 4.0}}, '
                 '"fec": {"directions": {"client_to_relay": '
                 '{"rx": {"lost_pkts_est_per_s": Infinity}}}}}')
    loss, residual = M.read_state(str(p), now_wall=1000.0, max_age_s=10.0)
    assert loss == {"wan1": 4.0} and residual is None


def test_validate_zone_rejects_a_boolean_level():
    # int(True) is 1, so a hand-edited `"level": true` would quietly become a
    # real floor of level 1 instead of being reported as the mistake it is.
    assert M.validate_zone({"label": "yard", "lat": 41.1, "lon": -73.5,
                            "radius_m": 300, "level": True}, 6) is None


def test_poll_once_publishes_a_zone_floor_without_any_state(tmp_path):
    cfg = M.load_location_config(_cfg_raw(tmp_path))
    store = T.TileStore()
    hold = M.ExitHold(cfg.exit_hold_s)
    rec = M.poll_once(cfg, store, hold, fix=(41.1, -73.5, 0.0, None),
                      state=None, now_mono=0.0, now_wall=1000.0)
    # A floor needs a POSITION, not live loss: the store and the zones already
    # hold everything the resolver needs.
    assert rec["wans"]["wan1"]["level"] == 2
    assert rec["set_ts"] == 1000.0


def test_poll_once_without_a_fix_writes_an_explicit_withdrawal(tmp_path):
    cfg = M.load_location_config(_cfg_raw(tmp_path))
    store = T.TileStore()
    hold = M.ExitHold(cfg.exit_hold_s)
    rec = M.poll_once(cfg, store, hold, fix=None, state=None,
                      now_mono=0.0, now_wall=1000.0)
    assert rec["wans"] == {}
    assert rec["set_ts"] == 1000.0


def test_poll_once_learns_from_state(tmp_path):
    cfg = M.load_location_config(_cfg_raw(tmp_path, zones=[]))
    store = T.TileStore()
    hold = M.ExitHold(cfg.exit_hold_s)
    tile = T.encode(41.1, -73.5, 7)
    for p in range(3):
        M.poll_once(cfg, store, hold, fix=(41.1, -73.5, 0.0, None),
                    state=({"wan1": 9.0}, None), now_mono=1000.0 * p,
                    now_wall=1000.0 + p)
        store.close_pass(now_wall=1000.0 + p)
    assert store.passes_for(tile, "wan1") == 3


def test_write_record_is_atomic_and_readable(tmp_path):
    p = tmp_path / "out" / "location_fec.json"
    M.write_record(str(p), {"set_ts": 1000.0, "wans": {"wan1": {"level": 2}}})
    assert _json.loads(p.read_text())["wans"]["wan1"]["level"] == 2
    assert not (tmp_path / "out" / "location_fec.json.tmp").exists()


def test_fresh_fix_passes_a_recent_fix():
    fix = (41.1, -73.5, 0.0, None, 1000.0)
    assert M.fresh_fix(fix, now_wall=1010.0, max_age_s=30) == fix


def test_fresh_fix_rejects_a_stale_fix():
    fix = (41.1, -73.5, 0.0, None, 1000.0)
    assert M.fresh_fix(fix, now_wall=1040.0, max_age_s=30) is None


def test_fresh_fix_trusts_a_fix_without_a_timestamp():
    fix5 = (41.1, -73.5, 0.0, None, None)
    assert M.fresh_fix(fix5, now_wall=1040.0, max_age_s=30) == fix5
    fix4 = (41.1, -73.5, 0.0, None)
    assert M.fresh_fix(fix4, now_wall=1040.0, max_age_s=30) == fix4


def test_fresh_fix_none_is_none():
    assert M.fresh_fix(None, now_wall=1000.0, max_age_s=30) is None


def test_exit_hold_remembers_the_reason_it_adopted():
    h = M.ExitHold(hold_s=20.0)
    h.update({"wan1": 3}, now_mono=0.0, reasons={"wan1": "zone A"})
    assert h.update({"wan1": 1}, now_mono=5.0, reasons={"wan1": "zone B"}) == {"wan1": 3}
    assert h.reason_for("wan1") == "zone A"


def test_poll_once_labels_a_held_level_with_the_exit_hold_and_its_origin(tmp_path):
    cfg = M.load_location_config(_cfg_raw(tmp_path, zones=[
        {"label": "A", "lat": 41.1, "lon": -73.5, "radius_m": 150, "level": 3},
        {"label": "B", "lat": 41.104, "lon": -73.5, "radius_m": 150, "level": 1},
    ], wans=["wan1"]))
    store = T.TileStore()
    hold = M.ExitHold(cfg.exit_hold_s)

    rec = M.poll_once(cfg, store, hold, fix=(41.1, -73.5, 0.0, None),
                      state=None, now_mono=0.0, now_wall=1000.0)
    assert rec["wans"]["wan1"]["level"] == 3
    assert rec["wans"]["wan1"]["reason"] == "zone A"

    rec = M.poll_once(cfg, store, hold, fix=(41.104, -73.5, 0.0, None),
                      state=None, now_mono=5.0, now_wall=1005.0)
    assert rec["wans"]["wan1"]["level"] == 3
    assert rec["wans"]["wan1"]["reason"] == "exit hold (zone A)"

    rec = M.poll_once(cfg, store, hold, fix=(41.104, -73.5, 0.0, None),
                      state=None, now_mono=30.0, now_wall=1030.0)
    assert rec["wans"]["wan1"]["level"] == 1
    assert rec["wans"]["wan1"]["reason"] == "zone B"


def test_example_config_loads():
    cfg = M.load_location_config(str(ROOT / "config" / "location-fec.example.json"))
    assert cfg.precision == 7
    assert cfg.wans == ["wan1", "wan2"]
    assert len(cfg.zones) == 2
    assert all(z["lat"] == 0.0 and z["lon"] == 0.0 for z in cfg.zones)


def test_episode_begins_once_and_ends_once():
    # A condition that persists across ticks must be announced once, not once
    # per tick: the daemon polls at 1 Hz and this repo has flooded a journal
    # once already.
    ep = M.Episode()
    assert ep.begin() is True
    assert ep.begin() is False
    assert ep.begin() is False
    assert ep.end() is True
    assert ep.end() is False


def test_episode_reopens_after_it_ends():
    ep = M.Episode()
    ep.begin()
    ep.end()
    assert ep.begin() is True


def test_rate_limited_log_swallows_a_repeat_inside_the_window():
    r = M.RateLimitedLog(60.0)
    assert r.due("poll error: boom", now_mono=0.0) == "poll error: boom"
    assert r.due("poll error: boom", now_mono=1.0) is None
    assert r.due("poll error: boom", now_mono=59.9) is None


def test_rate_limited_log_re_logs_with_the_suppressed_count():
    r = M.RateLimitedLog(60.0)
    r.due("poll error: boom", now_mono=0.0)
    for i in range(1, 60):
        r.due("poll error: boom", now_mono=float(i))
    assert r.due("poll error: boom", now_mono=60.0) == \
        "poll error: boom (repeated 59× in the last 60 s)"


def test_rate_limited_log_passes_a_different_message_immediately():
    r = M.RateLimitedLog(60.0)
    assert r.due("poll error: boom", now_mono=0.0) == "poll error: boom"
    assert r.due("poll error: other", now_mono=1.0) == "poll error: other"
    # ...and the new message now owns the window
    assert r.due("poll error: other", now_mono=2.0) is None
