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
