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
