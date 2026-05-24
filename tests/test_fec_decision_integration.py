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
