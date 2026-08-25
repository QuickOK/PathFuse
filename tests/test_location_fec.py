import fec_control as F
import tile_store as T
import location_fec as M


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
    assert suppressed == set()


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
    assert h.update({"wan1": 0}, now_mono=19.0) == {"wan1": 3}
    assert h.update({"wan1": 0}, now_mono=21.0) == {"wan1": 0}


def test_exit_hold_abandons_the_hold_when_the_level_climbs_again():
    h = M.ExitHold(hold_s=20.0)
    h.update({"wan1": 3}, now_mono=0.0)
    h.update({"wan1": 0}, now_mono=5.0)
    assert h.update({"wan1": 4}, now_mono=6.0) == {"wan1": 4}
    assert h.update({"wan1": 0}, now_mono=7.0) == {"wan1": 4}
