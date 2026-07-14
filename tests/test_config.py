import json
import pytest
from pathlib import Path

import sbfd_ctl


SAMPLE = {
    "wans": {
        "wan1": {"iface": "wan1", "session_id": 1, "label": "Cellular"},
        "wan2": {"iface": "wan2", "session_id": 2, "label": "Satellite"},
    },
    "relay": {"state_url": "http://100.64.0.2:9275/state",
            "fetch_interval_s": 1.0, "fetch_timeout_s": 2.0},
    "engarde": {"server_ip": "198.51.100.10", "server_port": 59402},
    "nft": {"table": "sbfd_ctl", "family": "inet"},
    "policy": {"default_mode": "full", "default_master_policy": "static_primary",
               "default_master_wan": "wan2", "failback_hold_s": 30},
    "ui": {"listen": "0.0.0.0:8081"},
    "sbfd_local_state": "/run/sbfd/state.json",
    "runtime_state": "/run/sbfd-ctl/runtime.json",
    "persist_state": "/var/lib/sbfd-ctl/runtime.json",
    "published_state": "/run/sbfd-ctl/state.json",
}


def test_load_config_minimal(tmp_path: Path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(SAMPLE))
    cfg = sbfd_ctl.load_config(str(p))
    assert cfg.wans["wan1"].iface == "wan1"
    assert cfg.wans["wan2"].session_id == 2
    assert cfg.relay.state_url.endswith("/state")
    assert cfg.engarde.server_port == 59402
    assert cfg.policy.default_mode == "full"
    assert cfg.policy.failback_hold_s == 30
    assert cfg.ui_listen == "0.0.0.0:8081"


def test_load_config_rejects_unknown_default_mode(tmp_path: Path):
    bad = dict(SAMPLE)
    bad["policy"] = dict(SAMPLE["policy"])
    bad["policy"]["default_mode"] = "magic"
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="default_mode"):
        sbfd_ctl.load_config(str(p))


def test_load_config_rejects_master_wan_not_in_wans(tmp_path: Path):
    bad = dict(SAMPLE)
    bad["policy"] = dict(SAMPLE["policy"])
    bad["policy"]["default_master_wan"] = "wan9"
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="default_master_wan"):
        sbfd_ctl.load_config(str(p))


def test_load_config_wraps_missing_top_level_key(tmp_path: Path):
    bad = dict(SAMPLE)
    del bad["engarde"]
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="missing required key"):
        sbfd_ctl.load_config(str(p))


def test_load_config_rejects_negative_failback_hold(tmp_path: Path):
    bad = dict(SAMPLE)
    bad["policy"] = dict(SAMPLE["policy"])
    bad["policy"]["failback_hold_s"] = -1
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="failback_hold_s"):
        sbfd_ctl.load_config(str(p))


def test_load_config_rejects_zero_fetch_interval(tmp_path: Path):
    bad = dict(SAMPLE)
    bad["relay"] = dict(SAMPLE["relay"])
    bad["relay"]["fetch_interval_s"] = 0
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="fetch_interval_s"):
        sbfd_ctl.load_config(str(p))


def test_load_config_rejects_bad_engarde_port(tmp_path: Path):
    bad = dict(SAMPLE)
    bad["engarde"] = dict(SAMPLE["engarde"])
    bad["engarde"]["server_port"] = 70000
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="server_port"):
        sbfd_ctl.load_config(str(p))


def test_load_config_engarde_admin_url_default_none(tmp_path: Path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(SAMPLE))
    cfg = sbfd_ctl.load_config(str(p))
    assert cfg.engarde.admin_url is None


def test_load_config_engarde_admin_url_loaded(tmp_path: Path):
    cfg_dict = dict(SAMPLE)
    cfg_dict["engarde"] = dict(SAMPLE["engarde"])
    cfg_dict["engarde"]["admin_url"] = "http://100.64.0.2:8080/api/v1/get-list"
    p = tmp_path / "c.json"
    p.write_text(json.dumps(cfg_dict))
    cfg = sbfd_ctl.load_config(str(p))
    assert cfg.engarde.admin_url == "http://100.64.0.2:8080/api/v1/get-list"


def test_load_config_dynamic_policy_fields(tmp_path: Path):
    cfg_dict = dict(SAMPLE)
    cfg_dict["policy"] = dict(SAMPLE["policy"])
    cfg_dict["policy"]["dynamic_rtt_margin_ms"] = 40
    cfg_dict["policy"]["dynamic_swap_dwell_s"] = 30
    cfg_dict["policy"]["dynamic_loss_margin_pct"] = 1.5
    p = tmp_path / "c.json"
    p.write_text(json.dumps(cfg_dict))
    cfg = sbfd_ctl.load_config(str(p))
    assert cfg.policy.dynamic_rtt_margin_ms == 40.0
    assert cfg.policy.dynamic_swap_dwell_s == 30.0
    assert cfg.policy.dynamic_loss_margin_pct == 1.5


def test_load_config_dynamic_policy_defaults_when_absent(tmp_path: Path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(SAMPLE))
    cfg = sbfd_ctl.load_config(str(p))
    assert cfg.policy.dynamic_rtt_margin_ms == 25.0
    assert cfg.policy.dynamic_swap_dwell_s == 10.0
    assert cfg.policy.dynamic_loss_margin_pct == 1.0


@pytest.mark.parametrize("mode", ["relay_vpn", "relay_direct", "local_direct"])
def test_load_config_egress_block_parses_all_valid_modes(tmp_path: Path, mode):
    cfg_raw = dict(SAMPLE)
    cfg_raw["egress"] = {"engarde_table": "engarde_v2", "wg_iface": "wg1", "default_mode": mode}
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg_raw))
    cfg = sbfd_ctl.load_config(str(p))
    assert cfg.egress.engarde_table == "engarde_v2"
    assert cfg.egress.wg_iface == "wg1"
    assert cfg.egress.default_mode == mode


def test_load_config_egress_defaults_when_block_omitted(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(SAMPLE))
    cfg = sbfd_ctl.load_config(str(p))
    assert cfg.egress.engarde_table == "engarde"
    assert cfg.egress.wg_iface == "wg0"
    assert cfg.egress.default_mode == "relay_vpn"


def test_load_config_egress_rejects_bad_default_mode(tmp_path: Path):
    cfg_raw = dict(SAMPLE)
    cfg_raw["egress"] = {"default_mode": "banana"}
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg_raw))
    with pytest.raises(ValueError, match="default_mode"):
        sbfd_ctl.load_config(str(p))


def test_load_config_no_notifications_section(tmp_path: Path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(SAMPLE))
    cfg = sbfd_ctl.load_config(str(p))
    assert cfg.notifications is None


def test_load_config_notifications_minimal(tmp_path: Path):
    raw = dict(SAMPLE)
    raw["notifications"] = {"topic": "pathfuse"}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(raw))
    cfg = sbfd_ctl.load_config(str(p))
    assert cfg.notifications.topic == "pathfuse"
    assert cfg.notifications.min_interval_s == 30.0
    assert cfg.notifications.command == "/usr/local/sbin/spool-notify"
    assert cfg.notifications.wan_down_hold_s == 10.0
    assert cfg.notifications.fec_alerts is False


def test_load_config_notifications_full(tmp_path: Path):
    raw = dict(SAMPLE)
    raw["notifications"] = {"topic": "pathfuse", "min_interval_s": 60,
                            "command": "/tmp/fake-spool-notify",
                            "wan_down_hold_s": 25, "fec_alerts": True}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(raw))
    cfg = sbfd_ctl.load_config(str(p))
    assert cfg.notifications.min_interval_s == 60.0
    assert cfg.notifications.command == "/tmp/fake-spool-notify"
    assert cfg.notifications.wan_down_hold_s == 25.0
    assert cfg.notifications.fec_alerts is True


def test_load_config_notifications_missing_topic_raises(tmp_path: Path):
    raw = dict(SAMPLE)
    raw["notifications"] = {"min_interval_s": 60}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="missing required key"):
        sbfd_ctl.load_config(str(p))


def test_load_config_notifications_empty_topic_raises(tmp_path: Path):
    raw = dict(SAMPLE)
    raw["notifications"] = {"topic": ""}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="topic"):
        sbfd_ctl.load_config(str(p))


def test_load_config_notifications_negative_interval_raises(tmp_path: Path):
    raw = dict(SAMPLE)
    raw["notifications"] = {"topic": "pathfuse", "min_interval_s": -5}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="min_interval_s"):
        sbfd_ctl.load_config(str(p))


def test_load_config_notifications_negative_wan_down_hold_raises(tmp_path: Path):
    raw = dict(SAMPLE)
    raw["notifications"] = {"topic": "pathfuse", "wan_down_hold_s": -1}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="wan_down_hold_s"):
        sbfd_ctl.load_config(str(p))


def _cfg_with_maint(tmp_path: Path, maint=None):
    raw = dict(SAMPLE)
    if maint is not None:
        raw["maintenance_reboot"] = maint
    p = tmp_path / "maint.json"
    p.write_text(json.dumps(raw))
    return sbfd_ctl.load_config(str(p))


def test_maintenance_section_parses(tmp_path: Path):
    # The optional maintenance_reboot block populates cfg.maintenance.
    cfg = _cfg_with_maint(tmp_path, {
        "enabled": True, "hour": 3,
        "window": {"path": "/run/sbfd-ctl/maintenance_window.json"}})
    assert cfg.maintenance.enabled is True
    assert cfg.maintenance.hour == 3
    assert cfg.maintenance.window_path == "/run/sbfd-ctl/maintenance_window.json"


def test_maintenance_absent_is_unconfigured(tmp_path: Path):
    # No section => feature unconfigured => cfg.maintenance is None.
    cfg = _cfg_with_maint(tmp_path)
    assert cfg.maintenance is None


def test_maintenance_window_path_defaults(tmp_path: Path):
    # window.path is optional; it falls back to the /run default.
    cfg = _cfg_with_maint(tmp_path, {"enabled": False, "hour": 0})
    assert cfg.maintenance.window_path == "/run/sbfd-ctl/maintenance_window.json"


def test_maintenance_hour_out_of_range_rejected(tmp_path: Path):
    # hour is validated 0..23 at config-load time, like the other bounds checks.
    with pytest.raises(ValueError, match="maintenance_reboot.hour"):
        _cfg_with_maint(tmp_path, {"enabled": True, "hour": 24})


def test_maintenance_hour_zero_is_valid(tmp_path: Path):
    # 0 is midnight, the most likely configured value, and must not be rejected.
    cfg = _cfg_with_maint(tmp_path, {"enabled": True, "hour": 0})
    assert cfg.maintenance.hour == 0


def test_maintenance_hour_bool_rejected(tmp_path: Path):
    # int(True) == 1: a boolean hour must not read as 1am.
    with pytest.raises(ValueError, match="maintenance_reboot.hour"):
        _cfg_with_maint(tmp_path, {"enabled": True, "hour": True})
