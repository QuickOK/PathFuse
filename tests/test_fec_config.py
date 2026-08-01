import json, tempfile, os
import sbfd_ctl as M

BASE = {
    "wans": {"wan1": {"iface": "wan1", "session_id": 1, "label": "Cellular"},
             "wan2": {"iface": "wan2", "session_id": 2, "label": "Satellite"}},
    "relay": {"state_url": "http://x/state"},
    "engarde": {"server_ip": "192.0.2.4", "server_port": 59402,
                "admin_url": "http://127.0.0.1:8080/api/v1/get-list"},
    "nft": {"table": "sbfd_ctl", "family": "inet"},
    "egress": {"engarde_table": "engarde", "wg_iface": "wg0", "default_mode": "relay_vpn"},
    "policy": {"default_mode": "full", "default_master_policy": "static_primary",
               "default_master_wan": "wan2", "failback_hold_s": 30,
               "manage_default_route": False},
    "ui": {"listen": "0.0.0.0:8081"},
    "sbfd_local_state": "/run/sbfd/state.json",
    "runtime_state": "/run/sbfd-ctl/runtime.json",
    "persist_state": "/var/lib/sbfd-ctl/runtime.json",
    "published_state": "/run/sbfd-ctl/state.json",
}

def _write(d):
    fd, p = tempfile.mkstemp(suffix=".json"); os.write(fd, json.dumps(d).encode()); os.close(fd); return p

def test_config_without_fec_block_has_none():
    cfg = M.load_config(_write(BASE))
    assert cfg.fec is None

def test_config_with_fec_block_parsed():
    d = dict(BASE)
    d["fec"] = {"enabled": True, "fifo": "/run/udpspeeder/client.fifo",
                "loss_table": [{"max_loss_pct": 0.5, "fec": "1:0"}, {"max_loss_pct": 100.0, "fec": "8:8"}],
                "ramp_up_ticks": 3, "ramp_down_hold_s": 25,
                "full_mode_backoff_fec": "1:0", "full_min_up_wans": 2}
    cfg = M.load_config(_write(d))
    assert cfg.fec is not None
    assert cfg.fec.enabled is True
    assert cfg.fec.fifo == "/run/udpspeeder/client.fifo"
    assert cfg.fec.ramp_up_ticks == 3
    assert cfg.fec.full_mode_backoff_fec == "1:0"
    assert cfg.fec.loss_table[-1]["fec"] == "8:8"


def test_config_parses_relay_fec_url():
    d = dict(BASE)
    d["relay"] = dict(BASE["relay"]); d["relay"]["fec_url"] = "http://relay:9276/fec"
    cfg = M.load_config(_write(d))
    assert cfg.relay.fec_url == "http://relay:9276/fec"


def test_config_relay_fec_url_defaults_none():
    cfg = M.load_config(_write(BASE))
    assert cfg.relay.fec_url is None


def test_fec_wire_unit_defaults():
    d = dict(BASE)
    d["fec"] = {"enabled": True, "fifo": "/run/udpspeeder/client.fifo"}
    cfg = M.load_config(_write(d))
    assert cfg.fec.wire_unit == "udpspeeder-client"
    assert cfg.fec.wire_stale_after_s == 30.0


def test_fec_wire_unit_override():
    d = dict(BASE)
    d["fec"] = {"enabled": True, "fifo": "/run/udpspeeder/client.fifo",
                "wire_unit": "udpspeeder-client@client", "wire_stale_after_s": 45}
    cfg = M.load_config(_write(d))
    assert cfg.fec.wire_unit == "udpspeeder-client@client"
    assert cfg.fec.wire_stale_after_s == 45.0


# ---------- FEC mode schema ----------
import fec_control as F


def test_fec_mode_defaults_to_min_adaptive_when_absent():
    # New configs (no `mode`, no legacy `enabled`) get the new default.
    d = dict(BASE)
    d["fec"] = {"fifo": "/run/udpspeeder/client.fifo"}
    cfg = M.load_config(_write(d))
    assert cfg.fec.mode == F.MODE_MIN_ADAPTIVE
    assert cfg.fec.fixed_ratio == F.DEFAULT_FIXED_RATIO
    assert cfg.fec.floor_ratio == F.DEFAULT_FLOOR_RATIO


def test_fec_mode_explicit_wins():
    d = dict(BASE)
    d["fec"] = {"enabled": True, "fifo": "/run/udpspeeder/client.fifo",
                "mode": "adaptive", "fixed_ratio": "8:2", "floor_ratio": "8:1"}
    cfg = M.load_config(_write(d))
    assert cfg.fec.mode == "adaptive"
    assert cfg.fec.fixed_ratio == "8:2"
    assert cfg.fec.floor_ratio == "8:1"


def test_fec_legacy_enabled_true_maps_to_adaptive():
    # An older config with enabled=true and no mode field preserves the explicit
    # choice as adaptive rather than silently lifting it to the new min_adaptive
    # default.
    d = dict(BASE)
    d["fec"] = {"fifo": "/run/udpspeeder/client.fifo", "enabled": True}
    cfg = M.load_config(_write(d))
    assert cfg.fec.mode == F.MODE_ADAPTIVE


def test_fec_legacy_enabled_false_maps_to_off():
    d = dict(BASE)
    d["fec"] = {"fifo": "/run/udpspeeder/client.fifo", "enabled": False}
    cfg = M.load_config(_write(d))
    assert cfg.fec.mode == F.MODE_OFF


def test_fec_explicit_mode_overrides_deprecated_enabled_false():
    # Greptile P2: migrating to mode:"adaptive" while leaving the deprecated
    # enabled:false behind must NOT silently keep FEC off. An explicit mode
    # wins; `enabled` must not act as a hidden kill-switch.
    d = dict(BASE)
    d["fec"] = {"fifo": "/run/udpspeeder/client.fifo",
                "mode": "adaptive", "enabled": False}
    cfg = M.load_config(_write(d))
    assert cfg.fec.mode == F.MODE_ADAPTIVE
    # effective_fec_mode must reflect the resolved mode, not the stale bool.
    assert M.effective_fec_mode(cfg, M.RuntimeOverlay()) == F.MODE_ADAPTIVE
    assert M.effective_fec_enabled(cfg, M.RuntimeOverlay()) is True


def test_fec_explicit_mode_off_overrides_enabled_true():
    # Symmetric: mode:"off" wins even if a stale enabled:true lingers.
    d = dict(BASE)
    d["fec"] = {"fifo": "/run/udpspeeder/client.fifo",
                "mode": "off", "enabled": True}
    cfg = M.load_config(_write(d))
    assert cfg.fec.mode == F.MODE_OFF
    assert M.effective_fec_mode(cfg, M.RuntimeOverlay()) == F.MODE_OFF


# ---------- per-WAN FEC profiles ----------

def test_wan_profiles_parsed_with_cell_defaults():
    d = dict(BASE)
    d["fec"] = {"fifo": "/run/udpspeeder/client.fifo",
                "wan_profiles": {"wan1": {}}}
    cfg = M.load_config(_write(d))
    p = cfg.fec.wan_profiles["wan1"]
    assert p.name == "wan1"
    assert [r["fec"] for r in p.loss_table] == ["8:0", "20:1", "12:1", "8:1"]
    assert p.ramp_up_ticks == 1
    assert p.ramp_down_hold_s == 60.0
    assert p.floor_ratio == "8:0"
    assert p.signal_floor_fec == "12:1"


def test_wan_profiles_absent_is_empty():
    d = dict(BASE)
    d["fec"] = {"fifo": "/run/udpspeeder/client.fifo"}
    cfg = M.load_config(_write(d))
    assert cfg.fec.wan_profiles == {}


# ---------- ladder scale candidates ----------

def test_fec_profile_candidates_covers_default_and_every_profile():
    d = dict(BASE)
    d["fec"] = {"fifo": "/run/udpspeeder/client.fifo", "floor_ratio": "8:1",
                "wan_profiles": {"wan1": {"floor_ratio": "12:1"}}}
    cfg = M.load_config(_write(d))
    cands = M.fec_profile_candidates(cfg, M.RuntimeOverlay())
    assert [floor for _t, floor in cands] == ["8:1", "12:1"]
    assert [r["fec"] for r in cands[0][0]] == ["8:0", "8:2", "8:4", "8:6", "8:8"]
    assert [r["fec"] for r in cands[1][0]] == ["8:0", "20:1", "12:1", "8:1"]
    # The scale the UI draws spans both, so a position means one ratio whichever
    # profile is driving.
    assert F.ladder_scale(cands, F.MODE_MIN_ADAPTIVE) == \
        ["12:1", "8:1", "8:2", "8:4", "8:6", "8:8"]


def test_fec_profile_candidates_reflects_a_runtime_floor_override():
    # An overridden floor must land ON the scale, not off the end of it.
    d = dict(BASE)
    d["fec"] = {"fifo": "/run/udpspeeder/client.fifo", "floor_ratio": "8:1",
                "wan_profiles": {"wan1": {"floor_ratio": "12:1"}}}
    cfg = M.load_config(_write(d))
    ov = M.RuntimeOverlay(fec_floor_ratio="8:4")
    assert [floor for _t, floor in M.fec_profile_candidates(cfg, ov)] == \
        ["8:4", "8:4"]


def test_fec_profile_candidates_empty_without_fec():
    d = dict(BASE)
    cfg = M.load_config(_write(d))
    assert M.fec_profile_candidates(cfg, M.RuntimeOverlay()) == []


def test_driver_hysteresis_defaults_and_overrides():
    d = dict(BASE)
    d["fec"] = {"fifo": "/run/udpspeeder/client.fifo"}
    cfg = M.load_config(_write(d))
    assert cfg.fec.driver_dwell_s == 120.0
    d["fec"] = {"fifo": "/run/udpspeeder/client.fifo", "driver_dwell_s": 45}
    cfg = M.load_config(_write(d))
    assert cfg.fec.driver_dwell_s == 45.0


def test_driver_dwell_rejects_values_that_defeat_the_hysteresis():
    # Negative promotes a challenger on the next tick; NaN/inf leave one
    # pending forever. Both disable the damping silently, so fail at load.
    import pytest
    for bad in (-1, float("nan"), float("inf")):
        d = dict(BASE)
        d["fec"] = {"fifo": "/run/udpspeeder/client.fifo", "driver_dwell_s": bad}
        with pytest.raises(ValueError, match="driver_dwell_s"):
            M.load_config(_write(d))
