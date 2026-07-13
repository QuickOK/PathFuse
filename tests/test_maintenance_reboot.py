import json as _json
from pathlib import Path

import maintenance_reboot as M


def cfg_dict(tmp_path, **kw):
    base = {
        "published_state": str(tmp_path / "state.json"),
        "sbfd_state_path": str(tmp_path / "sbfd.json"),
        "window_path": str(tmp_path / "window.json"),
        "wan1": {"iface": "wan1",
                 "watchdog_bin": "/opt/sbfd-ctl/hotspot_watchdog.py",
                 "watchdog_config": "/etc/sbfd-ctl/hotspot-watchdog.json"},
        "wan2": {"iface": "wan2", "grpcurl_bin": "/usr/local/bin/grpcurl",
                 "addr": "192.0.2.1:9200", "min_uptime_s": 43200},
        "recovery_deadline_s": 600,
        "settle_s": 30,
        "notify_bin": "/bin/true",
        "notify_topic": "pathfusetest",
        "dry_run": True,
    }
    base.update(kw)
    return base


def write_cfg(tmp_path, **kw):
    p = tmp_path / "maintenance.json"
    p.write_text(_json.dumps(cfg_dict(tmp_path, **kw)))
    return M.load_config(str(p))


def test_load_config_defaults_and_types(tmp_path):
    cfg = write_cfg(tmp_path)
    assert cfg.wan2.addr == "192.0.2.1:9200"
    assert cfg.recovery_deadline_s == 600.0
    assert cfg.dry_run is True


def test_load_config_rejects_bad_values(tmp_path):
    import pytest
    p = tmp_path / "bad.json"
    p.write_text(_json.dumps(cfg_dict(tmp_path, recovery_deadline_s=0)))
    with pytest.raises(ValueError):
        M.load_config(str(p))


def test_should_run_only_at_the_configured_hour(tmp_path):
    pub = {"maintenance": {"configured": True, "enabled": True, "hour": 3}}
    ok, why = M.should_run(pub, now_local_hour=3)
    assert ok is True
    ok, why = M.should_run(pub, now_local_hour=4)
    assert ok is False
    assert "hour" in why


def test_should_run_false_when_toggle_off(tmp_path):
    pub = {"maintenance": {"configured": True, "enabled": False, "hour": 3}}
    ok, why = M.should_run(pub, now_local_hour=3)
    assert ok is False
    assert "disabled" in why


def test_should_run_false_when_unconfigured_or_absent(tmp_path):
    ok, why = M.should_run({"maintenance": {"configured": False}}, 3)
    assert ok is False
    ok, why = M.should_run({}, 3)
    assert ok is False


def test_read_published_is_fail_safe_when_stale(tmp_path):
    # A stale or missing published state must never license a reboot: we would
    # be rebooting on a schedule nobody can confirm is still current.
    p = tmp_path / "state.json"
    p.write_text(_json.dumps({"ts": 1000.0, "maintenance": {"enabled": True}}))
    assert M.read_published(str(p), now=1000.0, max_age_s=60) is not None
    assert M.read_published(str(p), now=1500.0, max_age_s=60) is None
    assert M.read_published(str(tmp_path / "nope.json"), 1000.0, 60) is None


def test_read_wan_states_maps_iface_to_state(tmp_path):
    p = tmp_path / "sbfd.json"
    p.write_text(_json.dumps({"timestamp": 1000.0, "sessions": {
        "s1": {"iface": "wan1", "state": "UP"},
        "s2": {"iface": "wan2", "state": "DOWN"},
    }}))
    assert M.read_wan_states(str(p), now=1000.0, max_age_s=30) == {
        "wan1": "UP", "wan2": "DOWN"}
    # stale => unknown, not "UP"
    assert M.read_wan_states(str(p), now=9999.0, max_age_s=30) == {}
