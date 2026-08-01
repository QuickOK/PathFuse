import fec_control as F


def test_parse_ratio():
    assert F.parse_ratio("8:2") == (8, 2)
    assert F.parse_ratio("1:0") == (1, 0)


def test_format_ratio():
    assert F.format_ratio(8, 2) == "8:2"


def test_validate_ratio_bounds():
    assert F.validate_ratio(1, 0) is True
    assert F.validate_ratio(8, 8) is True
    assert F.validate_ratio(0, 1) is False        # a >= 1 required
    assert F.validate_ratio(1, -1) is False        # b >= 0 required
    assert F.validate_ratio(200, 60) is False      # a + b <= 254


def test_loss_to_level_default_table():
    assert F.loss_to_level(0.0) == 0       # 1:0 (off)
    assert F.loss_to_level(0.5) == 0       # boundary inclusive
    assert F.loss_to_level(1.0) == 1       # 8:2
    assert F.loss_to_level(4.9) == 2       # 8:4
    assert F.loss_to_level(8.0) == 3       # 8:6
    assert F.loss_to_level(50.0) == 4      # 8:8
    assert F.loss_to_level(999.0) == 4     # clamps to last row


def test_level_to_ratio_default_table():
    assert F.level_to_ratio(0) == "8:0"    # off tier: x=8 keeps mode-0 splitter
    assert F.level_to_ratio(4) == "8:8"
    assert F.level_to_ratio(99) == "8:8"   # clamps high
    assert F.level_to_ratio(-3) == "8:0"   # clamps low


def test_ratio_to_level():
    assert F.ratio_to_level("8:0") == 0
    assert F.ratio_to_level("8:6") == 3
    assert F.ratio_to_level("nonsense") == 0   # unknown -> 0 (safe/off)


def test_is_level_at_max_uses_the_given_table_not_a_fixed_length():
    # Default (5-row) table: max index is 4.
    assert F.is_level_at_max(3) is False
    assert F.is_level_at_max(4) is True
    # A shorter (4-row) cellular-style profile table: max index is 3, so a
    # level of 3 must already read as "at max" against THAT table, even
    # though it would read as "not at max" against the 5-row default table.
    short_table = F.DEFAULT_LOSS_TABLE[:4]
    assert F.is_level_at_max(3, short_table) is True
    assert F.is_level_at_max(2, short_table) is False


def test_mode_aware_backoff_in_full_with_two_up():
    assert F.mode_aware_level("full", up_count=2, loss_pct=9.0) == 0


def test_mode_aware_no_backoff_when_single_up_even_in_full():
    assert F.mode_aware_level("full", up_count=1, loss_pct=9.0) == 3   # 8:6


def test_mode_aware_master_backup_uses_loss_table():
    assert F.mode_aware_level("master_backup", up_count=2, loss_pct=3.0) == 2  # 8:4
    assert F.mode_aware_level("master_backup", up_count=1, loss_pct=0.0) == 0


def test_ramp_up_requires_consecutive_ticks():
    hyst = F.FecHysteresis(ramp_up_ticks=2, ramp_down_hold_s=20.0)
    rt = F.FecRuntime(current_level=0, up_streak=0, last_change_ts=0.0)
    rt, changed = F.step_level(2, rt, hyst, now=100.0)
    assert changed is False and rt.current_level == 0 and rt.up_streak == 1
    rt, changed = F.step_level(2, rt, hyst, now=100.5)
    assert changed is True and rt.current_level == 2 and rt.last_change_ts == 100.5


def test_ramp_up_streak_resets_when_target_drops_to_current():
    hyst = F.FecHysteresis(ramp_up_ticks=2, ramp_down_hold_s=20.0)
    rt = F.FecRuntime(current_level=0, up_streak=1, last_change_ts=0.0)
    rt, changed = F.step_level(0, rt, hyst, now=10.0)
    assert changed is False and rt.up_streak == 0


def test_ramp_down_held_until_hold_elapses():
    hyst = F.FecHysteresis(ramp_up_ticks=2, ramp_down_hold_s=20.0)
    rt = F.FecRuntime(current_level=3, up_streak=0, last_change_ts=100.0)
    rt2, changed = F.step_level(0, rt, hyst, now=110.0)
    assert changed is False and rt2.current_level == 3
    rt3, changed = F.step_level(0, rt, hyst, now=120.0)
    assert changed is True and rt3.current_level == 0 and rt3.last_change_ts == 120.0


import os, tempfile


def test_fifo_command():
    assert F.fifo_command("8:2") == "fec 8:2\n"


def test_write_fifo_succeeds_with_reader():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "f.fifo")
    os.mkfifo(p)
    rfd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
    try:
        assert F.write_fifo(p, "8:4") is True
        data = os.read(rfd, 64).decode()
        assert data == "fec 8:4\n"
    finally:
        os.close(rfd)


def test_write_fifo_returns_false_with_no_reader():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "f.fifo")
    os.mkfifo(p)
    assert F.write_fifo(p, "8:4") is False


def test_write_fifo_returns_false_when_path_missing():
    assert F.write_fifo("/nonexistent/dir/f.fifo", "1:0") is False


# ---------- tri/quad-state FEC mode ----------

def test_modes_exposed():
    assert F.ALL_MODES == (F.MODE_OFF, F.MODE_FIXED, F.MODE_ADAPTIVE, F.MODE_MIN_ADAPTIVE)
    assert F.DEFAULT_MODE == F.MODE_MIN_ADAPTIVE


def test_apply_mode_off_forces_off_ratio():
    assert F.apply_mode(F.MODE_OFF, "8:6", fixed_ratio="20:1") == "8:0"


def test_apply_mode_fixed_returns_fixed_ratio():
    assert F.apply_mode(F.MODE_FIXED, "8:4", fixed_ratio="20:1") == "20:1"
    assert F.apply_mode(F.MODE_FIXED, "8:0", fixed_ratio="8:2") == "8:2"


def test_apply_mode_adaptive_passes_through():
    assert F.apply_mode(F.MODE_ADAPTIVE, "8:0") == "8:0"
    assert F.apply_mode(F.MODE_ADAPTIVE, "8:4") == "8:4"


def test_apply_mode_min_adaptive_lifts_idle_to_floor():
    assert F.apply_mode(F.MODE_MIN_ADAPTIVE, "8:0", floor_ratio="20:1") == "20:1"
    # Above idle, min_adaptive behaves like adaptive.
    assert F.apply_mode(F.MODE_MIN_ADAPTIVE, "8:4", floor_ratio="20:1") == "8:4"


def test_apply_mode_unknown_passes_through_adaptive():
    # Defensive: an unknown mode shouldn't accidentally force off.
    assert F.apply_mode("nonsense", "8:2") == "8:2"


def test_normalize_mode_accepts_valid_and_falls_back():
    assert F.normalize_mode(F.MODE_OFF) == F.MODE_OFF
    assert F.normalize_mode(F.MODE_FIXED) == F.MODE_FIXED
    assert F.normalize_mode("garbage") == F.DEFAULT_MODE
    assert F.normalize_mode(None) == F.DEFAULT_MODE


def test_resolve_ratio_passes_through_explicit_ratios():
    for text, expected in [("8:2", "8:2"), (" 8:2 ", "8:2"), ("20:1", "20:1"),
                           ("16:3", "16:3"), ("8:0", "8:0")]:
        assert F.resolve_ratio(text) == expected


def test_resolve_ratio_snaps_percent_to_ladder():
    for text, expected in [("0%", "8:0"), ("5%", "20:1"), ("5", "20:1"),
                           ("12.5%", "8:1"), ("25%", "8:2"), ("50%", "8:4"),
                           ("75%", "8:6"), ("100%", "8:8"), ("4%", "20:1"),
                           ("30%", "8:2")]:
        assert F.resolve_ratio(text) == expected, text


def test_resolve_ratio_breaks_ties_upward():
    # 8.75% is closer to 12:1 (8.33%) than to 8:1 (12.5%), so it snaps to 12:1.
    # The tie-breaking rule (<=) ensures an operator asking for protection never
    # gets less than they typed (when there's a true tie, prefer higher overhead).
    assert F.resolve_ratio("8.75%") == "12:1"
    # 2.5% is the midpoint between 8:0 (0%) and 20:1 (5%).
    assert F.resolve_ratio("2.5%") == "20:1"


def test_resolve_ratio_rejects_bad_input():
    for text in ["abc", "", "   ", "-5%", "200%", "0:1", "200:200",
                 "1:2:3", "8:", ":2", "8:x"]:
        try:
            F.resolve_ratio(text)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {text!r}")


def test_resolve_ratio_rejects_non_strings():
    for text in [None, 5, 5.0, True, ["8:2"]]:
        try:
            F.resolve_ratio(text)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {text!r}")


def test_ratio_overhead_pct():
    assert F.ratio_overhead_pct(20, 1) == 5.0
    assert F.ratio_overhead_pct(8, 0) == 0.0
    assert F.ratio_overhead_pct(8, 8) == 100.0


def test_ratio_ladder_is_ascending_and_contains_off():
    pcts = [F.ratio_overhead_pct(*F.parse_ratio(r)) for r in F.RATIO_LADDER]
    assert pcts == sorted(pcts)
    assert F.OFF_RATIO in F.RATIO_LADDER


def test_min_adaptive_floor_holds_against_every_lower_tier():
    # The bug this guards: apply_mode used to lift only the 8:0 idle tier, so a
    # floor above the lowest non-off rung was silently ignored.
    assert F.apply_mode("min_adaptive", "8:2", floor_ratio="8:4") == "8:4"
    assert F.apply_mode("min_adaptive", "8:0", floor_ratio="8:4") == "8:4"
    assert F.apply_mode("min_adaptive", "20:1", floor_ratio="8:1") == "8:1"


def test_min_adaptive_keeps_adaptive_when_already_above_floor():
    assert F.apply_mode("min_adaptive", "8:6", floor_ratio="8:2") == "8:6"
    assert F.apply_mode("min_adaptive", "8:8", floor_ratio="20:1") == "8:8"


def test_min_adaptive_equal_overhead_keeps_adaptive():
    assert F.apply_mode("min_adaptive", "8:2", floor_ratio="8:2") == "8:2"


def test_min_adaptive_survives_a_malformed_floor():
    # A hand-edited overlay must not take down the control loop.
    assert F.apply_mode("min_adaptive", "8:4", floor_ratio="garbage") == "8:4"


def test_other_modes_ignore_the_floor():
    assert F.apply_mode("off", "8:4", floor_ratio="8:8") == "8:0"
    assert F.apply_mode("fixed", "8:4", fixed_ratio="8:1", floor_ratio="8:8") == "8:1"
    assert F.apply_mode("adaptive", "8:0", floor_ratio="8:8") == "8:0"


def test_safe_ratio_coerces_normalizes_and_falls_back():
    assert F.safe_ratio("25%", "20:1") == "8:2"
    assert F.safe_ratio("8:2", "20:1") == "8:2"
    assert F.safe_ratio("garbage", "20:1") == "20:1"
    assert F.safe_ratio(None, "20:1") == "20:1"
    assert F.safe_ratio("", "20:1") == "20:1"


def test_safe_ratio_warns_only_once_per_bad_value():
    class _Log:
        def __init__(self): self.n = 0
        def warning(self, *a, **k): self.n += 1
    log = _Log()
    F._warned_ratios.discard("still-garbage")
    for _ in range(5):
        assert F.safe_ratio("still-garbage", "20:1", log) == "20:1"
    assert log.n == 1   # a sub-second control loop must not spam the journal


def test_safe_ratio_survives_unhashable_garbage():
    # A hand-edited JSON file can yield a list/dict. The fallback path must not
    # raise from its own debounce — that would defeat the whole point.
    class _Log:
        def __init__(self): self.n = 0
        def warning(self, *a, **k): self.n += 1
    log = _Log()
    assert F.safe_ratio(["8:2"], "20:1", log) == "20:1"
    assert F.safe_ratio({"a": 1}, "20:1", log) == "20:1"
    assert F.safe_ratio(17, "20:1", log) == "20:1"
    assert log.n == 3


def test_ratio_ladder_has_12_1_in_ascending_overhead_order():
    overheads = [F.ratio_overhead_pct(*F.parse_ratio(r)) for r in F.RATIO_LADDER]
    assert overheads == sorted(overheads)
    assert "12:1" in F.RATIO_LADDER


def test_resolve_ratio_percent_snaps_to_12_1():
    assert F.resolve_ratio("8.3%") == "12:1"   # 12:1 ≈ 8.33%
    assert F.resolve_ratio("12:1") == "12:1"


def test_fixed_presets_offer_12_1():
    assert "12:1" in F.FIXED_RATIO_PRESETS
    assert "8:0" not in F.FIXED_RATIO_PRESETS  # off stays a mode, not a preset


def test_cell_loss_table_tiers():
    t = F.DEFAULT_CELL_LOSS_TABLE
    assert [r["fec"] for r in t] == ["8:0", "20:1", "12:1", "8:1"]
    assert F.level_to_ratio(F.loss_to_level(0.2, t), t) == "8:0"
    assert F.level_to_ratio(F.loss_to_level(1.0, t), t) == "20:1"
    assert F.level_to_ratio(F.loss_to_level(3.0, t), t) == "12:1"
    assert F.level_to_ratio(F.loss_to_level(50.0, t), t) == "8:1"   # cap


# ---------- hysteretic signal floor ----------

def test_signal_floor_rsrq_hysteresis():
    sf = F.SignalFloor()
    assert sf.update(rsrq=-11.0, rsrp=None) is False   # above degrade
    assert sf.update(rsrq=-12.5, rsrp=None) is True    # < -12 -> engage
    assert sf.update(rsrq=-11.0, rsrp=None) is True    # in band -> hold
    assert sf.update(rsrq=-9.5, rsrp=None) is False    # >= -10 -> release


def test_signal_floor_rsrp_only_when_rsrq_absent():
    sf = F.SignalFloor()
    assert sf.update(rsrq=None, rsrp=-115.0) is True   # secondary engages
    assert sf.update(rsrq=-9.0, rsrp=-115.0) is False  # rsrq present: it rules
    sf2 = F.SignalFloor()
    assert sf2.update(rsrq=None, rsrp=-115.0) is True
    assert sf2.update(rsrq=None, rsrp=-109.0) is True   # in band -> hold
    assert sf2.update(rsrq=None, rsrp=-107.0) is False  # >= -108 -> release


def test_signal_floor_no_data_disengages():
    sf = F.SignalFloor()
    sf.update(rsrq=-13.0, rsrp=None)
    assert sf.update(rsrq=None, rsrp=None) is False    # fail-open


def test_apply_signal_floor_lifts_to_12_1_rung():
    t = F.DEFAULT_CELL_LOSS_TABLE
    assert F.apply_signal_floor(0, True, t) == 2       # 8:0 -> 12:1 rung
    assert F.apply_signal_floor(3, True, t) == 3       # never lowers
    assert F.apply_signal_floor(0, False, t) == 0
    # floor ratio absent from the table -> no-op, never raises
    assert F.apply_signal_floor(1, True, F.DEFAULT_LOSS_TABLE) == 1


# ---------------------------------------------------------------------------
# Ladder view (UI pip row): where the applied ratio sits relative to the floor,
# on the rungs the ACTIVE profile actually has.
# ---------------------------------------------------------------------------

def test_ratio_rung_exact_rungs():
    assert F.ratio_rung("8:0") == 0
    assert F.ratio_rung("8:2") == 1
    assert F.ratio_rung("8:8") == 4


def test_ratio_rung_rounds_down_between_rungs():
    # 20:1 is 5% overhead: above the base table's 8:0 (0%) but well under its
    # 8:2 (25%). It must not be credited with the higher rung's protection.
    assert F.ratio_rung("20:1") == 0
    # Same ratio IS a rung of the cellular table.
    assert F.ratio_rung("20:1", F.DEFAULT_CELL_LOSS_TABLE) == 1
    assert F.ratio_rung("12:1", F.DEFAULT_CELL_LOSS_TABLE) == 2
    assert F.ratio_rung("8:1", F.DEFAULT_CELL_LOSS_TABLE) == 3
    # Above every cellular rung -> clamps to the top one.
    assert F.ratio_rung("8:8", F.DEFAULT_CELL_LOSS_TABLE) == 3


def test_ratio_rung_unusable_input_is_rung_zero():
    for bad in ("", "nonsense", None, 7, "0:2"):
        assert F.ratio_rung(bad) == 0


def test_ladder_state_min_adaptive_base_table():
    # Floor 20:1 sits on rung 0 of the base table, so all four rungs above the
    # idle tier are available and the floor itself lights nothing.
    lad = F.ladder_state(F.MODE_MIN_ADAPTIVE, "20:1", "20:1")
    assert lad == {"levels": 5, "floor_level": 0, "applied_level": 0}
    lad = F.ladder_state(F.MODE_MIN_ADAPTIVE, "8:4", "20:1")
    assert lad["applied_level"] == 2


def test_ladder_state_min_adaptive_cell_table():
    t = F.DEFAULT_CELL_LOSS_TABLE
    # Floor 20:1 IS rung 1 here: only 2 of the 4 rungs are above the floor.
    at_floor = F.ladder_state(F.MODE_MIN_ADAPTIVE, "20:1", "20:1", t)
    assert at_floor == {"levels": 4, "floor_level": 1, "applied_level": 1}
    top = F.ladder_state(F.MODE_MIN_ADAPTIVE, "8:1", "20:1", t)
    assert top["applied_level"] - top["floor_level"] == 2


def test_ladder_state_floor_only_counts_in_min_adaptive():
    # An adaptive/fixed/off leg has no floor holding it up: the whole ladder is
    # available even though a floor_ratio is still configured.
    for mode in (F.MODE_ADAPTIVE, F.MODE_FIXED, F.MODE_OFF):
        assert F.ladder_state(mode, "8:2", "8:4")["floor_level"] == 0


def test_ladder_state_tracks_the_applied_ratio_not_the_engine():
    # In fixed mode the adaptive engine keeps stepping, but the pip row must
    # follow the ratio actually on the wire.
    assert F.ladder_state(F.MODE_FIXED, "8:6", "20:1")["applied_level"] == 3
    assert F.ladder_state(F.MODE_OFF, "8:0", "20:1")["applied_level"] == 0


def test_ladder_state_no_ratio_yet():
    # First tick before the actuator has written anything.
    assert F.ladder_state(F.MODE_ADAPTIVE, None, "20:1")["applied_level"] == 0


def test_rung_positions_are_independent_of_row_order():
    # A loss table's rows are ordered by loss band; nothing validates that their
    # ratios ascend with them. Ladder positions must come from parity order, or
    # a hand-written table silently reports the wrong rung.
    shuffled = [
        {"max_loss_pct": 0.5,   "fec": "8:0"},
        {"max_loss_pct": 2.0,   "fec": "8:4"},
        {"max_loss_pct": 5.0,   "fec": "8:6"},
        {"max_loss_pct": 10.0,  "fec": "8:2"},
        {"max_loss_pct": 100.0, "fec": "8:8"},
    ]
    assert F.ratio_rung("8:2", shuffled) == 1
    assert F.ratio_rung("8:4", shuffled) == 2
    assert F.ratio_rung("8:6", shuffled) == 3
    lad = F.ladder_state(F.MODE_MIN_ADAPTIVE, "8:4", "8:2", shuffled)
    assert lad == {"levels": 5, "floor_level": 1, "applied_level": 2}


def test_duplicate_ratios_are_one_rung():
    # Two loss bands carrying the same parity are one level of protection.
    dupes = [
        {"max_loss_pct": 0.5,   "fec": "8:0"},
        {"max_loss_pct": 2.0,   "fec": "8:2"},
        {"max_loss_pct": 5.0,   "fec": "8:2"},
        {"max_loss_pct": 100.0, "fec": "8:4"},
    ]
    assert F.rung_overheads(dupes) == [0.0, 25.0, 50.0]
    assert F.ladder_state(F.MODE_ADAPTIVE, "8:4", "8:0", dupes)["levels"] == 3


def test_ladder_state_can_report_below_the_floor():
    # The actuator refused the write after the floor rose: the ratio on the wire
    # is the last one accepted, which is now UNDER the floor. The ladder must
    # expose that rather than clamp it into looking like "at floor".
    lad = F.ladder_state(F.MODE_MIN_ADAPTIVE, "20:1", "8:4")
    assert lad["applied_level"] < lad["floor_level"]
