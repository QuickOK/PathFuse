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
