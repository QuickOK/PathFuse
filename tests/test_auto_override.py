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


# -- maintenance-reboot schedule: overlay, resolvers, window ------------------

def test_effective_maintenance_overlay_wins_both_ways():
    # Config is the boot default only; the operator's overlay wins either way.
    cfg = base_cfg(maintenance=M.MaintenanceCfg(
        enabled=False, hour=0,
        window_path="/run/sbfd-ctl/maintenance_window.json"))
    ov = M.RuntimeOverlay()
    assert M.effective_maintenance_enabled(cfg, ov) is False
    assert M.effective_maintenance_hour(cfg, ov) == 0
    ov.maintenance_enabled = True
    ov.maintenance_hour = 3
    assert M.effective_maintenance_enabled(cfg, ov) is True
    assert M.effective_maintenance_hour(cfg, ov) == 3


def test_effective_maintenance_overlay_can_disable_a_config_enabled_feature():
    # The overlay must be able to turn OFF what config turned on (not a kill-switch).
    cfg = base_cfg(maintenance=M.MaintenanceCfg(enabled=True, hour=4))
    assert M.effective_maintenance_enabled(cfg, M.RuntimeOverlay()) is True
    assert M.effective_maintenance_enabled(
        cfg, M.RuntimeOverlay(maintenance_enabled=False)) is False


def test_effective_maintenance_unconfigured_is_off():
    # Unconfigured => hard off, whatever the overlay says.
    cfg = base_cfg()
    assert M.effective_maintenance_enabled(
        cfg, M.RuntimeOverlay(maintenance_enabled=True)) is False
    assert M.effective_maintenance_hour(
        cfg, M.RuntimeOverlay(maintenance_hour=5)) == 0


def test_effective_maintenance_hour_zero_from_overlay_is_honoured():
    # 0 is falsy but real: an overlay hour of 0 must not fall back to config.
    cfg = base_cfg(maintenance=M.MaintenanceCfg(enabled=True, hour=4))
    ov = M.RuntimeOverlay(maintenance_hour=0)
    assert M.effective_maintenance_hour(cfg, ov) == 0


def test_effective_maintenance_hour_ignores_junk_overlay():
    # A bool or out-of-range overlay hour must not authorize a reboot.
    cfg = base_cfg(maintenance=M.MaintenanceCfg(enabled=True, hour=4))
    assert M.effective_maintenance_hour(cfg, M.RuntimeOverlay(maintenance_hour=True)) == 4
    assert M.effective_maintenance_hour(cfg, M.RuntimeOverlay(maintenance_hour=99)) == 4
    assert M.effective_maintenance_hour(cfg, M.RuntimeOverlay(maintenance_hour="3")) == 4


def test_maintenance_hour_validation_rejects_bools_and_range():
    wans = {"wan1", "wan2"}
    # bool is an int subclass in Python: True must not pass as an hour.
    ok, err = M.validate_runtime_payload({"maintenance_hour": True}, wans)
    assert ok is False
    ok, err = M.validate_runtime_payload({"maintenance_hour": 24}, wans)
    assert ok is False
    ok, err = M.validate_runtime_payload({"maintenance_hour": -1}, wans)
    assert ok is False
    ok, err = M.validate_runtime_payload({"maintenance_hour": "3"}, wans)
    assert ok is False
    ok, err = M.validate_runtime_payload({"maintenance_hour": 0}, wans)
    assert ok is True                     # 0 is midnight, and it is valid


def test_maintenance_enabled_validation_rejects_non_bool():
    # maintenance_enabled is a plain boolean toggle.
    wans = {"wan1", "wan2"}
    ok, _ = M.validate_runtime_payload({"maintenance_enabled": "yes"}, wans)
    assert ok is False
    ok, _ = M.validate_runtime_payload({"maintenance_enabled": True}, wans)
    assert ok is True


def test_maintenance_hour_zero_survives_the_overlay_round_trip(tmp_path):
    # Guards the `payload.get(k) or default` bug: hour 0 is falsy but real.
    cfg = base_cfg(runtime_state=str(tmp_path / "rt.json"),
                   persist_state=str(tmp_path / "p.json"))
    ov = M.RuntimeOverlay(maintenance_enabled=True, maintenance_hour=0)
    M.save_runtime_overlay(cfg, ov)
    back = M.load_runtime_overlay(cfg)
    assert back.maintenance_hour == 0
    assert back.maintenance_enabled is True


def test_load_maintenance_window_returns_fresh_window(tmp_path):
    # A window whose wall-clock `until` is in the future is returned as-is.
    w = tmp_path / "win.json"
    w.write_text(json.dumps({"wan": "wan1", "until": 1000.0}))
    cfg = base_cfg(maintenance=M.MaintenanceCfg(
        enabled=True, hour=3, window_path=str(w)))
    got = M.load_maintenance_window(cfg, 900.0)
    assert got["wan"] == "wan1"


def test_load_maintenance_window_fails_open(tmp_path):
    # Every bad path suppresses NOTHING: the failure mode is a spurious page,
    # never a missed one.
    w = tmp_path / "win.json"
    cfg = base_cfg(maintenance=M.MaintenanceCfg(
        enabled=True, hour=3, window_path=str(w)))
    assert M.load_maintenance_window(cfg, 900.0) is None          # missing file
    w.write_text("{not json")
    assert M.load_maintenance_window(cfg, 900.0) is None          # unparseable
    w.write_text(json.dumps([1, 2, 3]))
    assert M.load_maintenance_window(cfg, 900.0) is None          # not a dict
    w.write_text(json.dumps({"wan": "wan1"}))
    assert M.load_maintenance_window(cfg, 900.0) is None          # no `until`
    w.write_text(json.dumps({"wan": "wan1", "until": "soon"}))
    assert M.load_maintenance_window(cfg, 900.0) is None          # `until` not a number
    w.write_text(json.dumps({"wan": "wan1", "until": True}))
    assert M.load_maintenance_window(cfg, 900.0) is None          # `until` is a bool
    w.write_text(json.dumps({"wan": "wan1", "until": 1000.0}))
    assert M.load_maintenance_window(cfg, 1000.0) is None         # expired (now == until)
    assert M.load_maintenance_window(cfg, 1200.0) is None         # expired


def test_load_maintenance_window_unconfigured_is_none(tmp_path):
    # Feature unconfigured: never read a file, never suppress.
    assert M.load_maintenance_window(base_cfg(), 900.0) is None


def _stub_controller_io(monkeypatch, stop):
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


def test_published_snapshot_carries_the_resolved_maintenance_schedule(tmp_path, monkeypatch):
    # maintenance_reboot.read_published() consumes exactly this block, and it
    # rejects a snapshot with no fresh `ts` as stale (which would skip the night).
    import threading
    import time
    cfg = base_cfg(
        runtime_state=str(tmp_path / "runtime.json"),
        persist_state=str(tmp_path / "persist.json"),
        published_state=str(tmp_path / "state.json"),
        sbfd_local_state=str(tmp_path / "sbfd.json"),
        maintenance=M.MaintenanceCfg(enabled=False, hour=2,
                                     window_path=str(tmp_path / "win.json")),
    )
    # Operator's overlay wins over the config default, in both directions.
    M.save_runtime_overlay(cfg, M.RuntimeOverlay(
        maintenance_enabled=True, maintenance_hour=0, set_by="ui", set_ts=1.0))

    stop = threading.Event()
    _stub_controller_io(monkeypatch, stop)
    M.run_controller(cfg, stop_event=stop)

    snap = json.loads(Path(cfg.published_state).read_text())
    assert snap["maintenance"] == {"configured": True, "enabled": True, "hour": 0}
    assert abs(snap["ts"] - time.time()) < 60      # fresh wall-clock ts, or the night is skipped


def test_published_snapshot_maintenance_unconfigured(tmp_path, monkeypatch):
    # No config section => configured:false => the UI greys it out and the
    # one-shot skips.
    import threading
    cfg = base_cfg(
        runtime_state=str(tmp_path / "runtime.json"),
        persist_state=str(tmp_path / "persist.json"),
        published_state=str(tmp_path / "state.json"),
        sbfd_local_state=str(tmp_path / "sbfd.json"),
    )
    stop = threading.Event()
    _stub_controller_io(monkeypatch, stop)
    M.run_controller(cfg, stop_event=stop)

    snap = json.loads(Path(cfg.published_state).read_text())
    assert snap["maintenance"]["configured"] is False
    assert snap["maintenance"]["enabled"] is False


# -- cell telemetry reader / snapshot ----------------------------------------

def _cell_cfg(tmp_path):
    cfg = base_cfg()
    cfg.cell = M.CellTelemetryCfg(state_path=str(tmp_path / "cell.json"))
    return cfg


def test_load_cell_sample_missing_file_is_none(tmp_path):
    assert M.load_cell_sample(_cell_cfg(tmp_path), now=1000.0) is None


def test_load_cell_sample_unconfigured_is_none():
    assert M.load_cell_sample(base_cfg(), now=1000.0) is None


def test_load_cell_sample_reads_and_coerces(tmp_path):
    cfg = _cell_cfg(tmp_path)
    (tmp_path / "cell.json").write_text(json.dumps(
        {"set_ts": 990.0, "rsrp": -98, "rsrq": -11.0, "sinr": None,
         "cell_id": 1234567, "band": "LTE B2"}))
    s = M.load_cell_sample(cfg, now=1000.0)
    assert s["rsrp"] == -98.0 and s["cell_id"] == "1234567"
    assert s["sinr"] is None
    snap = M.cell_snapshot(cfg, s, now=1000.0)
    assert snap["configured"] is True and snap["stale"] is False
    assert snap["age_s"] == 10.0
    snap_old = M.cell_snapshot(cfg, s, now=1010.1)
    assert snap_old["stale"] is True     # > stale_after_s (10)


def test_load_cell_sample_malformed_json_is_none(tmp_path):
    cfg = _cell_cfg(tmp_path)
    Path(cfg.cell.state_path).write_text("not json")
    assert M.load_cell_sample(cfg, now=1000.0) is None


def test_load_cell_sample_missing_set_ts_is_none(tmp_path):
    cfg = _cell_cfg(tmp_path)
    Path(cfg.cell.state_path).write_text(json.dumps({"rsrp": -98}))
    assert M.load_cell_sample(cfg, now=1000.0) is None


def test_load_cell_sample_bool_metrics_coerced_to_none(tmp_path):
    # bool is an int subclass; rsrp/rsrq/sinr: true must not read as 1.0.
    cfg = _cell_cfg(tmp_path)
    Path(cfg.cell.state_path).write_text(json.dumps(
        {"set_ts": 990.0, "rsrp": True, "rsrq": False, "sinr": 5}))
    s = M.load_cell_sample(cfg, now=1000.0)
    assert s["rsrp"] is None and s["rsrq"] is None and s["sinr"] == 5.0


def test_cell_snapshot_unconfigured_and_absent(tmp_path):
    cfg = base_cfg()
    assert M.cell_snapshot(cfg, None, 0.0) == {"configured": False}
    cfg2 = _cell_cfg(tmp_path)
    snap = M.cell_snapshot(cfg2, None, 0.0)
    assert snap["configured"] is True and snap["stale"] is True
    assert snap["rsrp"] is None


# -- cell handoff duplication window -----------------------------------------

def _handoff_cfg(tmp_path):
    cfg = base_cfg()
    cfg.cell = M.CellTelemetryCfg(state_path=str(tmp_path / "cell.json"),
                                  handoff_path=str(tmp_path / "handoff.json"))
    return cfg


def test_load_cell_handoff_fail_open_and_expiry(tmp_path):
    cfg = _handoff_cfg(tmp_path)
    assert M.load_cell_handoff(cfg, now=1000.0) is None          # missing
    (tmp_path / "handoff.json").write_text(json.dumps(
        {"set_ts": 1000.0, "until_ts": 1004.0, "reason": "cell_change:1->2"}))
    w = M.load_cell_handoff(cfg, now=1002.0)
    assert w.reason == "cell_change:1->2" and w.until_ts == 1004.0
    assert M.load_cell_handoff(cfg, now=1004.0) is None          # expired (>=)
    (tmp_path / "handoff.json").write_text("not json")
    assert M.load_cell_handoff(cfg, now=1002.0) is None          # malformed
    # stale set_ts beyond the sanity TTL (e.g. daemon clock skew): ignored
    (tmp_path / "handoff.json").write_text(json.dumps(
        {"set_ts": 900.0, "until_ts": 99999.0, "reason": "x"}))
    assert M.load_cell_handoff(cfg, now=1000.0) is None


def test_load_cell_handoff_unconfigured_is_none():
    assert M.load_cell_handoff(base_cfg(), now=1000.0) is None


def test_load_cell_handoff_rejects_infinite_until_ts(tmp_path):
    # CodeRabbit PR#5 CR1: json.loads accepts the bareword Infinity, and
    # `now >= until_ts` is False forever against it -- a corrupt/crafted
    # handoff file could hold forced full-mode duplication open forever.
    cfg = _handoff_cfg(tmp_path)
    (tmp_path / "handoff.json").write_text(json.dumps(
        {"set_ts": 1000.0, "until_ts": float("inf"), "reason": "x"}))
    assert M.load_cell_handoff(cfg, now=1000.0) is None
    assert M.load_cell_handoff(cfg, now=10_000_000.0) is None


def test_load_cell_handoff_rejects_nan_set_ts(tmp_path):
    # A NaN set_ts would make `now - set_ts > handoff_ttl_s` False forever too,
    # making the sanity TTL inert.
    cfg = _handoff_cfg(tmp_path)
    (tmp_path / "handoff.json").write_text(json.dumps(
        {"set_ts": float("nan"), "until_ts": 1004.0, "reason": "x"}))
    assert M.load_cell_handoff(cfg, now=1002.0) is None


def test_load_cell_handoff_rejects_future_set_ts(tmp_path):
    # Clock-skew guard: a forged/corrupt pair with set_ts far in the future
    # must not ride under the sanity-TTL age check (now - set_ts would be
    # negative, always <= handoff_ttl_s).
    cfg = _handoff_cfg(tmp_path)
    (tmp_path / "handoff.json").write_text(json.dumps(
        {"set_ts": 100_000.0, "until_ts": 100_004.0, "reason": "x"}))
    assert M.load_cell_handoff(cfg, now=1000.0) is None


def _loc_cfg(tmp_path, enabled=True):
    return M.Config(
        wans={"wan1": M.WanCfg("wan1", 1, "A"), "wan2": M.WanCfg("wan2", 2, "B")},
        relay=M.RelayCfg("http://x"),
        engarde=M.EngardeCfg("198.51.100.10", 59402),
        nft=M.NftCfg(), policy=M.PolicyCfg(), ui_listen="127.0.0.1:0",
        sbfd_local_state=str(tmp_path / "s.json"),
        runtime_state=str(tmp_path / "r.json"),
        persist_state=str(tmp_path / "p.json"),
        published_state=str(tmp_path / "pub.json"),
        location=M.LocationFecCfg(state_path=str(tmp_path / "location_fec.json"),
                                  enabled=enabled, stale_after_s=30.0),
    )


def test_load_location_floor_unconfigured_is_none(tmp_path):
    c = _loc_cfg(tmp_path)
    c.location = None
    assert M.load_location_floor(c, 1000.0) is None


def test_load_location_floor_missing_file_is_none(tmp_path):
    assert M.load_location_floor(_loc_cfg(tmp_path), 1000.0) is None


def test_load_location_floor_reads_fresh_levels(tmp_path):
    c = _loc_cfg(tmp_path)
    Path(c.location.state_path).write_text(json.dumps({
        "set_ts": 1000.0, "wans": {"wan1": {"level": 3, "reason": "learned x"}}}))
    out = M.load_location_floor(c, 1010.0)
    assert out == {"wan1": {"level": 3, "reason": "learned x"}}


def test_load_location_floor_stale_is_none(tmp_path):
    c = _loc_cfg(tmp_path)
    Path(c.location.state_path).write_text(json.dumps({
        "set_ts": 1000.0, "wans": {"wan1": {"level": 3, "reason": ""}}}))
    assert M.load_location_floor(c, 1031.0) is None


def test_load_location_floor_staleness_boundary_is_inclusive(tmp_path):
    """age == stale_after_s is still FRESH; anything past it is not.

    The daemon writes at 1 Hz and sbfd-ctl reads on its own tick, so the two
    clocks land on the boundary regularly -- an off-by-one here drops a live
    floor for one tick and the ratio visibly flaps."""
    c = _loc_cfg(tmp_path)                        # stale_after_s = 30.0
    Path(c.location.state_path).write_text(json.dumps({
        "set_ts": 1000.0, "wans": {"wan1": {"level": 3, "reason": "learned x"}}}))
    assert M.load_location_floor(c, 1030.0) == {"wan1": {"level": 3,
                                                         "reason": "learned x"}}
    assert M.load_location_floor(c, 1030.5) is None


def test_load_location_floor_drops_junk_entries(tmp_path):
    c = _loc_cfg(tmp_path)
    Path(c.location.state_path).write_text(json.dumps({
        "set_ts": 1000.0,
        "wans": {"wan1": {"level": "3"}, "wan2": {"level": True},
                 "wan3": {"level": 2, "reason": 7}, "wan4": "nope"}}))
    out = M.load_location_floor(c, 1001.0)
    assert out == {"wan3": {"level": 2, "reason": "7"}}


def test_load_location_floor_corrupt_is_none(tmp_path):
    c = _loc_cfg(tmp_path)
    Path(c.location.state_path).write_text("{not json")
    assert M.load_location_floor(c, 1001.0) is None


def test_effective_location_fec_enabled_false_when_unconfigured(tmp_path):
    c = _loc_cfg(tmp_path)
    c.location = None
    assert M.effective_location_fec_enabled(c, M.RuntimeOverlay()) is False


def test_effective_location_fec_enabled_uses_config_default(tmp_path):
    assert M.effective_location_fec_enabled(_loc_cfg(tmp_path, enabled=False),
                                            M.RuntimeOverlay()) is False
    assert M.effective_location_fec_enabled(_loc_cfg(tmp_path, enabled=True),
                                            M.RuntimeOverlay()) is True


def test_effective_location_fec_enabled_operator_override_wins(tmp_path):
    c = _loc_cfg(tmp_path, enabled=True)
    assert M.effective_location_fec_enabled(
        c, M.RuntimeOverlay(location_fec_enabled=False)) is False


def test_runtime_overlay_roundtrips_location_fec_enabled(tmp_path):
    c = _loc_cfg(tmp_path)
    M.save_runtime_overlay(c, M.RuntimeOverlay(location_fec_enabled=False,
                                               set_by="t", set_ts=1.0))
    assert M.load_runtime_overlay(c).location_fec_enabled is False


def test_runtime_overlay_ignores_a_non_bool_location_fec_enabled(tmp_path):
    """The API path rejects a non-bool; the FILE path did not.

    bool("false") is True, so a hand-edited persist file saying the operator
    turned the floor off would have turned it on. An unusable value means "no
    operator opinion", which lets the config default stand."""
    c = _loc_cfg(tmp_path, enabled=False)
    Path(c.runtime_state).write_text(json.dumps({
        "set_by": "hand", "set_ts": 1.0, "location_fec_enabled": "false"}))
    ov = M.load_runtime_overlay(c)
    assert ov.location_fec_enabled is None
    assert M.effective_location_fec_enabled(c, ov) is False   # config default


def test_runtime_overlay_still_honours_a_real_bool_location_fec_enabled(tmp_path):
    c = _loc_cfg(tmp_path, enabled=False)
    Path(c.runtime_state).write_text(json.dumps({
        "set_by": "ui", "set_ts": 1.0, "location_fec_enabled": True}))
    ov = M.load_runtime_overlay(c)
    assert ov.location_fec_enabled is True
    assert M.effective_location_fec_enabled(c, ov) is True


def test_runtime_overlay_ignores_a_non_bool_environmental_enabled(tmp_path):
    """Same hand-edit hazard as location_fec_enabled: bool("false") is True, so
    a persist file saying the operator turned environmental off would have
    turned it on. An unusable value means "no operator opinion"."""
    c = base_cfg(environmental=_env_cfg(tmp_path, enabled=False),
                 runtime_state=str(tmp_path / "r.json"),
                 persist_state=str(tmp_path / "p.json"))
    Path(c.persist_state).write_text(json.dumps({
        "set_by": "hand", "set_ts": 1.0, "environmental_enabled": "false"}))
    ov = M.load_runtime_overlay(c)
    assert ov.environmental_enabled is None
    assert M.effective_environmental_enabled(c, ov) is False   # config default


def test_runtime_overlay_still_honours_a_real_bool_environmental_enabled(tmp_path):
    c = base_cfg(environmental=_env_cfg(tmp_path, enabled=False),
                 runtime_state=str(tmp_path / "r.json"),
                 persist_state=str(tmp_path / "p.json"))
    Path(c.persist_state).write_text(json.dumps({
        "set_by": "ui", "set_ts": 1.0, "environmental_enabled": True}))
    ov = M.load_runtime_overlay(c)
    assert ov.environmental_enabled is True
    assert M.effective_environmental_enabled(c, ov) is True


def _oversized_set_ts_json(extra=""):
    """A JSON integer with more digits than a double can hold. json.loads keeps
    it as a Python int, and float() on it raises OverflowError rather than
    returning inf — so the finiteness guards downstream never get a chance."""
    return '{"set_ts": 1' + "0" * 400 + extra + '}'


def test_load_location_floor_survives_an_oversized_set_ts(tmp_path):
    # The controller tick calls this loader directly, so a raise here
    # interrupts failover for as long as the poisoned file sits there.
    c = _loc_cfg(tmp_path)
    Path(c.location.state_path).write_text(_oversized_set_ts_json(', "wans": {}'))
    assert M.load_location_floor(c, 1001.0) is None


def test_load_auto_override_survives_an_oversized_set_ts(tmp_path):
    # Same idiom, same tick, same one-word fix.
    c = base_cfg(environmental=M.EnvironmentalCfg(
        enabled=True, auto_override_path=str(tmp_path / "auto.json"),
        auto_override_ttl_s=180.0))
    Path(c.environmental.auto_override_path).write_text(
        _oversized_set_ts_json(', "mode": "full"'))
    assert M.load_auto_override(c, 1001.0) is None


def test_validate_runtime_payload_location_fec_enabled():
    ok, _ = M.validate_runtime_payload({"location_fec_enabled": True},
                                       wan_names={"wan1"})
    assert ok
    ok, err = M.validate_runtime_payload({"location_fec_enabled": "yes"},
                                         wan_names={"wan1"})
    assert not ok and "location_fec_enabled" in err


def test_load_location_floor_non_dict_wans_is_none(tmp_path):
    c = _loc_cfg(tmp_path)
    for bad_wans in ("nope", [1, 2], 7):
        Path(c.location.state_path).write_text(json.dumps(
            {"set_ts": 1000.0, "wans": bad_wans}))
        assert M.load_location_floor(c, 1001.0) is None


def test_load_location_floor_missing_wans_is_empty(tmp_path):
    c = _loc_cfg(tmp_path)
    Path(c.location.state_path).write_text(json.dumps({"set_ts": 1000.0}))
    assert M.load_location_floor(c, 1001.0) == {}


def test_load_location_floor_empty_wans_is_explicit_withdrawal(tmp_path):
    c = _loc_cfg(tmp_path)
    Path(c.location.state_path).write_text(json.dumps(
        {"set_ts": 1000.0, "wans": {}}))
    assert M.load_location_floor(c, 1001.0) == {}


def test_load_location_floor_rejects_nan_set_ts(tmp_path):
    c = _loc_cfg(tmp_path)
    Path(c.location.state_path).write_text(
        '{"set_ts": NaN, "wans": {"wan1": {"level": 9, "reason": ""}}}')
    assert M.load_location_floor(c, 1001.0) is None


def test_load_location_floor_rejects_future_set_ts(tmp_path):
    c = _loc_cfg(tmp_path)
    Path(c.location.state_path).write_text(json.dumps(
        {"set_ts": 9999999.0, "wans": {"wan1": {"level": 9, "reason": ""}}}))
    assert M.load_location_floor(c, 1001.0) is None
    Path(c.location.state_path).write_text(json.dumps(
        {"set_ts": 1005.0, "wans": {"wan1": {"level": 9, "reason": ""}}}))
    assert M.load_location_floor(c, 1001.0) == {"wan1": {"level": 9, "reason": ""}}


def test_load_auto_override_rejects_nan_set_ts(tmp_path):
    """json.loads accepts the bareword NaN, and every comparison against NaN is
    False — so `now - set_ts > ttl` would call it fresh forever."""
    c = base_cfg(environmental=_env_cfg(tmp_path))
    Path(c.environmental.auto_override_path).write_text(
        '{"set_ts": NaN, "force_full": true}')
    assert M.load_auto_override(c, 1001.0) is None


def test_load_auto_override_rejects_a_future_set_ts(tmp_path):
    c = base_cfg(environmental=_env_cfg(tmp_path))
    _write_override(c.environmental.auto_override_path, set_ts=9999999.0)
    assert M.load_auto_override(c, 1001.0) is None


def test_load_auto_override_allows_small_clock_skew(tmp_path):
    """Inside the 5 s skew band a slightly-ahead writer is still trusted."""
    c = base_cfg(environmental=_env_cfg(tmp_path))
    _write_override(c.environmental.auto_override_path, set_ts=1005.0,
                    force_full=True)
    ao = M.load_auto_override(c, 1001.0)
    assert ao is not None and ao.force_full is True
