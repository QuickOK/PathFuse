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
