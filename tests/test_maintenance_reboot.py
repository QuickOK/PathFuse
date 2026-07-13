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


class FakeRunner:
    """Captures argv and replays canned results, like tests/test_actuator.py."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.kwargs = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.kwargs.append(kw)
        rc, out = self.results.pop(0) if self.results else (0, "{}")

        class P:
            returncode = rc
            stdout = out
            stderr = ""
        return P()


class RaisingRunner:
    """Simulates a runner that never returns: OSError (binary missing / not
    executable) or subprocess.TimeoutExpired (grpcurl hung past its own
    timeout)."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = []
        self.kwargs = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.kwargs.append(kw)
        raise self.exc


STATUS_IDLE = _json.dumps({"dishGetStatus": {
    "deviceInfo": {"bootcount": 1321, "softwareVersion": "2026.06.28"},
    "deviceState": {"uptimeS": 90000},
    "softwareUpdateState": "IDLE",
    "secondsUntilSwupdateRebootPossible": -1,
}})

STATUS_UPDATE_STAGED = _json.dumps({"dishGetStatus": {
    "deviceInfo": {"bootcount": 1321},
    "deviceState": {"uptimeS": 90000},
    "softwareUpdateState": "IDLE",
    "swupdateRebootReady": True,
    "secondsUntilSwupdateRebootPossible": 0,
}})

STATUS_APPLYING = _json.dumps({"dishGetStatus": {
    "deviceInfo": {"bootcount": 1321},
    "deviceState": {"uptimeS": 90000},
    "softwareUpdateState": "APPLYING",
}})

# Real observed grpcurl output: protojson renders the uint64 uptimeS field as
# a quoted JSON string, not a plain number. bootcount and
# secondsUntilSwupdateRebootPossible arrive as plain numbers today.
STATUS_STRING_UPTIME = _json.dumps({"dishGetStatus": {
    "bootcount": 1321,
    "deviceInfo": {"bootcount": 1321},
    "deviceState": {"uptimeS": "16610"},
    "softwareUpdateState": "IDLE",
    "secondsUntilSwupdateRebootPossible": -1,
}})

# A firmware could widen bootcount/secondsUntilSwupdateRebootPossible to a
# 64-bit field too; grpcurl would then quote them exactly like uptimeS.
STATUS_STRING_WIDENED_FIELDS = _json.dumps({"dishGetStatus": {
    "deviceInfo": {"bootcount": "1321"},
    "deviceState": {"uptimeS": "16610"},
    "softwareUpdateState": "IDLE",
    "secondsUntilSwupdateRebootPossible": "5",
}})

# grpcurl booleans must never be mistaken for numbers: isinstance(True, int)
# is True in Python, so a naive numeric check would let a bool through.
STATUS_BOOL_FIELDS = _json.dumps({"dishGetStatus": {
    "deviceInfo": {"bootcount": True},
    "deviceState": {"uptimeS": True},
    "softwareUpdateState": "IDLE",
    "secondsUntilSwupdateRebootPossible": True,
}})

NON_DICT_BODIES = ("[]", '"x"', "3", "null")


def dish(tmp_path, results):
    cfg = write_cfg(tmp_path)
    r = FakeRunner(results)
    return M.DishClient(cfg.wan2, runner=r), r


def test_status_parses_and_targets_the_configured_addr(tmp_path):
    d, r = dish(tmp_path, [(0, STATUS_IDLE)])
    st = d.status()
    assert st["deviceInfo"]["bootcount"] == 1321
    argv = r.calls[0]
    assert argv[0] == "/usr/local/bin/grpcurl"
    assert "192.0.2.1:9200" in argv
    assert "-plaintext" in argv


def test_status_none_on_grpcurl_failure(tmp_path):
    d, _ = dish(tmp_path, [(1, "")])
    assert d.status() is None


def test_bootcount_and_uptime(tmp_path):
    d, _ = dish(tmp_path, [(0, STATUS_IDLE), (0, STATUS_IDLE)])
    assert d.bootcount() == 1321
    assert d.uptime_s() == 90000


def test_update_staged_detects_reboot_ready(tmp_path):
    d, _ = dish(tmp_path, [(0, STATUS_UPDATE_STAGED)])
    assert d.update_staged() is True


def test_update_not_staged_when_seconds_negative(tmp_path):
    # swupdateRebootReady is omitted (proto3 default) when false — a missing
    # key must read as False, not as "unknown, go ahead".
    d, _ = dish(tmp_path, [(0, STATUS_IDLE)])
    assert d.update_staged() is False


def test_update_in_flight_blocks_on_applying(tmp_path):
    # Interrupting a firmware write is how terminals get bricked.
    d, _ = dish(tmp_path, [(0, STATUS_APPLYING)])
    assert d.update_in_flight() is True


def test_reboot_sends_plain_reboot_request(tmp_path):
    d, r = dish(tmp_path, [(0, "{}")])
    assert d.reboot() is True
    argv = r.calls[0]
    assert "SpaceX.API.Device.Device/Handle" in argv
    payload = _json.loads(argv[argv.index("-d") + 1])
    assert payload == {"reboot": {}}


def test_apply_update_sends_schedule_reboot(tmp_path):
    d, r = dish(tmp_path, [(0, "{}")])
    assert d.apply_update() is True
    argv = r.calls[0]
    payload = _json.loads(argv[argv.index("-d") + 1])
    assert payload == {"update": {"schedule_reboot": True}}


def test_reboot_false_on_grpcurl_failure(tmp_path):
    d, _ = dish(tmp_path, [(1, "")])
    assert d.reboot() is False


def test_uptime_s_accepts_protojson_string_encoded_uint64(tmp_path):
    # CONFIRMED bug: grpcurl quotes uptimeS ("16610") since it's a uint64;
    # this must parse to 16610.0, not silently return None on every call.
    d, _ = dish(tmp_path, [(0, STATUS_STRING_UPTIME)])
    assert d.uptime_s() == 16610.0


def test_bootcount_and_seconds_tolerate_string_encoding_too(tmp_path):
    # a future firmware could widen bootcount / secondsUntilSwupdateRebootPossible
    # to a 64-bit field, quoting them exactly like uptimeS; accessors must not break
    d, r = dish(tmp_path, [(0, STATUS_STRING_WIDENED_FIELDS),
                           (0, STATUS_STRING_WIDENED_FIELDS)])
    assert d.bootcount() == 1321
    assert d.update_staged() is True  # secs "5" >= 0 must still mean staged


def test_bootcount_rejects_bool(tmp_path):
    # a bool bootcount (isinstance(True, int) is True) must never be handed
    # back as the reboot receipt the sequencer compares before/after
    d, _ = dish(tmp_path, [(0, STATUS_BOOL_FIELDS)])
    assert d.bootcount() is None


def test_uptime_s_rejects_bool(tmp_path):
    # a bool uptimeS must not be coerced into a float via bool-as-int
    d, _ = dish(tmp_path, [(0, STATUS_BOOL_FIELDS)])
    assert d.uptime_s() is None


def test_update_staged_rejects_bool_seconds(tmp_path):
    # secondsUntilSwupdateRebootPossible: true must not satisfy the ">= 0" check
    d, _ = dish(tmp_path, [(0, STATUS_BOOL_FIELDS)])
    assert d.update_staged() is False


def test_status_none_on_non_dict_json_bodies(tmp_path):
    # grpcurl printing valid JSON that isn't an object ([]/"x"/3/null) must
    # yield None, not raise
    for body in NON_DICT_BODIES:
        d, _ = dish(tmp_path, [(0, body)])
        assert d.status() is None


def test_bootcount_none_on_non_dict_json_bodies(tmp_path):
    # this is where the AttributeError shipped: resp.get(...) on a list/str/int/None
    for body in NON_DICT_BODIES:
        d, _ = dish(tmp_path, [(0, body)])
        assert d.bootcount() is None


def test_uptime_s_none_on_non_dict_json_bodies(tmp_path):
    # a non-dict grpcurl body must not raise out of uptime_s()
    for body in NON_DICT_BODIES:
        d, _ = dish(tmp_path, [(0, body)])
        assert d.uptime_s() is None


def test_update_staged_false_on_non_dict_json_bodies(tmp_path):
    # a non-dict grpcurl body must not raise out of update_staged()
    for body in NON_DICT_BODIES:
        d, _ = dish(tmp_path, [(0, body)])
        assert d.update_staged() is False


def test_update_in_flight_false_on_non_dict_json_bodies(tmp_path):
    # a non-dict grpcurl body must not raise out of update_in_flight()
    for body in NON_DICT_BODIES:
        d, _ = dish(tmp_path, [(0, body)])
        assert d.update_in_flight() is False


def test_reboot_false_on_non_dict_json_bodies(tmp_path):
    # a non-dict grpcurl body must not raise out of reboot(), and must not
    # be mistaken for success
    for body in NON_DICT_BODIES:
        d, _ = dish(tmp_path, [(0, body)])
        assert d.reboot() is False


def test_apply_update_false_on_non_dict_json_bodies(tmp_path):
    # a non-dict grpcurl body must not raise out of apply_update(), and must
    # not be mistaken for success
    for body in NON_DICT_BODIES:
        d, _ = dish(tmp_path, [(0, body)])
        assert d.apply_update() is False


def test_status_none_when_grpcurl_binary_missing(tmp_path):
    # grpcurl missing / not executable raises OSError from subprocess.run;
    # that must yield None, never propagate
    cfg = write_cfg(tmp_path)
    r = RaisingRunner(OSError("no such file or directory"))
    d = M.DishClient(cfg.wan2, runner=r)
    assert d.status() is None


def test_status_none_when_grpcurl_times_out(tmp_path):
    # a hung grpcurl raises subprocess.TimeoutExpired; that must yield None,
    # never propagate and abort the nightly run mid-sequence
    import subprocess
    cfg = write_cfg(tmp_path)
    r = RaisingRunner(subprocess.TimeoutExpired(cmd="grpcurl", timeout=30))
    d = M.DishClient(cfg.wan2, runner=r)
    assert d.status() is None


def test_status_none_on_non_json_garbage(tmp_path):
    # grpcurl printing non-JSON garbage must yield None, not raise ValueError
    d, _ = dish(tmp_path, [(0, "not json at all {{{")])
    assert d.status() is None


def test_call_passes_max_time_and_a_longer_subprocess_timeout(tmp_path):
    # a hung grpcurl must self-terminate via -max-time before subprocess's
    # own timeout reaps it — if a later edit drops -max-time or shrinks the
    # subprocess timeout below it, this must fail
    d, r = dish(tmp_path, [(0, STATUS_IDLE)])
    d.status()
    argv = r.calls[0]
    assert "-max-time" in argv
    max_time = float(argv[argv.index("-max-time") + 1])
    kw = r.kwargs[0]
    assert "timeout" in kw
    assert kw["timeout"] > max_time
