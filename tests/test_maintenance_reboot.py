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


def test_should_run_true_at_midnight(tmp_path):
    # hour 0 (midnight) is a valid configured hour and must not be treated as falsy
    pub = {"maintenance": {"configured": True, "enabled": True, "hour": 0}}
    ok, why = M.should_run(pub, now_local_hour=0)
    assert ok is True


def test_should_run_rejects_bool_hour_true(tmp_path):
    # a bool hour must never authorize a reboot, even though True == 1 in Python
    pub = {"maintenance": {"configured": True, "enabled": True, "hour": True}}
    ok, why = M.should_run(pub, now_local_hour=1)
    assert ok is False
    assert "hour" in why


def test_should_run_rejects_bool_hour_false(tmp_path):
    # a bool hour must never authorize a reboot, even at local hour 0 (midnight)
    pub = {"maintenance": {"configured": True, "enabled": True, "hour": False}}
    ok, why = M.should_run(pub, now_local_hour=0)
    assert ok is False
    assert "hour" in why


def test_should_run_rejects_non_dict_maintenance(tmp_path):
    # a maintenance value that parsed but isn't an object must fail safe, not raise
    ok, why = M.should_run({"maintenance": ["not", "a", "dict"]}, 3)
    assert ok is False
    ok, why = M.should_run({"maintenance": None}, 3)
    assert ok is False


def test_read_published_rejects_non_dict_json_bodies(tmp_path):
    # valid JSON that isn't an object (null, list, scalar) must return None, not raise
    for body in ("null", "[]", "123", '"x"'):
        p = tmp_path / "state.json"
        p.write_text(body)
        assert M.read_published(str(p), now=1000.0, max_age_s=60) is None


def test_read_wan_states_rejects_non_dict_json_bodies(tmp_path):
    # valid JSON that isn't an object (null, list, scalar) must return {}, not raise
    for body in ("null", "[]", "123", '"x"'):
        p = tmp_path / "sbfd.json"
        p.write_text(body)
        assert M.read_wan_states(str(p), now=1000.0, max_age_s=30) == {}


def test_read_wan_states_tolerates_malformed_sessions(tmp_path):
    # sessions as a list, a non-dict entry, and an unhashable iface must all be
    # skipped without raising and without inventing a phantom UP state
    p = tmp_path / "sbfd.json"
    p.write_text(_json.dumps({"timestamp": 1000.0, "sessions": ["not", "a", "dict"]}))
    assert M.read_wan_states(str(p), now=1000.0, max_age_s=30) == {}

    p.write_text(_json.dumps({"timestamp": 1000.0, "sessions": {
        "s1": "not-a-dict-entry",
        "s2": {"iface": ["wan1"], "state": "UP"},
    }}))
    assert M.read_wan_states(str(p), now=1000.0, max_age_s=30) == {}


def test_read_published_rejects_future_timestamp(tmp_path):
    # a clock-skewed future timestamp (e.g. pre-NTP-sync boot) must not look fresh
    p = tmp_path / "state.json"
    p.write_text(_json.dumps({"ts": 100000.0, "maintenance": {"enabled": True}}))
    assert M.read_published(str(p), now=1000.0, max_age_s=60) is None


def test_read_wan_states_rejects_future_timestamp(tmp_path):
    # a clock-skewed future timestamp must not make a stale UP look current
    p = tmp_path / "sbfd.json"
    p.write_text(_json.dumps({"timestamp": 100000.0, "sessions": {
        "s1": {"iface": "wan1", "state": "UP"},
    }}))
    assert M.read_wan_states(str(p), now=1000.0, max_age_s=30) == {}


def test_peer_of_raises_on_unrecognized_iface(tmp_path):
    import pytest
    # an unrecognized iface must raise, not silently hand back a plausible wan1
    cfg = write_cfg(tmp_path)
    with pytest.raises(ValueError):
        M.peer_of(cfg, "wan9")
