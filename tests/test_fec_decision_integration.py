import sbfd_ctl as M
import fec_control as F

def _cfg_with_fec():
    return M.FecCfg(enabled=True, fifo="/tmp/none.fifo", loss_table=F.DEFAULT_LOSS_TABLE,
                    ramp_up_ticks=2, ramp_down_hold_s=20.0,
                    full_mode_backoff_fec="8:0", full_min_up_wans=2)

def test_compute_fec_target_full_mode_two_up_backs_off():
    assert M.compute_fec_target(_cfg_with_fec(), "full",
        {"wan1": "UP", "wan2": "UP"}, {"wan1": 9.0, "wan2": 9.0}, {"wan1", "wan2"}) == 0

def test_compute_fec_target_master_backup_uses_active_loss():
    assert M.compute_fec_target(_cfg_with_fec(), "master_backup",
        {"wan1": "UP", "wan2": "UP"}, {"wan1": 0.0, "wan2": 6.0}, {"wan2"}) == 3   # 8:6

def test_compute_fec_target_single_up_in_full_still_scales():
    assert M.compute_fec_target(_cfg_with_fec(), "full",
        {"wan1": "DOWN", "wan2": "UP"}, {"wan1": 0.0, "wan2": 3.0}, {"wan2"}) == 2  # 8:4


# ---------- per-WAN FEC profiles: pure resolution ----------

def _fec_cfg_with_profile():
    cfg = _cfg_with_fec()
    cfg.wan_profiles = {"wan1": M.WanProfileCfg(
        name="wan1", loss_table=F.DEFAULT_CELL_LOSS_TABLE,
        ramp_up_ticks=1, ramp_down_hold_s=60.0,
        floor_ratio="8:0", signal_floor_fec="12:1")}
    return cfg


def test_resolve_fec_profile_selects_by_driver():
    fc = _fec_cfg_with_profile()
    name, table, hyst, floor, sf = M.resolve_fec_profile(fc, "wan1")
    assert name == "wan1" and table is fc.wan_profiles["wan1"].loss_table
    assert hyst.ramp_up_ticks == 1 and hyst.ramp_down_hold_s == 60.0
    assert floor == "8:0" and sf == "12:1"
    name, table, hyst, floor, sf = M.resolve_fec_profile(fc, "wan2")
    assert name == "default" and table is fc.loss_table
    assert hyst.ramp_up_ticks == 2 and floor is None
    assert M.resolve_fec_profile(fc, None)[0] == "default"


def test_effective_floor_precedence_profile_beats_config_not_operator(tmp_path):
    fec = _cfg_with_fec()
    fec.floor_ratio = "20:1"
    cfg = M.Config(
        wans={"wan1": M.WanCfg("wan1", 1, "T-Mo"),
              "wan2": M.WanCfg("wan2", 2, "Satellite")},
        relay=M.RelayCfg("http://x"),
        engarde=M.EngardeCfg("198.51.100.10", 59402),
        nft=M.NftCfg(),
        policy=M.PolicyCfg(),
        ui_listen="127.0.0.1:0",
        sbfd_local_state=str(tmp_path / "sbfd-state.json"),
        runtime_state=str(tmp_path / "runtime.json"),
        persist_state=str(tmp_path / "persist.json"),
        published_state=str(tmp_path / "published.json"),
        fec=fec,
    )
    ov = M.RuntimeOverlay()
    assert M.effective_fec_floor_ratio(cfg, ov) == "20:1"
    assert M.effective_fec_floor_ratio(cfg, ov, profile_floor="8:0") == "8:0"
    ov.fec_floor_ratio = "8:2"                  # operator runtime override
    assert M.effective_fec_floor_ratio(cfg, ov, profile_floor="8:0") == "8:2"


def test_compute_fec_target_accepts_table_override():
    fc = _fec_cfg_with_profile()
    eff = {"wan1": "UP", "wan2": "UP"}
    loss = {"wan1": 3.0, "wan2": 0.0}
    lvl = M.compute_fec_target(fc, "master_backup", eff, loss, {"wan1"},
                               loss_table=F.DEFAULT_CELL_LOSS_TABLE)
    assert F.level_to_ratio(lvl, F.DEFAULT_CELL_LOSS_TABLE) == "12:1"


def test_cell_profile_pipeline_signal_floor_lifts_zero_parity():
    fc = _fec_cfg_with_profile()
    name, table, hyst, prof_floor, sf_fec = M.resolve_fec_profile(fc, "wan1")
    sfl = F.SignalFloor()
    # good signal, no loss -> 8:0
    target = M.compute_fec_target(fc, "master_backup",
                                  {"wan1": "UP", "wan2": "UP"},
                                  {"wan1": 0.0, "wan2": 0.0}, {"wan1"},
                                  loss_table=table)
    rt = F.FecRuntime(0, 0, 0.0)
    rt, _ = F.step_level(target, rt, hyst, now=1.0)
    engaged = sfl.update(rsrq=-9.0, rsrp=None)
    lvl = F.apply_signal_floor(rt.current_level, engaged, table, sf_fec)
    assert F.level_to_ratio(lvl, table) == "8:0"
    # RSRQ collapses with zero measured loss -> floor to 12:1 in ONE tick
    engaged = sfl.update(rsrq=-13.0, rsrp=None)
    lvl = F.apply_signal_floor(rt.current_level, engaged, table, sf_fec)
    assert F.level_to_ratio(lvl, table) == "12:1"
    # loss also rises -> table wins over the floor when higher
    target = M.compute_fec_target(fc, "master_backup",
                                  {"wan1": "UP", "wan2": "UP"},
                                  {"wan1": 8.0, "wan2": 0.0}, {"wan1"},
                                  loss_table=table)
    rt, _ = F.step_level(target, rt, hyst, now=2.0)   # ramp_up_ticks=1
    lvl = F.apply_signal_floor(rt.current_level, engaged, table, sf_fec)
    assert F.level_to_ratio(lvl, table) == "8:1"      # capped ladder


def test_profile_switch_translates_level():
    # ratio carried across a table swap: 8:2 exists only in the default table
    assert F.ratio_to_level("8:2", F.DEFAULT_CELL_LOSS_TABLE) == 0  # unknown -> 0
    assert F.ratio_to_level("20:1", F.DEFAULT_CELL_LOSS_TABLE) == 1


# ---------- full-mode backoff must win over the signal floor ----------
# (engarde is already duplicating in full mode with >=2 WANs up; parity must
# never stack on top of that duplication — the signal floor is suppressed.)

def _gated_ratio(mode, fc, table, hyst, sf_fec, engaged):
    """Mirror the tick's fec_full_backoff / fec_signal_floor_applied gate,
    then run compute_fec_target -> step_level -> apply_signal_floor exactly
    like the run_loop's cellular-profile pipeline does."""
    eff = {"wan1": "UP", "wan2": "UP"}
    loss = {"wan1": 0.0, "wan2": 0.0}
    active = {"wan1", "wan2"}
    target = M.compute_fec_target(fc, mode, eff, loss, active, loss_table=table)
    rt = F.FecRuntime(0, 0, 0.0)
    rt, _ = F.step_level(target, rt, hyst, now=1.0)
    fec_full_backoff = (mode == "full" and
                        sum(1 for st in eff.values() if st == "UP")
                        >= fc.full_min_up_wans)
    applied = engaged and not fec_full_backoff
    lvl = F.apply_signal_floor(rt.current_level, applied, table, sf_fec)
    return F.level_to_ratio(lvl, table)


def test_full_mode_backoff_suppresses_signal_floor_stacking():
    fc = _fec_cfg_with_profile()
    name, table, hyst, prof_floor, sf_fec = M.resolve_fec_profile(fc, "wan1")
    sfl = F.SignalFloor()
    engaged = sfl.update(rsrq=-13.0, rsrp=None)   # engage the floor
    assert engaged is True
    # full mode, 2 WANs UP: full-mode backoff (8:0) wins, floor must not stack
    assert _gated_ratio("full", fc, table, hyst, sf_fec, engaged) == "8:0"


def test_master_backup_signal_floor_still_applies():
    fc = _fec_cfg_with_profile()
    name, table, hyst, prof_floor, sf_fec = M.resolve_fec_profile(fc, "wan1")
    sfl = F.SignalFloor()
    engaged = sfl.update(rsrq=-13.0, rsrp=None)   # engage the floor
    assert engaged is True
    # same telemetry/loss, but master_backup: the gate is full-mode-specific,
    # so the signal floor still lifts the level to 12:1.
    assert _gated_ratio("master_backup", fc, table, hyst, sf_fec, engaged) == "12:1"


def test_apply_handoff_window_guards():
    w = M.HandoffWindow(reason="cell_change:1->2", set_ts=0.0, until_ts=4.0)
    assert M.apply_handoff_window("master_backup", w, "wan1", {"wan1"}) == ("full", w)
    # wan1 not active -> inert
    assert M.apply_handoff_window("master_backup", w, "wan1", {"wan2"}) == \
        ("master_backup", None)
    # already full -> inert (no stacking, no bookkeeping)
    assert M.apply_handoff_window("full", w, "wan1", {"wan1"}) == ("full", None)
    # no window -> passthrough
    assert M.apply_handoff_window("master_backup", None, "wan1", {"wan1"}) == \
        ("master_backup", None)
    # no cell wan configured -> inert
    assert M.apply_handoff_window("master_backup", w, None, {"wan1"}) == \
        ("master_backup", None)
