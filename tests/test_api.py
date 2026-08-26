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


def test_save_runtime_overlay_is_atomic_against_interrupted_write(cfg, monkeypatch):
    # CR #15 / Greptile P2: the 0.5s controller loop reads runtime.json while
    # the API writes it. A non-atomic truncate-then-write can be observed as a
    # partial/empty file. An interrupted save must leave the previous COMPLETE
    # overlay intact, never a truncated one.
    c = cfg
    M.save_runtime_overlay(c, M.RuntimeOverlay(mode="master_backup", set_by="A", set_ts=1.0))

    def boom(*a, **k):
        raise RuntimeError("crash mid-publish")

    monkeypatch.setattr(M.os, "replace", boom)
    try:
        M.save_runtime_overlay(c, M.RuntimeOverlay(mode="full", set_by="B", set_ts=2.0))
    except RuntimeError:
        pass  # atomic impl raises here; the point is what survives on disk

    loaded = M.load_runtime_overlay(c)
    assert loaded.mode == "master_backup"  # old complete state, never partial


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


def test_run_controller_closes_pooled_relay_connections_on_exit(cfg, monkeypatch):
    """A stopped controller must not leave a relay connection pooled.

    The keep-alive pool is process-global, so anything left behind outlives the
    loop that opened it and would be handed to whatever runs next.
    """
    import threading as _t

    class FakeConn:
        def __init__(self): self.closed = False
        def close(self): self.closed = True

    monkeypatch.setattr(M, "apply_engarde_table_action", lambda action: None)
    monkeypatch.setattr(M, "read_engarde_table_default", lambda table: None)
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
    Path(cfg.sbfd_local_state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.sbfd_local_state).write_text("{}")

    pooled = FakeConn()
    M._relay_conns[("http", "198.51.100.10", 9275)] = pooled
    try:
        stop = _t.Event()
        _t.Thread(target=lambda: (__import__("time").sleep(0.6), stop.set()),
                  daemon=True).start()
        M.run_controller(cfg, stop_event=stop)
        # Observed before the teardown below, or the teardown would be what
        # satisfies the assertion instead of the controller.
        closed_by_controller = pooled.closed
        pool_after_exit = dict(M._relay_conns)
    finally:
        M.close_relay_conns()

    assert closed_by_controller is True, "pooled relay connection was never closed"
    assert not pool_after_exit, f"pool not emptied on exit: {pool_after_exit}"


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
                     ramp_down_hold_s=0, full_mode_backoff_fec="8:0", full_min_up_wans=3,
                     # distinct from fixed_ratio's default so a misordered
                     # post_relay_fec argument list can't pass unnoticed, and
                     # below the adaptive tier this test asserts so the floor
                     # doesn't mask what the loss table chose
                     floor_ratio="8:1"),
    )


def test_effective_fec_enabled_defaults_true_when_configured(cfg_with_fec):
    assert M.effective_fec_enabled(cfg_with_fec, M.RuntimeOverlay()) is True


def test_validate_runtime_payload_accepts_fec_mode():
    ok, err = M.validate_runtime_payload({"fec_mode": "min_adaptive"},
                                         wan_names={"wan1", "wan2"})
    assert ok and err is None


def test_validate_runtime_payload_rejects_bad_fec_mode():
    ok, err = M.validate_runtime_payload({"fec_mode": "magic"},
                                         wan_names={"wan1", "wan2"})
    assert not ok and "fec_mode" in err


def test_validate_runtime_payload_accepts_fec_fixed_ratio():
    ok, err = M.validate_runtime_payload({"fec_fixed_ratio": "20:1"},
                                         wan_names={"wan1", "wan2"})
    assert ok and err is None


def test_validate_runtime_payload_rejects_bad_fec_fixed_ratio():
    ok, err = M.validate_runtime_payload({"fec_fixed_ratio": "garbage"},
                                         wan_names={"wan1", "wan2"})
    assert not ok and "fec_fixed_ratio" in err


def test_effective_fec_mode_defaults_to_cfg_default(cfg_with_fec):
    assert M.effective_fec_mode(cfg_with_fec, M.RuntimeOverlay()) == "min_adaptive"


def test_effective_fec_mode_overlay_wins(cfg_with_fec):
    ov = M.RuntimeOverlay(fec_mode="fixed", fec_fixed_ratio="8:2")
    assert M.effective_fec_mode(cfg_with_fec, ov) == "fixed"
    assert M.effective_fec_fixed_ratio(cfg_with_fec, ov) == "8:2"


def test_effective_fec_mode_off_when_unconfigured(cfg):
    assert M.effective_fec_mode(cfg, M.RuntimeOverlay(fec_mode="adaptive")) == "off"


def test_effective_fec_mode_legacy_overlay_enabled_maps_to_adaptive(cfg_with_fec):
    assert M.effective_fec_mode(
        cfg_with_fec, M.RuntimeOverlay(fec_enabled=True)) == "adaptive"
    assert M.effective_fec_mode(
        cfg_with_fec, M.RuntimeOverlay(fec_enabled=False)) == "off"


def test_post_runtime_applies_fec_mode_and_fixed_ratio(cfg):
    from pathlib import Path
    Path(cfg.published_state).write_text("{}")
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        body = json.dumps({
            "mode": "full", "master_policy": "static_primary", "master_wan": "wan2",
            "egress_mode": "relay_vpn", "fec_mode": "fixed",
            "fec_fixed_ratio": "8:2", "persist": False,
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/runtime",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 200
        ov = M.load_runtime_overlay(cfg)
        assert ov.fec_mode == "fixed"
        assert ov.fec_fixed_ratio == "8:2"
        # Legacy flag kept in sync so a downgrade still sees the off/on intent.
        assert ov.fec_enabled is True
    finally:
        stop.set(); httpd.shutdown()


def test_runtime_post_null_floor_clears_override(cfg_with_fec):
    # cfg_with_fec configures floor_ratio="8:1" as the config default; the
    # posted override "8:2" is deliberately different so the two are never
    # confused. The snapshot's fec.floor_override is ov.fec_floor_ratio
    # (str or None) and fec.floor_ratio stays the effective floor, per the
    # sbfd_ctl "fec" snapshot dict — verified here through the same
    # load_runtime_overlay/effective_fec_floor_ratio helpers that build it.
    Path(cfg_with_fec.published_state).write_text("{}")
    stop = threading.Event()
    httpd = M.start_ui_server(cfg_with_fec, stop)
    try:
        port = httpd.server_address[1]

        def post(body):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/runtime",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=2) as r:
                return r.status

        status = post({
            "mode": "full", "master_policy": "static_primary", "master_wan": "wan2",
            "egress_mode": "relay_vpn", "fec_mode": "min_adaptive",
            "fec_floor_ratio": "8:2", "persist": False,
        })
        assert status == 200
        ov = M.load_runtime_overlay(cfg_with_fec)
        assert ov.fec_floor_ratio == "8:2"  # snapshot fec.floor_override == "8:2"

        status = post({
            "mode": "full", "master_policy": "static_primary", "master_wan": "wan2",
            "egress_mode": "relay_vpn", "fec_mode": "min_adaptive",
            "fec_floor_ratio": None, "persist": False,
        })
        assert status == 200
        ov = M.load_runtime_overlay(cfg_with_fec)
        assert ov.fec_floor_ratio is None  # snapshot fec.floor_override is None
        assert M.effective_fec_floor_ratio(cfg_with_fec, ov) == \
            cfg_with_fec.fec.floor_ratio  # fec.floor_ratio == config-resolved effective floor
    finally:
        stop.set()
        httpd.shutdown()


def test_runtime_post_null_fixed_clears_override(cfg_with_fec):
    # cfg_with_fec configures fixed_ratio="20:1" as the config default; the
    # posted override "12:4" is deliberately different so the two are never
    # confused. The snapshot's fec.fixed_override is ov.fec_fixed_ratio
    # (str or None) and fec.fixed_ratio stays the effective fixed ratio, per the
    # sbfd_ctl "fec" snapshot dict — verified here through the same
    # load_runtime_overlay/effective_fec_fixed_ratio helpers that build it.
    Path(cfg_with_fec.published_state).write_text("{}")
    stop = threading.Event()
    httpd = M.start_ui_server(cfg_with_fec, stop)
    try:
        port = httpd.server_address[1]

        def post(body):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/runtime",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=2) as r:
                return r.status

        status = post({
            "mode": "full", "master_policy": "static_primary", "master_wan": "wan2",
            "egress_mode": "relay_vpn", "fec_mode": "fixed",
            "fec_fixed_ratio": "12:4", "persist": False,
        })
        assert status == 200
        ov = M.load_runtime_overlay(cfg_with_fec)
        assert ov.fec_fixed_ratio == "12:4"  # snapshot fec.fixed_override == "12:4"

        status = post({
            "mode": "full", "master_policy": "static_primary", "master_wan": "wan2",
            "egress_mode": "relay_vpn", "fec_mode": "fixed",
            "fec_fixed_ratio": None, "persist": False,
        })
        assert status == 200
        ov = M.load_runtime_overlay(cfg_with_fec)
        assert ov.fec_fixed_ratio is None  # snapshot fec.fixed_override is None
        assert M.effective_fec_fixed_ratio(cfg_with_fec, ov) == \
            cfg_with_fec.fec.fixed_ratio  # fec.fixed_ratio == config-resolved effective fixed
    finally:
        stop.set()
        httpd.shutdown()


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
    # Local sbfd measures relay->client loss (1.5% on wan1) — that is pushed
    # to the relay, not used for our own leg. The relay-fetched snapshot
    # (client->relay direction: 4% on wan1) is what drives our TX ratio.
    monkeypatch.setattr(M, "read_local_sbfd_state",
        lambda p, m: M.StateSnapshot(ok=True, per_wan={
            "wan1": M.WanSample("UP", 10.0, 1.5, 100.0),
            "wan2": M.WanSample("UP", 12.0, 0.0, 100.0)}))
    monkeypatch.setattr(M, "fetch_remote_sbfd_state",
        lambda *a, **k: M.StateSnapshot(ok=True, per_wan={
            "wan1": M.WanSample("UP", 10.0, 4.0, 100.0),
            "wan2": M.WanSample("UP", 12.0, 0.0, 100.0)}))
    monkeypatch.setattr(M, "fetch_relay_fec",
        lambda url, t: {"ok": True, "error": None,
                        "data": {"enabled": True, "ratio": "8:2", "level": 1,
                                 "driving_loss_pct": 1.2, "since": 5.0}})
    pushed = []
    def fake_post(url, mode, fixed_ratio, floor_ratio, t, client_loss_pct=None,
                  wan_profile=None, signal_floor=None, location_level=None):
        pushed.append((floor_ratio, client_loss_pct))
        return True
    monkeypatch.setattr(M, "post_relay_fec", fake_post)
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
    assert t2o["ratio"] == "8:4"          # relay-measured wan1 4% -> level 2 -> 8:4
    assert t2o["driver_wan"] == "wan1"    # higher-loss WAN drives the ratio
    assert t2o["loss_source"] == "relay"  # driven by relay-measured loss, not local
    assert t2o["actuator_ok"] is True
    assert pushed and pushed[0][1] == 1.5  # our relay->client measurement got pushed
    assert pushed[0][0] == "8:1"           # the effective floor reached the relay
    assert fec["floor_ratio"] == "8:1"     # and the UI sees the same value
    o2t = fec["directions"]["relay_to_client"]
    assert o2t["ratio"] == "8:2"
    assert o2t["ok"] is True
    assert o2t["reconcile_pending"] is False


def test_run_controller_pushes_wan_profile_and_signal_floor_to_relay(tmp_path, monkeypatch):
    """C2/C3 (CodeRabbit): the fake_post used by
    test_run_controller_publishes_per_direction_fec discards wan_profile and
    signal_floor entirely, so a run_controller-level regression in either
    field would pass unnoticed. Configure a real wan1 profile + cell
    telemetry so the signal floor actually engages, and assert both fields
    reach the relay POST."""
    import threading as _t
    cell_state = tmp_path / "cell.json"
    cfg = M.Config(
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
                     ramp_down_hold_s=0, full_mode_backoff_fec="8:0",
                     full_min_up_wans=3, floor_ratio="8:1",
                     wan_profiles={"wan1": M.WanProfileCfg(
                         name="wan1", loss_table=fec_control.DEFAULT_CELL_LOSS_TABLE,
                         ramp_up_ticks=1, ramp_down_hold_s=0,
                         floor_ratio="8:0", signal_floor_fec="12:1")}),
        cell=M.CellTelemetryCfg(state_path=str(cell_state), wan="wan1",
                                stale_after_s=30.0, rsrq_degrade_db=-12.0,
                                rsrq_recover_db=-10.0, rsrp_degrade_dbm=-110.0),
    )
    # RSRQ well past the degrade threshold -> signal floor engages this tick.
    cell_state.write_text(json.dumps({"rsrq": -13.0, "rsrp": None,
                                      "set_ts": __import__("time").time()}))

    monkeypatch.setattr(M, "apply_nft_init", lambda c: None)
    monkeypatch.setattr(M, "list_current_drops", lambda c: set())
    monkeypatch.setattr(M, "apply_nft_diff", lambda c, a: None)
    monkeypatch.setattr(M, "apply_engarde_table_action", lambda a: None)
    monkeypatch.setattr(M, "read_engarde_table_default", lambda table: {"via": None, "dev": "wg0"})
    monkeypatch.setattr(M, "read_local_sbfd_state",
        lambda p, m: M.StateSnapshot(ok=True, per_wan={
            "wan1": M.WanSample("UP", 10.0, 1.5, 100.0),
            "wan2": M.WanSample("UP", 12.0, 0.0, 100.0)}))
    monkeypatch.setattr(M, "fetch_remote_sbfd_state",
        lambda *a, **k: M.StateSnapshot(ok=True, per_wan={
            "wan1": M.WanSample("UP", 10.0, 4.0, 100.0),
            "wan2": M.WanSample("UP", 12.0, 0.0, 100.0)}))
    monkeypatch.setattr(M, "fetch_relay_fec",
        lambda url, t: {"ok": True, "error": None,
                        "data": {"enabled": True, "ratio": "8:2", "level": 1,
                                 "driving_loss_pct": 1.2, "since": 5.0}})
    pushed = []
    def fake_post(url, mode, fixed_ratio, floor_ratio, t, client_loss_pct=None,
                  wan_profile=None, signal_floor=None, location_level=None):
        pushed.append({"wan_profile": wan_profile, "signal_floor": signal_floor})
        return True
    monkeypatch.setattr(M, "post_relay_fec", fake_post)
    monkeypatch.setattr(M.fec_control, "write_fifo", lambda path, ratio, logger=None: True)

    Path(cfg.sbfd_local_state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.sbfd_local_state).write_text("{}")

    stop = _t.Event()
    _t.Thread(target=lambda: (__import__("time").sleep(0.6), stop.set()), daemon=True).start()
    M.run_controller(cfg, stop_event=stop)

    assert pushed, "expected at least one relay POST"
    # wan1 is the driver (higher loss) -> its profile must be named on push.
    assert all(p["wan_profile"] == "wan1" for p in pushed)
    # RSRQ collapse -> the engaged signal floor must propagate too.
    assert all(p["signal_floor"] is True for p in pushed)


def test_run_controller_suppresses_signal_floor_when_full_mode_backoff_gate_closed(tmp_path, monkeypatch):
    """Twin of test_run_controller_pushes_wan_profile_and_signal_floor_to_relay
    (P2 review's deferred gap): same RSRQ collapse, but full_min_up_wans=2
    with both WANs UP closes the full-mode-backoff gate. fec_full_backoff
    wins over the raw signal-floor engagement (sbfd_ctl.py's
    fec_signal_floor_applied = fec_signal_engaged and not fec_full_backoff),
    so every relay post and the published signal_floor_active must both be
    False even though the radio is degraded."""
    import threading as _t
    cell_state = tmp_path / "cell.json"
    cfg = M.Config(
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
                     ramp_down_hold_s=0, full_mode_backoff_fec="8:0",
                     full_min_up_wans=2, floor_ratio="8:1",
                     wan_profiles={"wan1": M.WanProfileCfg(
                         name="wan1", loss_table=fec_control.DEFAULT_CELL_LOSS_TABLE,
                         ramp_up_ticks=1, ramp_down_hold_s=0,
                         floor_ratio="8:0", signal_floor_fec="12:1")}),
        cell=M.CellTelemetryCfg(state_path=str(cell_state), wan="wan1",
                                stale_after_s=30.0, rsrq_degrade_db=-12.0,
                                rsrq_recover_db=-10.0, rsrp_degrade_dbm=-110.0),
    )
    # RSRQ well past the degrade threshold -> would engage the signal floor
    # if not for the full-mode-backoff gate below.
    cell_state.write_text(json.dumps({"rsrq": -13.0, "rsrp": None,
                                      "set_ts": __import__("time").time()}))

    monkeypatch.setattr(M, "apply_nft_init", lambda c: None)
    monkeypatch.setattr(M, "list_current_drops", lambda c: set())
    monkeypatch.setattr(M, "apply_nft_diff", lambda c, a: None)
    monkeypatch.setattr(M, "apply_engarde_table_action", lambda a: None)
    monkeypatch.setattr(M, "read_engarde_table_default", lambda table: {"via": None, "dev": "wg0"})
    monkeypatch.setattr(M, "read_local_sbfd_state",
        lambda p, m: M.StateSnapshot(ok=True, per_wan={
            "wan1": M.WanSample("UP", 10.0, 1.5, 100.0),
            "wan2": M.WanSample("UP", 12.0, 0.0, 100.0)}))
    monkeypatch.setattr(M, "fetch_remote_sbfd_state",
        lambda *a, **k: M.StateSnapshot(ok=True, per_wan={
            "wan1": M.WanSample("UP", 10.0, 4.0, 100.0),
            "wan2": M.WanSample("UP", 12.0, 0.0, 100.0)}))
    monkeypatch.setattr(M, "fetch_relay_fec",
        lambda url, t: {"ok": True, "error": None,
                        "data": {"enabled": True, "ratio": "8:2", "level": 1,
                                 "driving_loss_pct": 1.2, "since": 5.0}})
    pushed = []
    def fake_post(url, mode, fixed_ratio, floor_ratio, t, client_loss_pct=None,
                  wan_profile=None, signal_floor=None, location_level=None):
        pushed.append({"wan_profile": wan_profile, "signal_floor": signal_floor})
        return True
    monkeypatch.setattr(M, "post_relay_fec", fake_post)
    monkeypatch.setattr(M.fec_control, "write_fifo", lambda path, ratio, logger=None: True)

    Path(cfg.sbfd_local_state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.sbfd_local_state).write_text("{}")

    stop = _t.Event()
    _t.Thread(target=lambda: (__import__("time").sleep(0.6), stop.set()), daemon=True).start()
    M.run_controller(cfg, stop_event=stop)

    assert pushed, "expected at least one relay POST"
    # Gate closed (2 WANs UP >= full_min_up_wans=2) -> backoff wins, signal
    # floor must never reach the relay despite the RSRQ collapse.
    assert all(p["signal_floor"] is False for p in pushed)
    snap = json.loads(Path(cfg.published_state).read_text())
    assert snap["fec"]["signal_floor_active"] is False


def test_run_controller_handoff_window_forces_full_and_publishes(tmp_path, monkeypatch):
    """Loop-level pin for the Task 1/2 seam: a live duplication window
    (cell_handoff.json) must force mode 'full' end-to-end through
    run_controller, publish the duplication block, and back FEC off to the
    full-mode ratio -- then release everything cleanly once the window
    expires, without losing the lifetime duplication counter."""
    import threading as _t
    import time as _time
    cell_state = tmp_path / "cell.json"
    handoff_path = tmp_path / "cell_handoff.json"
    cfg = M.Config(
        wans={"wan1": M.WanCfg("wan1", 1, "T-Mo"),
              "wan2": M.WanCfg("wan2", 2, "Satellite")},
        relay=M.RelayCfg("http://x/state", fec_url="http://relay:9276/fec"),
        engarde=M.EngardeCfg("198.51.100.10", 59402),
        nft=M.NftCfg(),
        policy=M.PolicyCfg(default_mode="master_backup",
                           default_master_policy="static_primary",
                           default_master_wan="wan1"),
        ui_listen="127.0.0.1:0",
        sbfd_local_state=str(tmp_path / "sbfd-state.json"),
        runtime_state=str(tmp_path / "runtime.json"),
        persist_state=str(tmp_path / "persist.json"),
        published_state=str(tmp_path / "published.json"),
        fec=M.FecCfg(enabled=True, fifo=str(tmp_path / "client.fifo"),
                     loss_table=fec_control.DEFAULT_LOSS_TABLE, ramp_up_ticks=1,
                     ramp_down_hold_s=0, full_mode_backoff_fec="8:0",
                     full_min_up_wans=2, floor_ratio="8:0",
                     # floor_ratio="8:0" is the operative guard: set equal to
                     # full_mode_backoff_fec, so even under the module default
                     # MODE_MIN_ADAPTIVE its hard floor couldn't lift the
                     # ratio back up and muddy this test's actual target (the
                     # backoff). mode=MODE_ADAPTIVE is belt-and-suspenders --
                     # it sidesteps the min_adaptive floor path entirely
                     # rather than relying on it to be a no-op.
                     mode=fec_control.MODE_ADAPTIVE),
        cell=M.CellTelemetryCfg(state_path=str(cell_state), wan="wan1",
                                stale_after_s=30.0, rsrq_degrade_db=-12.0,
                                rsrq_recover_db=-10.0, rsrp_degrade_dbm=-110.0,
                                handoff_path=str(handoff_path), handoff_ttl_s=30.0),
    )
    # No radio degradation in play here -- this test pins the handoff window
    # seam, not the signal floor.
    cell_state.write_text(json.dumps({"rsrq": -8.0, "rsrp": None,
                                      "set_ts": _time.time()}))
    now = _time.time()
    handoff_path.write_text(json.dumps({
        "set_ts": now, "until_ts": now + 4.0, "reason": "cell_change:1->2"}))

    monkeypatch.setattr(M, "apply_nft_init", lambda c: None)
    monkeypatch.setattr(M, "list_current_drops", lambda c: set())
    monkeypatch.setattr(M, "apply_nft_diff", lambda c, a: None)
    monkeypatch.setattr(M, "apply_engarde_table_action", lambda a: None)
    monkeypatch.setattr(M, "read_engarde_table_default", lambda table: {"via": None, "dev": "wg0"})
    monkeypatch.setattr(M, "read_local_sbfd_state",
        lambda p, m: M.StateSnapshot(ok=True, per_wan={
            "wan1": M.WanSample("UP", 10.0, 0.0, 100.0),
            "wan2": M.WanSample("UP", 12.0, 0.0, 100.0)}))
    monkeypatch.setattr(M, "fetch_remote_sbfd_state",
        lambda *a, **k: M.StateSnapshot(ok=True, per_wan={
            "wan1": M.WanSample("UP", 10.0, 0.0, 100.0),
            "wan2": M.WanSample("UP", 12.0, 0.0, 100.0)}))
    monkeypatch.setattr(M, "fetch_relay_fec",
        lambda url, t: {"ok": True, "error": None,
                        "data": {"enabled": True, "ratio": "8:0", "level": 0,
                                 "driving_loss_pct": 0.0, "since": 5.0}})
    monkeypatch.setattr(M, "post_relay_fec",
        lambda url, mode, fixed_ratio, floor_ratio, t, client_loss_pct=None,
               wan_profile=None, signal_floor=None, location_level=None: True)
    monkeypatch.setattr(M.fec_control, "write_fifo", lambda path, ratio, logger=None: True)

    Path(cfg.sbfd_local_state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.sbfd_local_state).write_text("{}")

    def read_published():
        return json.loads(Path(cfg.published_state).read_text())

    mid = {}
    stop = _t.Event()

    def driver():
        # A couple of 0.5s ticks -> the window must already be forcing full.
        _time.sleep(0.6)
        mid["snap"] = read_published()
        # Age the file out from under the controller instead of sleeping the
        # full 4s until_ts -- the loop must react to the rewrite on its very
        # next tick, so this keeps the test's wall-clock budget under ~1.5s.
        handoff_path.write_text(json.dumps({
            "set_ts": now, "until_ts": _time.time() - 1.0,
            "reason": "cell_change:1->2"}))
        # A couple more ticks -> the loop must have re-checked the (now
        # expired) window and reverted.
        _time.sleep(0.6)
        stop.set()

    t = _t.Thread(target=driver, daemon=True)
    t.start()
    M.run_controller(cfg, stop_event=stop)
    t.join(timeout=2)

    assert "snap" in mid, "expected a published snapshot while the window was open"
    dup_mid = mid["snap"]["duplication"]
    # Window open -> mode forced to 'full', duplication published active,
    # and FEC backed off (2 WANs UP >= full_min_up_wans -> full-mode ratio).
    assert mid["snap"]["mode"] == "full"
    assert dup_mid["active"] is True
    assert dup_mid["count"] == 1
    assert dup_mid["last_reason"] == "cell_change:1->2"
    assert mid["snap"]["fec"]["directions"]["client_to_relay"]["ratio"] == "8:0"

    final = read_published()
    dup_final = final["duplication"]
    # Window expired -> mode reverts to the configured default, duplication
    # reports inactive, but the lifetime count is never decremented.
    assert final["mode"] == "master_backup"
    assert dup_final["active"] is False
    assert dup_final["count"] == 1


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
    monkeypatch.setattr(M, "post_relay_fec", lambda url, mode, fixed_ratio, floor_ratio, t, client_loss_pct=None, wan_profile=None, signal_floor=None, location_level=None: False)
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
    # Distinctive markers for the two independent rx sources: client_to_relay's
    # rx comes from the relay fetch (decode outcomes measured AT THE RELAY of
    # our uplink), relay_to_client's rx comes from the local tracker (our own
    # decoder). If sbfd_ctl.py ever swapped these two at the call site, this
    # test must fail — see the swap experiment in the review report.
    monkeypatch.setattr(M, "fetch_relay_fec",
        lambda url, t: {"ok": True, "error": None,
                        "data": {"rx": {"delivered_per_s": 111.0}}})
    monkeypatch.setattr(M, "post_relay_fec", lambda url, mode, fixed_ratio, floor_ratio, t, client_loss_pct=None, wan_profile=None, signal_floor=None, location_level=None: True)
    monkeypatch.setattr(M.fec_control, "write_fifo", lambda path, ratio, logger=None: True)

    class FakeTracker:
        def snapshot(self, now):
            return {"tx_mbps": 4.2, "overhead_pct": 16.7, "sample_age_s": 6.0, "stale": False}

        def rx_snapshot(self, now):
            return {"delivered_per_s": 222.0}

    Path(cfg_with_fec.sbfd_local_state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg_with_fec.sbfd_local_state).write_text("{}")
    stop = _t.Event()
    _t.Thread(target=lambda: (__import__("time").sleep(0.6), stop.set()), daemon=True).start()
    M.run_controller(cfg_with_fec, stop_event=stop, wire_tracker=FakeTracker())

    snap = json.loads(Path(cfg_with_fec.published_state).read_text())
    assert snap["fec"]["directions"]["client_to_relay"]["wire"] == \
        {"tx_mbps": 4.2, "overhead_pct": 16.7, "sample_age_s": 6.0, "stale": False}
    # client_to_relay.rx: relay-fetch side (decode outcomes measured at the relay)
    assert snap["fec"]["directions"]["client_to_relay"]["rx"]["delivered_per_s"] == 111.0
    # relay_to_client.rx: local-tracker side (our own decoder)
    assert snap["fec"]["directions"]["relay_to_client"]["rx"]["delivered_per_s"] == 222.0


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
    monkeypatch.setattr(M, "post_relay_fec", lambda url, mode, fixed_ratio, floor_ratio, t, client_loss_pct=None, wan_profile=None, signal_floor=None, location_level=None: True)
    monkeypatch.setattr(M.fec_control, "write_fifo", lambda path, ratio, logger=None: True)
    Path(cfg_with_fec.sbfd_local_state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg_with_fec.sbfd_local_state).write_text("{}")
    stop = _t.Event()
    _t.Thread(target=lambda: (__import__("time").sleep(0.6), stop.set()), daemon=True).start()
    M.run_controller(cfg_with_fec, stop_event=stop)  # no wire_tracker
    snap = json.loads(Path(cfg_with_fec.published_state).read_text())
    assert snap["fec"]["directions"]["client_to_relay"]["wire"] is None


def test_post_runtime_accepts_maintenance_keys(cfg):
    # hour 0 is midnight and falsy: the handler must use the `in payload` idiom,
    # not `payload.get(k) or default`, or it would rewrite 0 into the default.
    Path(cfg.published_state).write_text("{}")
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        body = json.dumps({"maintenance_enabled": True,
                           "maintenance_hour": 0}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/runtime",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 200
        ov = M.load_runtime_overlay(cfg)
        assert ov.maintenance_enabled is True
        assert ov.maintenance_hour == 0
    finally:
        stop.set()
        httpd.shutdown()


def test_post_runtime_rejects_bool_maintenance_hour(cfg):
    # bool is an int subclass: a POSTed hour of `true` must be a 400, not a
    # reboot of both uplinks at hour 1.
    Path(cfg.published_state).write_text("{}")
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        body = json.dumps({"maintenance_hour": True}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/runtime",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=2)
        assert e.value.code == 400
        assert M.load_runtime_overlay(cfg).maintenance_hour is None
    finally:
        stop.set()
        httpd.shutdown()


def test_post_runtime_omitting_maintenance_keys_leaves_them_alone(cfg):
    # An unrelated Apply from the UI must not clear the maintenance schedule.
    M.save_runtime_overlay(cfg, M.RuntimeOverlay(
        maintenance_enabled=True, maintenance_hour=3))
    Path(cfg.published_state).write_text("{}")
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        body = json.dumps({"mode": "full"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/runtime",
            data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 200
        ov = M.load_runtime_overlay(cfg)
        assert ov.maintenance_enabled is True
        assert ov.maintenance_hour == 3
    finally:
        stop.set()
        httpd.shutdown()


def test_overlay_round_trips_fec_floor_ratio(cfg):
    ov = M.RuntimeOverlay(fec_floor_ratio="8:1", persist=True)
    M.save_runtime_overlay(cfg, ov)
    assert M.load_runtime_overlay(cfg).fec_floor_ratio == "8:1"


def test_overlay_missing_fec_floor_ratio_reads_as_none(cfg):
    # Pre-existing persist files have no such key; absence must not fault and
    # must fall back to config, preserving today's behavior.
    Path(cfg.runtime_state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.runtime_state).write_text(json.dumps({"mode": "master_backup"}))
    assert M.load_runtime_overlay(cfg).fec_floor_ratio is None


def test_effective_fec_floor_ratio_overlay_wins(cfg):
    ov = M.RuntimeOverlay(fec_floor_ratio="8:2")
    assert M.effective_fec_floor_ratio(cfg, ov) == "8:2"


def test_effective_fec_floor_ratio_falls_back_when_unconfigured(cfg):
    # The cfg fixture passes no fec= block, so cfg.fec is None and the module
    # default is the last fallback.
    assert cfg.fec is None
    assert M.effective_fec_floor_ratio(cfg, M.RuntimeOverlay()) == \
        fec_control.DEFAULT_FLOOR_RATIO


def test_validate_normalizes_fec_ratios_in_place():
    payload = {"fec_fixed_ratio": "5%", "fec_floor_ratio": "25%"}
    ok, err = M.validate_runtime_payload(payload, {"wan1", "wan2"})
    assert (ok, err) == (True, None)
    assert payload["fec_fixed_ratio"] == "20:1"
    assert payload["fec_floor_ratio"] == "8:2"


def test_validate_accepts_explicit_floor_ratio():
    payload = {"fec_floor_ratio": "8:1"}
    ok, err = M.validate_runtime_payload(payload, {"wan1"})
    assert (ok, err) == (True, None)
    assert payload["fec_floor_ratio"] == "8:1"


def test_validate_rejects_bad_floor_ratio():
    # None is deliberately excluded: it is the "auto" sentinel that clears the
    # operator override (see test_validate_runtime_null_ratios_clear), not a
    # bad value.
    for bad in ["abc", "200%", "0:1", "", 5]:
        ok, err = M.validate_runtime_payload({"fec_floor_ratio": bad}, {"wan1"})
        assert ok is False, bad
        assert "fec_floor_ratio" in err


def test_validate_rejects_bad_fixed_ratio():
    for bad in ["abc", "200%", "0:1", "", 5]:
        ok, err = M.validate_runtime_payload({"fec_fixed_ratio": bad}, {"wan1"})
        assert ok is False, bad
        assert "fec_fixed_ratio" in err


def test_validate_runtime_null_ratios_clear():
    ok, err = M.validate_runtime_payload({"fec_floor_ratio": None}, {"wan1", "wan2"})
    assert ok and err is None
    ok, _ = M.validate_runtime_payload({"fec_fixed_ratio": None}, {"wan1", "wan2"})
    assert ok
    ok, err = M.validate_runtime_payload({"fec_floor_ratio": 5}, {"wan1", "wan2"})
    assert not ok and "fec_floor_ratio" in err


def test_validate_leaves_absent_ratio_keys_absent():
    payload = {"mode": "master_backup"}
    ok, _ = M.validate_runtime_payload(payload, {"wan1"})
    assert ok is True
    assert "fec_floor_ratio" not in payload
    assert "fec_fixed_ratio" not in payload


def test_effective_fec_ratios_survive_a_hand_edited_overlay(cfg):
    # /var/lib and /etc are hand-editable. A junk ratio must degrade to the
    # fallback, not reach write_fifo and break the 0.5s control loop.
    ov = M.RuntimeOverlay(fec_floor_ratio="garbage", fec_fixed_ratio="9:")
    assert M.effective_fec_floor_ratio(cfg, ov) == fec_control.DEFAULT_FLOOR_RATIO
    assert M.effective_fec_fixed_ratio(cfg, ov) == fec_control.DEFAULT_FIXED_RATIO


def test_effective_fec_ratios_normalize_a_hand_edited_percent(cfg):
    ov = M.RuntimeOverlay(fec_floor_ratio="25%")
    assert M.effective_fec_floor_ratio(cfg, ov) == "8:2"


def test_api_fec_history_returns_samples(cfg):
    import fec_history as FH
    Path(cfg.published_state).write_text("{}")
    hist = FH.FecHistory()
    hist.append_from_directions(1000.0, {
        "client_to_relay": {"wire": {"tx_mbps": 1.5, "overhead_pct": 50.0},
                            "rx": {"delivered_per_s": 100.0, "recovered_per_s": 2.0,
                                   "lost_pkts_est_per_s": 0.0, "par_waste_per_s": 30.0}},
        "relay_to_client": {"wire": None, "rx": None},
    })
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop, fec_hist=hist)
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/fec_history", timeout=2) as r:
            body = json.loads(r.read())
        assert body["samples"][0]["c2r"]["delivered_per_s"] == 100.0
        assert body["samples"][0]["r2c"]["delivered_per_s"] is None
    finally:
        stop.set()
        httpd.shutdown()


def test_api_fec_history_without_history_is_empty_not_500(cfg):
    Path(cfg.published_state).write_text("{}")
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/fec_history", timeout=2) as r:
            body = json.loads(r.read())
        assert body == {"samples": []}
    finally:
        stop.set()
        httpd.shutdown()


def test_api_engarde_includes_wan_ifaces(cfg):
    # The UI can't tell a runtime-excluded WAN (show as standby) from a
    # config-excluded non-WAN like the LAN bridge (hide) by payload alone:
    # engarde reports both with status "excluded" and no dstAddress. The proxy
    # knows the managed WAN set, so it annotates the response with it.
    # WAN keys deliberately differ from iface names (and sort differently)
    # so the assertion catches returning sorted keys instead of ifaces.
    cfg.wans = {
        "cell": M.WanCfg("wwan0", 1, "Cell"),
        "sat": M.WanCfg("eth9", 2, "Satellite"),
    }
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Stub(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"type": "client", "interfaces": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # keep test output quiet
            pass

    stub = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    cfg.engarde.admin_url = f"http://127.0.0.1:{stub.server_address[1]}/api/v1/get-list"
    Path(cfg.published_state).write_text("{}")
    stop = threading.Event()
    httpd = M.start_ui_server(cfg, stop)
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/engarde", timeout=2) as r:
            body = json.loads(r.read())
        assert body["ok"] is True
        assert body["wan_ifaces"] == ["eth9", "wwan0"]
    finally:
        stop.set()
        httpd.shutdown()
        stub.shutdown()


def _run_with_location_floor(cfg, tmp_path, monkeypatch, write_ok):
    """Run the controller for a few ticks with a level-3 location floor
    published and the FIFO either accepting or refusing the write. Returns the
    published fec block."""
    import threading as _t, time as _time
    monkeypatch.setattr(M, "apply_nft_init", lambda c: None)
    monkeypatch.setattr(M, "list_current_drops", lambda c: set())
    monkeypatch.setattr(M, "apply_nft_diff", lambda c, a: None)
    monkeypatch.setattr(M, "apply_engarde_table_action", lambda a: None)
    monkeypatch.setattr(M, "read_engarde_table_default",
                        lambda table: {"via": None, "dev": "wg0"})
    # Zero loss everywhere: the adaptive engine sits at level 0, so anything
    # above the config floor on the wire came from the location floor alone.
    monkeypatch.setattr(M, "read_local_sbfd_state",
        lambda p, m: M.StateSnapshot(ok=True, per_wan={
            "wan1": M.WanSample("UP", 10.0, 0.0, 100.0),
            "wan2": M.WanSample("UP", 12.0, 0.0, 100.0)}))
    monkeypatch.setattr(M, "fetch_remote_sbfd_state",
                        lambda *a, **k: M.StateSnapshot(ok=True, per_wan={}))
    monkeypatch.setattr(M, "fetch_relay_fec",
        lambda url, t: {"ok": False, "data": None, "error": "unreachable"})
    monkeypatch.setattr(M, "post_relay_fec",
        lambda url, mode, fixed_ratio, floor_ratio, t, client_loss_pct=None,
               wan_profile=None, signal_floor=None, location_level=None: False)
    monkeypatch.setattr(M.fec_control, "write_fifo",
                        lambda path, ratio, logger=None: write_ok)

    # Both WANs carry the same floor, so whichever one the driver pick lands on
    # the floor applies and the test is not hostage to the driver heuristic.
    Path(cfg.location.state_path).write_text(json.dumps({
        "set_ts": _time.time(),
        "wans": {"wan1": {"level": 3, "reason": "learned dr79z6n"},
                 "wan2": {"level": 3, "reason": "learned dr79z6n"}}}))
    Path(cfg.sbfd_local_state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.sbfd_local_state).write_text("{}")

    stop = _t.Event()
    _t.Thread(target=lambda: (_time.sleep(0.6), stop.set()), daemon=True).start()
    M.run_controller(cfg, stop_event=stop)
    return json.loads(Path(cfg.published_state).read_text())["fec"]


def test_location_floor_not_active_when_the_fifo_refused_the_write(
        cfg_with_fec, tmp_path, monkeypatch):
    """`active` is a claim about the wire, so it must answer to the actuator.

    The ladder a few lines below already refuses to light dots for parity that
    a failed FIFO write never sent; the location floor's badge has to hold to
    the same standard, or the card says the vehicle is protected at exactly the
    place it isn't."""
    cfg_with_fec.location = M.LocationFecCfg(
        state_path=str(tmp_path / "location_fec.json"), enabled=True,
        stale_after_s=30.0)
    fec = _run_with_location_floor(cfg_with_fec, tmp_path, monkeypatch,
                                   write_ok=False)
    assert fec["location_floor"]["level"] == 3      # still reported as asked
    assert fec["location_floor"]["active"] is False
    # Nothing the actuator took, so nothing on the wire to credit it with.
    assert fec["directions"]["client_to_relay"]["ratio"] != "8:6"


def test_location_floor_active_when_the_fifo_took_the_write(
        cfg_with_fec, tmp_path, monkeypatch):
    cfg_with_fec.location = M.LocationFecCfg(
        state_path=str(tmp_path / "location_fec.json"), enabled=True,
        stale_after_s=30.0)
    fec = _run_with_location_floor(cfg_with_fec, tmp_path, monkeypatch,
                                   write_ok=True)
    assert fec["location_floor"]["active"] is True
    assert fec["directions"]["client_to_relay"]["ratio"] == "8:6"


def _write_location_floor(cfg, level, when=None):
    import time as _time
    Path(cfg.location.state_path).write_text(json.dumps({
        "set_ts": when if when is not None else _time.time(),
        "wans": {"wan1": {"level": level, "reason": "learned dr79z6n"},
                 "wan2": {"level": level, "reason": "learned dr79z6n"}}}))


def _capture_relay_pushes(cfg, tmp_path, monkeypatch, run_s=0.6, mutate=None):
    """Run the controller with zero loss everywhere and return the kwargs
    every post_relay_fec call was handed. `mutate` (a callable) runs on its own
    thread alongside the loop, so a test can change the floor mid-run."""
    import threading as _t, time as _time
    monkeypatch.setattr(M, "apply_nft_init", lambda c: None)
    monkeypatch.setattr(M, "list_current_drops", lambda c: set())
    monkeypatch.setattr(M, "apply_nft_diff", lambda c, a: None)
    monkeypatch.setattr(M, "apply_engarde_table_action", lambda a: None)
    monkeypatch.setattr(M, "read_engarde_table_default",
                        lambda table: {"via": None, "dev": "wg0"})
    # Zero loss: the quantized loss level in relay_desired stays put, so the
    # only thing that can trigger a second POST is the location level itself.
    monkeypatch.setattr(M, "read_local_sbfd_state",
        lambda p, m: M.StateSnapshot(ok=True, per_wan={
            "wan1": M.WanSample("UP", 10.0, 0.0, 100.0),
            "wan2": M.WanSample("UP", 12.0, 0.0, 100.0)}))
    monkeypatch.setattr(M, "fetch_remote_sbfd_state",
                        lambda *a, **k: M.StateSnapshot(ok=True, per_wan={}))
    monkeypatch.setattr(M, "fetch_relay_fec",
        lambda url, t: {"ok": False, "data": None, "error": "unreachable"})
    pushed = []

    def fake_post(url, mode, fixed_ratio, floor_ratio, t, client_loss_pct=None,
                  wan_profile=None, signal_floor=None, location_level=None):
        pushed.append({"wan_profile": wan_profile, "signal_floor": signal_floor,
                       "location_level": location_level})
        return True

    monkeypatch.setattr(M, "post_relay_fec", fake_post)
    monkeypatch.setattr(M.fec_control, "write_fifo",
                        lambda path, ratio, logger=None: True)
    Path(cfg.sbfd_local_state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.sbfd_local_state).write_text("{}")

    stop = _t.Event()
    _t.Thread(target=lambda: (_time.sleep(run_s), stop.set()), daemon=True).start()
    if mutate is not None:
        _t.Thread(target=mutate, daemon=True).start()
    M.run_controller(cfg, stop_event=stop)
    return pushed


def test_run_controller_pushes_the_location_level_to_the_relay(
        cfg_with_fec, tmp_path, monkeypatch):
    """The relay->client leg can only lift itself for a bad place if the level
    reaches it — this is the loop-level pin that it does."""
    cfg_with_fec.location = M.LocationFecCfg(
        state_path=str(tmp_path / "location_fec.json"), enabled=True,
        stale_after_s=30.0)
    _write_location_floor(cfg_with_fec, 3)
    pushed = _capture_relay_pushes(cfg_with_fec, tmp_path, monkeypatch)
    assert pushed, "expected at least one relay POST"
    assert all(p["location_level"] == 3 for p in pushed)


def test_run_controller_pushes_zero_location_level_when_the_toggle_is_off(
        cfg_with_fec, tmp_path, monkeypatch):
    """Toggled off, location_floor_for_driver returns level 0 — and 0 must be
    pushed, not withheld: it is what releases the relay's floor."""
    cfg_with_fec.location = M.LocationFecCfg(
        state_path=str(tmp_path / "location_fec.json"), enabled=False,
        stale_after_s=30.0)
    _write_location_floor(cfg_with_fec, 3)
    pushed = _capture_relay_pushes(cfg_with_fec, tmp_path, monkeypatch)
    assert pushed, "expected at least one relay POST"
    assert all(p["location_level"] == 0 for p in pushed)


def test_run_controller_reposts_when_only_the_location_level_changes(
        cfg_with_fec, tmp_path, monkeypatch):
    """The level belongs in relay_desired, or a change would sit unsent until
    the 30 s heartbeat. Nothing else in the desired tuple moves here."""
    import time as _time
    cfg_with_fec.relay = M.RelayCfg("http://x/state", fetch_interval_s=0.5,
                                    fec_url="http://relay:9276/fec")
    cfg_with_fec.location = M.LocationFecCfg(
        state_path=str(tmp_path / "location_fec.json"), enabled=True,
        stale_after_s=30.0)
    _write_location_floor(cfg_with_fec, 3)

    def drop_to_level_1():
        _time.sleep(0.8)
        _write_location_floor(cfg_with_fec, 1)

    pushed = _capture_relay_pushes(cfg_with_fec, tmp_path, monkeypatch,
                                   run_s=1.8, mutate=drop_to_level_1)
    levels = [p["location_level"] for p in pushed]
    assert levels[0] == 3
    assert levels[-1] == 1, f"expected a re-post at the new level, got {levels}"
