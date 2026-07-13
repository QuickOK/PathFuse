import copy
import json
from pathlib import Path
import pytest
import sbfd_ctl as M


BASE_WITH_ENV = {
    "wans": {"wan1": {"iface": "wan1", "session_id": 1, "label": "Cell"},
             "wan2": {"iface": "wan2", "session_id": 2, "label": "Sat"}},
    "relay": {"state_url": "http://r/state", "fec_url": "http://r/fec"},
    "engarde": {"server_ip": "198.51.100.10", "server_port": 59402},
    "nft": {"table": "sbfd_ctl", "family": "inet"},
    "egress": {"engarde_table": "engarde", "wg_iface": "wg0", "default_mode": "relay_vpn"},
    "policy": {"default_mode": "master_backup", "default_master_policy": "static_primary",
               "default_master_wan": "wan2"},
    "ui": {"listen": "0.0.0.0:8081"},
    "sbfd_local_state": "/run/sbfd/state.json",
    "runtime_state": "/run/sbfd-ctl/runtime.json",
    "persist_state": "/var/lib/sbfd-ctl/runtime.json",
    "published_state": "/run/sbfd-ctl/state.json",
    "environmental": {"enabled": True,
                      "auto_override": {"path": "/run/sbfd-ctl/auto_override.json",
                                        "ttl_s": 180}},
}


def base_cfg(**over):
    """Minimal Config; override fields via kwargs."""
    defaults = dict(
        wans={"wan1": M.WanCfg("wan1", 1, "Cell"),
              "wan2": M.WanCfg("wan2", 2, "Sat")},
        relay=M.RelayCfg("http://x"),
        engarde=M.EngardeCfg("198.51.100.10", 59402),
        nft=M.NftCfg(table="sbfd_ctl", family="inet"),
        policy=M.PolicyCfg(),
        ui_listen="", sbfd_local_state="", runtime_state="",
        persist_state="", published_state="",
    )
    defaults.update(over)
    return M.Config(**defaults)


def test_environmental_cfg_defaults_to_none_when_absent():
    cfg = base_cfg()
    assert cfg.environmental is None


def test_load_config_parses_environmental_block(tmp_path):
    raw = copy.deepcopy(BASE_WITH_ENV)
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(raw))
    cfg = M.load_config(str(p))
    assert cfg.environmental is not None
    assert cfg.environmental.enabled is True
    assert cfg.environmental.auto_override_path == "/run/sbfd-ctl/auto_override.json"
    assert cfg.environmental.auto_override_ttl_s == 180.0


def test_load_config_rejects_zero_ttl_s(tmp_path):
    raw = copy.deepcopy(BASE_WITH_ENV)
    raw["environmental"]["auto_override"]["ttl_s"] = 0
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="ttl_s"):
        M.load_config(str(p))


def _env_cfg(tmp_path, ttl_s=180.0, enabled=True):
    return M.EnvironmentalCfg(
        enabled=enabled,
        auto_override_path=str(tmp_path / "auto_override.json"),
        auto_override_ttl_s=ttl_s,
    )


def _write_override(path, **over):
    rec = {"force_full": True, "source": "environ_ctl", "reason": "precip ahead", "set_ts": 1000.0}
    rec.update(over)
    with open(path, "w") as f:
        json.dump(rec, f)


def test_load_auto_override_none_when_unconfigured(tmp_path):
    cfg = base_cfg()  # environmental is None
    assert M.load_auto_override(cfg, now=1000.0) is None


def test_load_auto_override_reads_fresh_record(tmp_path):
    cfg = base_cfg(environmental=_env_cfg(tmp_path))
    _write_override(cfg.environmental.auto_override_path, set_ts=1000.0)
    ao = M.load_auto_override(cfg, now=1050.0)
    assert ao is not None
    assert ao.force_full is True
    assert ao.source == "environ_ctl"
    assert ao.reason == "precip ahead"
    assert ao.set_ts == 1000.0


def test_load_auto_override_none_when_stale(tmp_path):
    cfg = base_cfg(environmental=_env_cfg(tmp_path, ttl_s=180.0))
    _write_override(cfg.environmental.auto_override_path, set_ts=1000.0)
    assert M.load_auto_override(cfg, now=1000.0 + 181) is None


def test_load_auto_override_none_when_missing(tmp_path):
    cfg = base_cfg(environmental=_env_cfg(tmp_path))
    assert M.load_auto_override(cfg, now=1000.0) is None


def test_load_auto_override_none_when_malformed(tmp_path):
    cfg = base_cfg(environmental=_env_cfg(tmp_path))
    with open(cfg.environmental.auto_override_path, "w") as f:
        f.write("{ not json")
    assert M.load_auto_override(cfg, now=1000.0) is None


def test_load_auto_override_live_at_exact_ttl_boundary(tmp_path):
    cfg = base_cfg(environmental=_env_cfg(tmp_path, ttl_s=180.0))
    _write_override(cfg.environmental.auto_override_path, set_ts=1000.0)
    # exactly at boundary: not stale
    assert M.load_auto_override(cfg, now=1000.0 + 180.0) is not None


def test_effective_environmental_enabled_false_when_unconfigured():
    cfg = base_cfg()  # environmental None
    ov = M.RuntimeOverlay()
    assert M.effective_environmental_enabled(cfg, ov) is False


def test_effective_environmental_enabled_uses_config_default(tmp_path):
    cfg = base_cfg(environmental=_env_cfg(tmp_path, enabled=True))
    assert M.effective_environmental_enabled(cfg, M.RuntimeOverlay()) is True
    cfg2 = base_cfg(environmental=_env_cfg(tmp_path, enabled=False))
    assert M.effective_environmental_enabled(cfg2, M.RuntimeOverlay()) is False


def test_effective_environmental_enabled_operator_override_wins(tmp_path):
    cfg = base_cfg(environmental=_env_cfg(tmp_path, enabled=True))
    assert M.effective_environmental_enabled(cfg, M.RuntimeOverlay(environmental_enabled=False)) is False
    cfg2 = base_cfg(environmental=_env_cfg(tmp_path, enabled=False))
    assert M.effective_environmental_enabled(cfg2, M.RuntimeOverlay(environmental_enabled=True)) is True


def test_runtime_overlay_roundtrips_environmental_enabled(tmp_path):
    cfg = base_cfg(
        runtime_state=str(tmp_path / "runtime.json"),
        persist_state=str(tmp_path / "persist.json"),
    )
    ov = M.RuntimeOverlay(environmental_enabled=False, set_by="ui", set_ts=5.0)
    M.save_runtime_overlay(cfg, ov)
    back = M.load_runtime_overlay(cfg)
    assert back.environmental_enabled is False


def test_validate_runtime_payload_accepts_environmental_enabled():
    ok, err = M.validate_runtime_payload({"environmental_enabled": True}, {"wan1", "wan2"})
    assert ok and err is None


def test_validate_runtime_payload_rejects_bad_environmental_enabled():
    ok, err = M.validate_runtime_payload({"environmental_enabled": "yes"}, {"wan1", "wan2"})
    assert not ok
    assert "environmental_enabled" in err


def _ao(force_full=True, reason="precip ahead", set_ts=1000.0):
    return M.AutoOverride(force_full=force_full, source="environ_ctl",
                          reason=reason, set_ts=set_ts)


def test_apply_auto_override_raises_to_full_when_enabled_and_hazard():
    mode, active = M.apply_auto_override("master_backup", env_enabled=True, auto=_ao(True))
    assert mode == "full"
    assert active is not None and active.reason == "precip ahead"


def test_apply_auto_override_passthrough_when_toggle_off():
    mode, active = M.apply_auto_override("master_backup", env_enabled=False, auto=_ao(True))
    assert mode == "master_backup"
    assert active is None


def test_apply_auto_override_passthrough_when_no_hazard():
    mode, active = M.apply_auto_override("master_backup", env_enabled=True, auto=_ao(False))
    assert mode == "master_backup"
    assert active is None


def test_apply_auto_override_passthrough_when_no_override_file():
    mode, active = M.apply_auto_override("master_backup", env_enabled=True, auto=None)
    assert mode == "master_backup"
    assert active is None


def test_apply_auto_override_keeps_operator_full():
    mode, active = M.apply_auto_override("full", env_enabled=True, auto=_ao(False))
    assert mode == "full"
    assert active is None


def test_environmental_snapshot_shapes_fields():
    snap = M.environmental_snapshot(
        configured=True, env_enabled=True, active=_ao(True, "smoke", set_ts=1000.0),
        auto=_ao(True, "smoke", set_ts=1000.0), now=1004.0)
    assert snap == {
        "configured": True, "enabled": True, "active": True, "force_full": True,
        "source": "environ_ctl", "reason": "smoke", "age_s": 4.0,
    }


def test_environmental_snapshot_inactive_when_no_override():
    snap = M.environmental_snapshot(
        configured=True, env_enabled=True, active=None, auto=None, now=1004.0)
    assert snap == {
        "configured": True, "enabled": True, "active": False, "force_full": False,
        "source": None, "reason": None, "age_s": None,
    }


def test_environmental_snapshot_override_present_but_disabled():
    ao = _ao(True, "smoke", set_ts=1000.0)
    snap = M.environmental_snapshot(
        configured=True, env_enabled=False, active=None, auto=ao, now=1004.0)
    assert snap["active"] is False
    assert snap["enabled"] is False
    assert snap["force_full"] is True
    assert snap["source"] == "environ_ctl"
    assert snap["age_s"] == 4.0


def test_published_snapshot_includes_environmental_block(tmp_path, monkeypatch):
    """Drive one controller iteration far enough to publish a snapshot and assert
    the environmental block is present and reflects a fresh force_full override."""
    import threading
    import time as _t
    cfg = base_cfg(
        environmental=_env_cfg(tmp_path, enabled=True),
        runtime_state=str(tmp_path / "runtime.json"),
        persist_state=str(tmp_path / "persist.json"),
        published_state=str(tmp_path / "state.json"),
        sbfd_local_state=str(tmp_path / "sbfd.json"),
        policy=M.PolicyCfg(default_mode="master_backup", default_master_wan="wan2"),
    )
    _write_override(cfg.environmental.auto_override_path, force_full=True,
                    reason="precip ahead", set_ts=_t.time())

    stop = threading.Event()
    orig_publish = M.publish_state
    def publish_and_stop(c, snap):
        orig_publish(c, snap)
        stop.set()
    monkeypatch.setattr(M, "publish_state", publish_and_stop)
    monkeypatch.setattr(M, "apply_nft_diff", lambda *a, **k: None)
    monkeypatch.setattr(M, "list_current_drops", lambda *a, **k: set())
    monkeypatch.setattr(M, "apply_nft_init", lambda *a, **k: None)
    monkeypatch.setattr(M, "fetch_remote_sbfd_state",
                        lambda *a, **k: M.StateSnapshot(ok=False, per_wan={}, error="stub"))
    monkeypatch.setattr(M, "read_local_sbfd_state",
                        lambda *a, **k: M.StateSnapshot(ok=False, per_wan={}, error="stub"))
    monkeypatch.setattr(M, "read_wan_gateway", lambda *a, **k: None)
    monkeypatch.setattr(M, "read_managed_default", lambda *a, **k: None)
    monkeypatch.setattr(M, "read_engarde_table_default", lambda *a, **k: None)
    monkeypatch.setattr(M, "apply_engarde_table_action", lambda *a, **k: None)

    M.run_controller(cfg, stop_event=stop)

    snap = json.loads(Path(cfg.published_state).read_text())
    assert "environmental" in snap
    assert snap["environmental"]["enabled"] is True
    assert snap["environmental"]["active"] is True
    assert snap["mode"] == "full"
    assert "precip ahead" in (snap["environmental"]["reason"] or "")


@pytest.mark.parametrize("persisted", [True, False])
def test_published_snapshot_reports_overlay_persist(tmp_path, monkeypatch, persisted):
    """The UI's "persist across reboot" checkbox can only render the overlay's
    real persist flag if the snapshot publishes it. Without this, a freshly
    loaded page shows the box unchecked and the next Apply silently clears a
    persisted overlay."""
    import threading
    cfg = base_cfg(
        runtime_state=str(tmp_path / "runtime.json"),
        persist_state=str(tmp_path / "persist.json"),
        published_state=str(tmp_path / "state.json"),
        sbfd_local_state=str(tmp_path / "sbfd.json"),
        policy=M.PolicyCfg(default_mode="master_backup", default_master_wan="wan2"),
    )
    M.save_runtime_overlay(cfg, M.RuntimeOverlay(
        mode="master_backup", persist=persisted, set_by="ui", set_ts=1.0))

    stop = threading.Event()
    orig_publish = M.publish_state
    def publish_and_stop(c, snap):
        orig_publish(c, snap)
        stop.set()
    monkeypatch.setattr(M, "publish_state", publish_and_stop)
    monkeypatch.setattr(M, "apply_nft_diff", lambda *a, **k: None)
    monkeypatch.setattr(M, "list_current_drops", lambda *a, **k: set())
    monkeypatch.setattr(M, "apply_nft_init", lambda *a, **k: None)
    monkeypatch.setattr(M, "fetch_remote_sbfd_state",
                        lambda *a, **k: M.StateSnapshot(ok=False, per_wan={}, error="stub"))
    monkeypatch.setattr(M, "read_local_sbfd_state",
                        lambda *a, **k: M.StateSnapshot(ok=False, per_wan={}, error="stub"))
    monkeypatch.setattr(M, "read_wan_gateway", lambda *a, **k: None)
    monkeypatch.setattr(M, "read_managed_default", lambda *a, **k: None)
    monkeypatch.setattr(M, "read_engarde_table_default", lambda *a, **k: None)
    monkeypatch.setattr(M, "apply_engarde_table_action", lambda *a, **k: None)

    M.run_controller(cfg, stop_event=stop)

    snap = json.loads(Path(cfg.published_state).read_text())
    assert snap["persist"] is persisted
