import json
import threading
import urllib.request, urllib.error
from pathlib import Path

import pytest
import sbfd_ctl as M
import fec_control


@pytest.fixture
def cfg(tmp_path: Path):
    return M.Config(
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
    )


def test_validate_runtime_payload_accepts_valid():
    ok, err = M.validate_runtime_payload({
        "mode": "master_backup",
        "master_policy": "static_primary",
        "master_wan": "wan2",
        "persist": False,
    }, wan_names={"wan1", "wan2"})
    assert ok and err is None


def test_validate_runtime_payload_rejects_bad_mode():
    ok, err = M.validate_runtime_payload({"mode": "magic"}, wan_names={"wan1","wan2"})
    assert not ok and "mode" in err


def test_validate_runtime_payload_rejects_bad_policy():
    ok, err = M.validate_runtime_payload({"master_policy": "trick"}, wan_names={"wan1","wan2"})
    assert not ok and "policy" in err


def test_validate_runtime_payload_rejects_bad_wan():
    ok, err = M.validate_runtime_payload({"master_wan": "wan9"}, wan_names={"wan1","wan2"})
    assert not ok and "master_wan" in err


def test_api_get_state_returns_published_json(cfg):
    Path(cfg.published_state).write_text(json.dumps({"hello": "world"}))
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            body = json.loads(r.read())
        assert body == {"hello": "world"}
    finally:
        stop.set()
        httpd.shutdown()


def test_api_post_runtime_writes_overlay(cfg):
    Path(cfg.published_state).write_text("{}")
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        body = json.dumps({
            "mode": "master_backup",
            "master_policy": "static_primary",
            "master_wan": "wan2",
            "persist": False,
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/runtime",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 200
        ov = M.load_runtime_overlay(cfg)
        assert ov.mode == "master_backup"
        assert ov.master_policy == "static_primary"
    finally:
        stop.set()
        httpd.shutdown()


def test_api_post_runtime_400_on_bad_input(cfg):
    Path(cfg.published_state).write_text("{}")
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        body = json.dumps({"mode": "magic"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/runtime",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=2)
        assert ei.value.code == 400
    finally:
        stop.set()
        httpd.shutdown()


def test_runtime_overlay_roundtrip_preserves_egress_mode(cfg):
    ov = M.RuntimeOverlay(
        mode="full", master_policy="static_primary", master_wan="wan2",
        persist=True, set_by="test", set_ts=123.0, egress_mode="local_direct",
    )
    M.save_runtime_overlay(cfg, ov)
    loaded = M.load_runtime_overlay(cfg)
    assert loaded.egress_mode == "local_direct"


def test_runtime_overlay_default_egress_mode_is_none(cfg):
    ov = M.RuntimeOverlay()
    assert ov.egress_mode is None


def test_effective_policy_returns_egress_mode_from_overlay(cfg):
    ov = M.RuntimeOverlay(egress_mode="relay_direct")
    mode, policy, master_wan, egress_mode = M.effective_policy(cfg, ov)
    assert egress_mode == "relay_direct"


def test_effective_policy_returns_default_egress_mode_when_overlay_blank(cfg):
    ov = M.RuntimeOverlay()
    mode, policy, master_wan, egress_mode = M.effective_policy(cfg, ov)
    assert egress_mode == cfg.egress.default_mode


def test_validate_runtime_payload_accepts_egress_mode():
    ok, err = M.validate_runtime_payload(
        {"egress_mode": "local_direct"}, wan_names={"wan1","wan2"})
    assert ok and err is None


def test_validate_runtime_payload_rejects_bad_egress_mode():
    ok, err = M.validate_runtime_payload(
        {"egress_mode": "magic"}, wan_names={"wan1","wan2"})
    assert not ok and "egress_mode" in err


def test_post_runtime_accepts_egress_mode_when_master_up(cfg):
    Path(cfg.published_state).write_text(json.dumps({
        "client_local": {"wan1": {"state":"UP"}, "wan2": {"state":"UP"}},
    }))
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        body = json.dumps({
            "mode": "full", "master_policy": "static_primary",
            "master_wan": "wan2", "egress_mode": "local_direct", "persist": False,
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/runtime",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 200
        ov = M.load_runtime_overlay(cfg)
        assert ov.egress_mode == "local_direct"
    finally:
        stop.set(); httpd.shutdown()


def test_post_runtime_rejects_local_direct_when_master_down(cfg):
    # Pre-seed runtime.json with egress_mode="relay_vpn" so we can prove the 409
    # path leaves it untouched (default-RuntimeOverlay would also be is None,
    # making the post-condition trivially pass without proving anything).
    M.save_runtime_overlay(cfg, M.RuntimeOverlay(
        mode="full", master_policy="static_primary", master_wan="wan2",
        egress_mode="relay_vpn", set_by="test", set_ts=1.0,
    ))
    Path(cfg.published_state).write_text(json.dumps({
        "client_local": {"wan1": {"state":"UP"}, "wan2": {"state":"DOWN"}},
    }))
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        body = json.dumps({
            "mode": "full", "master_policy": "static_primary",
            "master_wan": "wan2", "egress_mode": "local_direct", "persist": False,
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/runtime",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=2)
        assert ei.value.code == 409
        # Pre-seeded "relay_vpn" must survive the 409.
        ov = M.load_runtime_overlay(cfg)
        assert ov.egress_mode == "relay_vpn"
    finally:
        stop.set(); httpd.shutdown()


def test_post_runtime_relay_vpn_mode_unaffected_by_master_down(cfg):
    Path(cfg.published_state).write_text(json.dumps({
        "client_local": {"wan1": {"state":"UP"}, "wan2": {"state":"DOWN"}},
    }))
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        body = json.dumps({
            "mode": "full", "master_policy": "static_primary",
            "master_wan": "wan2", "egress_mode": "relay_vpn", "persist": False,
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/runtime",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 200
    finally:
        stop.set(); httpd.shutdown()


def test_get_api_desired_egress_returns_current_state(cfg):
    Path(cfg.published_state).write_text(json.dumps({
        "ts": 1234.5,
        "master_wan": "wan2",
        "egress_mode": "local_direct",
    }))
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/desired_egress", timeout=2) as r:
            body = json.loads(r.read())
        assert body["mode"] == "local_direct"
        assert body["master_wan"] == "wan2"
        assert "ts" in body
    finally:
        stop.set(); httpd.shutdown()


def test_get_api_desired_egress_503_when_state_invalid_json(cfg):
    Path(cfg.published_state).write_text("not json{{")
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/desired_egress", timeout=2)
        assert ei.value.code == 503
    finally:
        stop.set(); httpd.shutdown()


def test_get_api_desired_egress_falls_back_to_default_mode_when_field_absent(cfg):
    # Pre-T7 snapshot shape — no egress_mode field. Endpoint must still serve
    # the config default rather than null/missing.
    Path(cfg.published_state).write_text(json.dumps({"ts": 1.0, "master_wan": "wan2"}))
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/desired_egress", timeout=2) as r:
            body = json.loads(r.read())
        assert body["mode"] == cfg.egress.default_mode
    finally:
        stop.set(); httpd.shutdown()


def test_get_api_desired_egress_503_when_state_not_published(cfg):
    # No published_state file at all.
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/desired_egress", timeout=2)
        assert ei.value.code == 503
    finally:
        stop.set(); httpd.shutdown()


def test_run_controller_calls_engarde_actuator_with_relay_vpn_action(cfg, monkeypatch):
    """Smoke: one tick of run_controller invokes apply_engarde_table_action with
    the relay_vpn action shape. Stops after a single tick via stop_event."""
    import threading as _t
    seen = []
    def fake_apply_engarde(action):
        seen.append(action)
    monkeypatch.setattr(M, "apply_engarde_table_action", fake_apply_engarde)
    monkeypatch.setattr(M, "read_engarde_table_default",
                        lambda table: {"via": "192.0.2.4", "dev": "wan2"})  # currently wrong → expect a replace
    monkeypatch.setattr(M, "apply_nft_init", lambda c: None)
    monkeypatch.setattr(M, "list_current_drops", lambda c: [])
    monkeypatch.setattr(M, "apply_nft_diff", lambda c, a: None)
    monkeypatch.setattr(M, "read_local_sbfd_state",
        lambda p, m: M.StateSnapshot(ok=True, per_wan={
            "wan1": M.WanSample("UP", 10.0, 0.0, 100.0),
            "wan2": M.WanSample("UP", 12.0, 0.0, 100.0)}))
    monkeypatch.setattr(M, "fetch_remote_sbfd_state",
        lambda *a, **k: M.StateSnapshot(ok=True, per_wan={}))
    monkeypatch.setattr(M, "read_managed_default", lambda metric=50: None)
    monkeypatch.setattr(M, "read_wan_gateway", lambda iface: "192.0.2.1")
    monkeypatch.setattr(M, "apply_route_action", lambda action, metric=50: None)

    Path(cfg.runtime_state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.runtime_state).write_text(json.dumps({"egress_mode": "relay_vpn"}))
    Path(cfg.sbfd_local_state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.sbfd_local_state).write_text("{}")

    stop = _t.Event()
    def stop_after_tick():
        import time; time.sleep(0.6); stop.set()
    _t.Thread(target=stop_after_tick, daemon=True).start()
    M.run_controller(cfg, stop_event=stop)

    assert seen, "apply_engarde_table_action was never called"
    assert seen[0]["dev"] == "wg0"
    assert seen[0]["via"] is None
    assert seen[0]["table"] == "engarde"


def test_runtime_overlay_roundtrip_preserves_fec_enabled(cfg):
    ov = M.RuntimeOverlay(persist=True, set_by="test", set_ts=1.0, fec_enabled=False)
    M.save_runtime_overlay(cfg, ov)
    assert M.load_runtime_overlay(cfg).fec_enabled is False


def test_runtime_overlay_default_fec_enabled_is_none(cfg):
    assert M.RuntimeOverlay().fec_enabled is None


def test_validate_runtime_payload_accepts_fec_enabled():
    ok, err = M.validate_runtime_payload({"fec_enabled": False}, wan_names={"wan1", "wan2"})
    assert ok and err is None


def test_validate_runtime_payload_rejects_bad_fec_enabled():
    ok, err = M.validate_runtime_payload({"fec_enabled": "no"}, wan_names={"wan1", "wan2"})
    assert not ok and "fec_enabled" in err


@pytest.fixture
def cfg_with_fec(tmp_path: Path):
    return M.Config(
        wans={"wan1": M.WanCfg("wan1", 1, "T-Mo"),
              "wan2": M.WanCfg("wan2", 2, "Satellite")},
        relay=M.RelayCfg("http://x/state", fec_url="http://relay:9276/fec"),
        engarde=M.EngardeCfg("198.51.100.10", 59402),
        nft=M.NftCfg(),
        policy=M.PolicyCfg(default_mode="full"),
        ui_listen="127.0.0.1:0",
        sbfd_local_state=str(tmp_path / "sbfd-state.json"),
        runtime_state=str(tmp_path / "runtime.json"),
        persist_state=str(tmp_path / "persist.json"),
        published_state=str(tmp_path / "published.json"),
        fec=M.FecCfg(enabled=True, fifo=str(tmp_path / "client.fifo"),
                     loss_table=fec_control.DEFAULT_LOSS_TABLE, ramp_up_ticks=1,
                     ramp_down_hold_s=0, full_mode_backoff_fec="8:0", full_min_up_wans=3),
    )


def test_effective_fec_enabled_defaults_true_when_configured(cfg_with_fec):
    assert M.effective_fec_enabled(cfg_with_fec, M.RuntimeOverlay()) is True


def test_effective_fec_enabled_overlay_false_wins(cfg_with_fec):
    assert M.effective_fec_enabled(cfg_with_fec, M.RuntimeOverlay(fec_enabled=False)) is False


def test_effective_fec_enabled_false_when_unconfigured(cfg):
    # cfg fixture has fec=None
    assert M.effective_fec_enabled(cfg, M.RuntimeOverlay(fec_enabled=True)) is False


def test_post_runtime_applies_fec_enabled(cfg):
    Path(cfg.published_state).write_text("{}")
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        body = json.dumps({
            "mode": "full", "master_policy": "static_primary", "master_wan": "wan2",
            "egress_mode": "relay_vpn", "fec_enabled": False, "persist": False,
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/runtime",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 200
        assert M.load_runtime_overlay(cfg).fec_enabled is False
    finally:
        stop.set(); httpd.shutdown()


def test_run_controller_publishes_per_direction_fec(cfg_with_fec, monkeypatch):
    import threading as _t
    monkeypatch.setattr(M, "apply_nft_init", lambda c: None)
    monkeypatch.setattr(M, "list_current_drops", lambda c: set())
    monkeypatch.setattr(M, "apply_nft_diff", lambda c, a: None)
    monkeypatch.setattr(M, "apply_engarde_table_action", lambda a: None)
    monkeypatch.setattr(M, "read_engarde_table_default", lambda table: {"via": None, "dev": "wg0"})
    monkeypatch.setattr(M, "read_local_sbfd_state",
        lambda p, m: M.StateSnapshot(ok=True, per_wan={
            "wan1": M.WanSample("UP", 10.0, 4.0, 100.0),
            "wan2": M.WanSample("UP", 12.0, 0.0, 100.0)}))
    monkeypatch.setattr(M, "fetch_remote_sbfd_state",
        lambda *a, **k: M.StateSnapshot(ok=True, per_wan={}))
    monkeypatch.setattr(M, "fetch_relay_fec",
        lambda url, t: {"ok": True, "error": None,
                        "data": {"enabled": True, "ratio": "8:2", "level": 1,
                                 "driving_loss_pct": 1.2, "since": 5.0}})
    monkeypatch.setattr(M, "post_relay_fec", lambda url, en, t: True)
    monkeypatch.setattr(M.fec_control, "write_fifo", lambda path, ratio, logger=None: True)

    Path(cfg_with_fec.sbfd_local_state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg_with_fec.sbfd_local_state).write_text("{}")

    stop = _t.Event()
    _t.Thread(target=lambda: (__import__("time").sleep(0.6), stop.set()), daemon=True).start()
    M.run_controller(cfg_with_fec, stop_event=stop)

    snap = json.loads(Path(cfg_with_fec.published_state).read_text())
    fec = snap["fec"]
    assert fec["configured"] is True
    assert fec["desired_enabled"] is True
    t2o = fec["directions"]["client_to_relay"]
    assert t2o["ratio"] == "8:4"          # wan1 4% loss -> level 2 -> 8:4
    assert t2o["driver_wan"] == "wan1"    # higher-loss WAN drives the ratio
    assert t2o["actuator_ok"] is True
    o2t = fec["directions"]["relay_to_client"]
    assert o2t["ratio"] == "8:2"
    assert o2t["ok"] is True
    assert o2t["reconcile_pending"] is False


def test_run_controller_disabled_and_relay_unreachable(cfg_with_fec, monkeypatch):
    import threading as _t
    # Operator has disabled FEC via the runtime overlay.
    M.save_runtime_overlay(cfg_with_fec, M.RuntimeOverlay(fec_enabled=False, set_by="test", set_ts=1.0))
    monkeypatch.setattr(M, "apply_nft_init", lambda c: None)
    monkeypatch.setattr(M, "list_current_drops", lambda c: set())
    monkeypatch.setattr(M, "apply_nft_diff", lambda c, a: None)
    monkeypatch.setattr(M, "apply_engarde_table_action", lambda a: None)
    monkeypatch.setattr(M, "read_engarde_table_default", lambda table: {"via": None, "dev": "wg0"})
    monkeypatch.setattr(M, "read_local_sbfd_state",
        lambda p, m: M.StateSnapshot(ok=True, per_wan={
            "wan1": M.WanSample("UP", 10.0, 4.0, 100.0),
            "wan2": M.WanSample("UP", 12.0, 0.0, 100.0)}))
    monkeypatch.setattr(M, "fetch_remote_sbfd_state",
        lambda *a, **k: M.StateSnapshot(ok=True, per_wan={}))
    monkeypatch.setattr(M, "fetch_relay_fec",
        lambda url, t: {"ok": False, "data": None, "error": "transport: unreachable"})
    monkeypatch.setattr(M, "post_relay_fec", lambda url, en, t: False)
    captured = []
    monkeypatch.setattr(M.fec_control, "write_fifo",
        lambda path, ratio, logger=None: (captured.append(ratio), True)[1])

    Path(cfg_with_fec.sbfd_local_state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg_with_fec.sbfd_local_state).write_text("{}")

    stop = _t.Event()
    _t.Thread(target=lambda: (__import__("time").sleep(0.6), stop.set()), daemon=True).start()
    M.run_controller(cfg_with_fec, stop_event=stop)

    snap = json.loads(Path(cfg_with_fec.published_state).read_text())
    fec = snap["fec"]
    assert fec["desired_enabled"] is False
    t2o = fec["directions"]["client_to_relay"]
    assert t2o["ratio"] == "8:0"
    assert t2o["enabled"] is False
    assert t2o["driver_wan"] is None
    assert t2o["driving_loss_pct"] is None
    assert "8:0" in captured            # forced-off ratio was written to the FIFO
    o2t = fec["directions"]["relay_to_client"]
    assert o2t["ok"] is False
    assert o2t["reconcile_pending"] is True


def test_run_controller_publishes_client_wire(cfg_with_fec, monkeypatch):
    import threading as _t
    monkeypatch.setattr(M, "apply_nft_init", lambda c: None)
    monkeypatch.setattr(M, "list_current_drops", lambda c: set())
    monkeypatch.setattr(M, "apply_nft_diff", lambda c, a: None)
    monkeypatch.setattr(M, "apply_engarde_table_action", lambda a: None)
    monkeypatch.setattr(M, "read_engarde_table_default", lambda table: {"via": None, "dev": "wg0"})
    monkeypatch.setattr(M, "read_local_sbfd_state",
        lambda p, m: M.StateSnapshot(ok=True, per_wan={
            "wan1": M.WanSample("UP", 10.0, 0.0, 100.0),
            "wan2": M.WanSample("UP", 12.0, 0.0, 100.0)}))
    monkeypatch.setattr(M, "fetch_remote_sbfd_state", lambda *a, **k: M.StateSnapshot(ok=True, per_wan={}))
    monkeypatch.setattr(M, "fetch_relay_fec", lambda url, t: {"ok": False, "data": None, "error": "x"})
    monkeypatch.setattr(M, "post_relay_fec", lambda url, en, t: True)
    monkeypatch.setattr(M.fec_control, "write_fifo", lambda path, ratio, logger=None: True)

    class FakeTracker:
        def snapshot(self, now):
            return {"tx_mbps": 4.2, "overhead_pct": 16.7, "sample_age_s": 6.0, "stale": False}

    Path(cfg_with_fec.sbfd_local_state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg_with_fec.sbfd_local_state).write_text("{}")
    stop = _t.Event()
    _t.Thread(target=lambda: (__import__("time").sleep(0.6), stop.set()), daemon=True).start()
    M.run_controller(cfg_with_fec, stop_event=stop, wire_tracker=FakeTracker())

    snap = json.loads(Path(cfg_with_fec.published_state).read_text())
    assert snap["fec"]["directions"]["client_to_relay"]["wire"] == \
        {"tx_mbps": 4.2, "overhead_pct": 16.7, "sample_age_s": 6.0, "stale": False}


def test_run_controller_client_wire_none_without_tracker(cfg_with_fec, monkeypatch):
    import threading as _t
    monkeypatch.setattr(M, "apply_nft_init", lambda c: None)
    monkeypatch.setattr(M, "list_current_drops", lambda c: set())
    monkeypatch.setattr(M, "apply_nft_diff", lambda c, a: None)
    monkeypatch.setattr(M, "apply_engarde_table_action", lambda a: None)
    monkeypatch.setattr(M, "read_engarde_table_default", lambda table: {"via": None, "dev": "wg0"})
    monkeypatch.setattr(M, "read_local_sbfd_state",
        lambda p, m: M.StateSnapshot(ok=True, per_wan={"wan1": M.WanSample("UP", 10.0, 0.0, 100.0),
                                                        "wan2": M.WanSample("UP", 12.0, 0.0, 100.0)}))
    monkeypatch.setattr(M, "fetch_remote_sbfd_state", lambda *a, **k: M.StateSnapshot(ok=True, per_wan={}))
    monkeypatch.setattr(M, "fetch_relay_fec", lambda url, t: {"ok": False, "data": None, "error": "x"})
    monkeypatch.setattr(M, "post_relay_fec", lambda url, en, t: True)
    monkeypatch.setattr(M.fec_control, "write_fifo", lambda path, ratio, logger=None: True)
    Path(cfg_with_fec.sbfd_local_state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg_with_fec.sbfd_local_state).write_text("{}")
    stop = _t.Event()
    _t.Thread(target=lambda: (__import__("time").sleep(0.6), stop.set()), daemon=True).start()
    M.run_controller(cfg_with_fec, stop_event=stop)  # no wire_tracker
    snap = json.loads(Path(cfg_with_fec.published_state).read_text())
    assert snap["fec"]["directions"]["client_to_relay"]["wire"] is None
