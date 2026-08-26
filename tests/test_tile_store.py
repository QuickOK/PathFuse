# tests/test_tile_store.py
import tile_store as T


def test_encode_canonical_vector():
    # The reference vector every geohash implementation agrees on.
    assert T.encode(57.64911, 10.40744, precision=11) == "u4pruydqqvj"


def test_encode_precision_7_is_a_prefix_of_11():
    assert T.encode(57.64911, 10.40744, 7) == "u4pruyd"


def test_encode_corners():
    assert T.encode(-90.0, -180.0, 7) == "0000000"
    assert T.encode(89.9999, 179.9999, 7) == "zzzzzzz"


def test_encode_southern_hemisphere_positive_longitude():
    assert T.encode(-33.8688, 151.2093, 7) == "r3gx2f7"


def test_nearby_points_share_a_tile():
    # ~6 m apart: well inside one geohash-7 cell.
    assert T.encode(41.100000, -73.500000) == T.encode(41.100050, -73.500050)


def test_points_hundreds_of_metres_apart_do_not():
    assert T.encode(41.100000, -73.500000) != T.encode(41.104000, -73.500000)


def test_bbox_contains_its_own_centre():
    tile = T.encode(41.1, -73.5)
    south, west, north, east = T.bbox(tile)
    lat, lon = T.center(tile)
    assert south < lat < north
    assert west < lon < east
    assert T.encode(lat, lon) == tile


def test_bbox_is_about_150_m_tall():
    south, _, north, _ = T.bbox(T.encode(41.1, -73.5))
    assert 130.0 < (north - south) * 111320.0 < 170.0


import fec_control as F


def _store(**kw):
    return T.TileStore(**kw)


def test_one_pass_does_not_actuate():
    s = _store()
    s.observe("dr79z6n", {"wan1": 9.0}, None, now_mono=0.0, now_wall=1000.0)
    s.observe("dr79z6n", {"wan1": 9.0}, None, now_mono=1.0, now_wall=1001.0)
    s.close_pass(now_wall=1002.0)
    assert s.passes_for("dr79z6n", "wan1") == 1
    assert s.level_for("dr79z6n", "wan1", F.DEFAULT_LOSS_TABLE) == 0


def test_third_pass_actuates():
    s = _store()
    for i in range(3):
        s.observe("dr79z6n", {"wan1": 9.0}, None, now_mono=100.0 * i, now_wall=1000.0 + i)
        s.close_pass(now_wall=1000.0 + i)
    assert s.passes_for("dr79z6n", "wan1") == 3
    # 9% loss sits in the 8:6 band of the default table.
    assert s.level_for("dr79z6n", "wan1", F.DEFAULT_LOSS_TABLE) == 3


def test_a_long_dwell_in_one_tile_is_still_one_pass():
    s = _store(pass_gap_s=30.0)
    for i in range(50):
        s.observe("dr79z6n", {"wan1": 9.0}, None, now_mono=float(i), now_wall=1000.0 + i)
    s.close_pass(now_wall=1100.0)
    assert s.passes_for("dr79z6n", "wan1") == 1


def test_a_tile_change_closes_the_pass():
    s = _store()
    s.observe("dr79z6n", {"wan1": 9.0}, None, now_mono=0.0, now_wall=1000.0)
    s.observe("dr79z6y", {"wan1": 9.0}, None, now_mono=1.0, now_wall=1001.0)
    assert s.passes_for("dr79z6n", "wan1") == 1


def test_a_fix_gap_closes_the_pass_even_in_the_same_tile():
    s = _store(pass_gap_s=30.0)
    s.observe("dr79z6n", {"wan1": 9.0}, None, now_mono=0.0, now_wall=1000.0)
    s.observe("dr79z6n", {"wan1": 9.0}, None, now_mono=120.0, now_wall=1120.0)
    s.close_pass(now_wall=1121.0)
    assert s.passes_for("dr79z6n", "wan1") == 2


def test_pass_value_is_p90_which_is_the_max_below_ten_samples():
    s = _store()
    for i, loss in enumerate([0.0, 0.0, 9.0]):
        s.observe("dr79z6n", {"wan1": loss}, None, now_mono=float(i), now_wall=1000.0 + i)
    s.close_pass(now_wall=1003.0)
    assert abs(s.tiles["dr79z6n"]["wan1"]["ewma_loss"] - 9.0) < 1e-9


def test_learning_is_per_wan():
    s = _store()
    for i in range(3):
        s.observe("dr79z6n", {"wan1": 9.0, "wan2": 0.0}, None,
                  now_mono=100.0 * i, now_wall=1000.0 + i)
        s.close_pass(now_wall=1000.0 + i)
    assert s.level_for("dr79z6n", "wan1", F.DEFAULT_LOSS_TABLE) == 3
    assert s.level_for("dr79z6n", "wan2", F.DEFAULT_LOSS_TABLE) == 0


def test_clean_passes_decay_a_learned_level_to_zero():
    s = _store(alpha=0.35)
    for i in range(3):
        s.observe("dr79z6n", {"wan1": 8.0}, None, now_mono=100.0 * i, now_wall=1000.0 + i)
        s.close_pass(now_wall=1000.0 + i)
    assert s.level_for("dr79z6n", "wan1", F.DEFAULT_LOSS_TABLE) == 3
    levels = []
    for i in range(7):
        s.observe("dr79z6n", {"wan1": 0.0}, None, now_mono=1000.0 + 100.0 * i,
                  now_wall=2000.0 + i)
        s.close_pass(now_wall=2000.0 + i)
        levels.append(s.level_for("dr79z6n", "wan1", F.DEFAULT_LOSS_TABLE))
    assert levels == sorted(levels, reverse=True)   # never climbs on clean data
    assert levels[-1] == 0                          # seven clean passes clear it


def test_prune_drops_stale_and_clean_tiles():
    s = _store(max_age_days=14.0, clean_drop_days=7.0)
    for i in range(3):
        s.observe("dr79z6n", {"wan1": 9.0}, None, now_mono=100.0 * i, now_wall=1000.0 + i)
        s.close_pass(now_wall=1000.0 + i)
        s.observe("dr79z6y", {"wan1": 0.0}, None, now_mono=1000.0 + 100.0 * i,
                  now_wall=1000.0 + i)
        s.close_pass(now_wall=1000.0 + i)
    dropped = s.prune(now_wall=1000.0 + 8 * 86400.0)
    assert "dr79z6y" not in s.tiles      # clean for more than clean_drop_days
    assert "dr79z6n" in s.tiles          # lossy, and not yet max_age_days old
    assert dropped == 1
    s.prune(now_wall=1000.0 + 15 * 86400.0)
    assert s.tiles == {}                 # everything is now past max_age_days


def test_prune_evicts_least_recently_seen_over_max_tiles():
    s = _store(max_tiles=2)
    for n, tile in enumerate(["dr79z6n", "dr79z6y", "dr79z6p"]):
        s.observe(tile, {"wan1": 9.0}, None, now_mono=100.0 * n, now_wall=1000.0 + n)
        s.close_pass(now_wall=1000.0 + n)
    s.prune(now_wall=1010.0)
    assert set(s.tiles) == {"dr79z6y", "dr79z6p"}


def test_residual_is_recorded_but_never_changes_a_level():
    s = _store()
    for i in range(3):
        s.observe("dr79z6n", {"wan1": 0.0}, 25.0, now_mono=100.0 * i, now_wall=1000.0 + i)
        s.close_pass(now_wall=1000.0 + i)
    assert s.residual["dr79z6n"]["ewma"] > 0.0
    assert s.level_for("dr79z6n", "wan1", F.DEFAULT_LOSS_TABLE) == 0


def test_no_fix_does_not_open_or_close_a_pass():
    s = _store()
    s.observe("dr79z6n", {"wan1": 9.0}, None, now_mono=0.0, now_wall=1000.0)
    s.observe(None, {"wan1": 9.0}, None, now_mono=1.0, now_wall=1001.0)
    s.observe("dr79z6n", {"wan1": 9.0}, None, now_mono=2.0, now_wall=1002.0)
    s.close_pass(now_wall=1003.0)
    assert s.passes_for("dr79z6n", "wan1") == 1


def test_round_trip_through_save_and_load(tmp_path):
    s = _store()
    for i in range(3):
        s.observe("dr79z6n", {"wan1": 9.0}, 2.0, now_mono=100.0 * i, now_wall=1000.0 + i)
        s.close_pass(now_wall=1000.0 + i)
    p = tmp_path / "store.json"
    s.save(str(p))
    back = T.TileStore.load(str(p))
    assert back.level_for("dr79z6n", "wan1", F.DEFAULT_LOSS_TABLE) == 3
    assert back.passes_for("dr79z6n", "wan1") == 3


def test_a_corrupt_store_loads_empty(tmp_path):
    p = tmp_path / "store.json"
    p.write_text("{not json")
    back = T.TileStore.load(str(p))
    assert back.tiles == {}


def test_a_missing_store_loads_empty(tmp_path):
    back = T.TileStore.load(str(tmp_path / "absent.json"))
    assert back.tiles == {}


def test_a_schema_malformed_store_loads_empty_and_does_not_crash_later():
    s = T.TileStore.from_dict({"tiles": {"dr79z6n": {"wan1": "not-a-dict"}}})
    assert s.level_for("dr79z6n", "wan1", F.DEFAULT_LOSS_TABLE) == 0
    assert s.passes_for("dr79z6n", "wan1") == 0
    assert "dr79z6n" not in s.tiles


def test_a_wrong_shape_store_logs_and_loads_empty(tmp_path, caplog):
    p = tmp_path / "store.json"
    p.write_text("[1, 2, 3]")
    with caplog.at_level("WARNING", logger="tile_store"):
        back = T.TileStore.load(str(p))
    assert back.tiles == {}
    records = [r for r in caplog.records if r.name == "tile_store"]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"


def test_from_dict_keeps_valid_entries_beside_malformed_ones():
    raw = {
        "tiles": {
            "dr79z6n": {"wan1": {"passes": 3, "ewma_loss": 9.0, "last_seen": 1000.0}},
            "dr79z6y": {"wan1": "not-a-dict"},
        }
    }
    s = T.TileStore.from_dict(raw)
    assert s.tiles["dr79z6n"]["wan1"]["passes"] == 3
    assert abs(s.tiles["dr79z6n"]["wan1"]["ewma_loss"] - 9.0) < 1e-9
    assert "dr79z6y" not in s.tiles


def test_from_dict_drops_a_non_finite_number_and_keeps_the_good_sibling():
    """NaN and inf are floats, so every isinstance guard waves them through —
    and json.dumps then writes a bare NaN token that JSON.parse rejects, so one
    poisoned value costs the map its ENTIRE payload. Drop them where they enter
    the process, which is the only place that covers every reader."""
    import json
    raw = json.loads("""
        {"tiles": {"dr79z6n": {"wan1": {"passes": 4, "ewma_loss": NaN,
                                        "last_seen": 1000.0},
                               "wan2": {"passes": 3, "ewma_loss": 6.0,
                                        "last_seen": 1000.0}},
                   "dr79z6p": {"wan1": {"passes": 3, "ewma_loss": 6.0,
                                        "last_seen": Infinity}}},
         "residual": {"dr79z6n": {"ewma": -Infinity, "last_seen": 1000.0},
                      "dr79z6p": {"ewma": 2.5, "last_seen": 1000.0}}}""")
    s = T.TileStore.from_dict(raw)
    assert "wan1" not in s.tiles["dr79z6n"]           # NaN loss dropped
    assert s.tiles["dr79z6n"]["wan2"]["passes"] == 3  # good sibling survives
    assert "dr79z6p" not in s.tiles                   # inf last_seen dropped
    assert "dr79z6n" not in s.residual                # -inf residual dropped
    assert s.residual["dr79z6p"]["ewma"] == 2.5
    json.dumps(s.to_dict(), allow_nan=False)          # what the browser must parse


def test_finite_number_survives_an_int_too_large_for_a_float():
    """math.isfinite converts to float first, so a pathological integer raises
    OverflowError rather than answering. That would escape from_dict, observe
    and read_state — a raise out of the daemon loop over a value we were about
    to reject anyway, which is the fail-open posture inverted."""
    assert T.finite_number(10 ** 5000) is False
    assert T.finite_number(-(10 ** 5000)) is False
    # The ordinary large-but-representable case is unaffected.
    assert T.finite_number(10 ** 300) is True


def test_from_dict_drops_an_int_too_large_for_a_float():
    s = T.TileStore.from_dict({"tiles": {"dr79z6n": {
        "wan1": {"passes": 4, "ewma_loss": 10 ** 5000, "last_seen": 1000.0},
        "wan2": {"passes": 3, "ewma_loss": 6.0, "last_seen": 1000.0}}}})
    assert "wan1" not in s.tiles["dr79z6n"]
    assert s.tiles["dr79z6n"]["wan2"]["passes"] == 3


def test_observe_ignores_a_non_finite_sample():
    # The other boundary a number can enter the store by. A NaN folded into a
    # pass makes that tile's EWMA NaN for good.
    s = T.TileStore()
    s.observe("dr79z6n", {"wan1": float("nan"), "wan2": 6.0},
              float("inf"), now_mono=0.0, now_wall=1000.0)
    s.close_pass(now_wall=1000.0)
    assert "wan1" not in s.tiles["dr79z6n"]
    assert s.tiles["dr79z6n"]["wan2"]["ewma_loss"] == 6.0
    assert s.residual == {}


def test_store_rejects_a_non_finite_alpha():
    # NaN poisons the EWMA permanently and silently: every subsequent blend is
    # NaN, so a tile can never confirm again. Refuse it where it enters.
    import pytest
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="alpha"):
            T.TileStore(alpha=bad)
