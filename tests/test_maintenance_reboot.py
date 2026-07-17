import json as _json
from pathlib import Path

import maintenance_reboot as M


def cfg_dict(tmp_path, **kw):
    base = {
        "published_state": str(tmp_path / "state.json"),
        "sbfd_state_path": str(tmp_path / "sbfd.json"),
        "window_path": str(tmp_path / "window.json"),
        # never the real /run/sbfd-ctl/maintenance.lock: a test must not be
        # able to lock out the live node's maintenance run
        "lock_path": str(tmp_path / "maintenance.lock"),
        "wan1": {"iface": "wan1",
                 "watchdog_bin": "/opt/sbfd-ctl/hotspot_watchdog.py",
                 "watchdog_config": "/etc/sbfd-ctl/hotspot-watchdog.json"},
        "wan2": {"iface": "wan2", "grpcurl_bin": "/usr/local/bin/grpcurl",
                 "addr": "192.0.2.1:9200"},
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


def test_load_config_defaults_the_lock_path(tmp_path):
    # the lock is not optional plumbing: a config that omits it must still get
    # one, or two concurrent runs could take both WANs down
    raw = cfg_dict(tmp_path)
    raw.pop("lock_path")
    p = tmp_path / "nolock.json"
    p.write_text(_json.dumps(raw))
    assert M.load_config(str(p)).lock_path == M.DEFAULT_LOCK


def test_load_config_rejects_identical_ifaces(tmp_path):
    # peer_of() pairs each WAN with the OTHER one; identical names make a WAN
    # its own peer, so peer_is_up() — the never-both-down guard — degenerates
    # into "is the WAN I am about to reboot up?", which is true right up to the
    # moment it stops being true
    import pytest
    p = tmp_path / "same.json"
    raw = cfg_dict(tmp_path)
    raw["wan2"]["iface"] = raw["wan1"]["iface"]
    p.write_text(_json.dumps(raw))
    with pytest.raises(ValueError):
        M.load_config(str(p))


def test_load_config_pins_ifaces_to_leg_labels(tmp_path):
    # The leg labels "wan1"/"wan2" are load-bearing: --only, the start-gate,
    # the ntfy reboot allow-list, and the scoped sudoers all key off them. An
    # iface renamed to anything else must be rejected up front, not left to
    # POST a dead command outside the allow-list.
    import pytest
    for wan, other in (("wan1", "eth9"), ("wan2", "eth9")):
        raw = cfg_dict(tmp_path)
        raw[wan]["iface"] = other
        p = tmp_path / f"renamed-{wan}.json"
        p.write_text(_json.dumps(raw))
        with pytest.raises(ValueError):
            M.load_config(str(p))


def test_load_config_pins_control_topic_charset(tmp_path):
    # control_topic is interpolated verbatim into the ntfy Actions curl header
    # by _reboot_button, so a comma/space/CRLF from an operator typo could
    # malform it. A bad topic must be rejected up front; empty (= no button)
    # and a clean [A-Za-z0-9_-] topic must both load fine.
    import pytest
    bad = cfg_dict(tmp_path, control_topic="bad topic,x")
    p = tmp_path / "bad-topic.json"
    p.write_text(_json.dumps(bad))
    with pytest.raises(ValueError):
        M.load_config(str(p))
    # empty is valid: it disables the button
    assert write_cfg(tmp_path, control_topic="").control_topic == ""
    # a well-formed topic loads unchanged
    assert write_cfg(tmp_path, control_topic="ctl-x9").control_topic == "ctl-x9"


def test_load_config_rejects_empty_ifaces(tmp_path):
    # an empty iface name matches no BFD session, so every state read for it is
    # "not UP" — a config that can never reboot anything, failing silently
    import pytest
    for blank in ("", "   "):
        raw = cfg_dict(tmp_path)
        raw["wan1"]["iface"] = blank
        p = tmp_path / "blank.json"
        p.write_text(_json.dumps(raw))
        with pytest.raises(ValueError):
            M.load_config(str(p))


def test_load_config_rejects_non_finite_durations(tmp_path):
    # json.loads accepts bareword NaN/Infinity, and every comparison against
    # NaN is False — so a NaN deadline sails through a bare "<= 0" check and
    # then makes await_up's bound meaningless
    import pytest
    p = tmp_path / "nonfinite.json"
    for key, body in (("recovery_deadline_s", "NaN"),
                      ("recovery_deadline_s", "Infinity"),
                      ("settle_s", "NaN"),
                      ("settle_s", "Infinity")):
        raw = _json.dumps(cfg_dict(tmp_path)).replace(
            f'"{key}": 600', f'"{key}": {body}').replace(
            f'"{key}": 30', f'"{key}": {body}')
        p.write_text(raw)
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


def test_read_published_rejects_bool_and_non_finite_timestamps(tmp_path):
    # isinstance(True, int) is True, and json.loads parses bareword NaN into a
    # float — with a NaN ts, abs(now - ts) > max_age_s is False, so an
    # arbitrarily stale file reads as FRESH and its schedule licenses a reboot
    p = tmp_path / "state.json"
    for ts in ("true", "false", "NaN", "Infinity", "-Infinity"):
        p.write_text('{"ts": %s, "maintenance": {"enabled": true}}' % ts)
        assert M.read_published(str(p), now=1000.0, max_age_s=60) is None


def test_read_wan_states_rejects_bool_and_non_finite_timestamps(tmp_path):
    # the same phantom-freshness bug, but here it invents a phantom UP: a
    # stale file read as fresh is how a WAN that is already down gets rebooted
    p = tmp_path / "sbfd.json"
    for ts in ("true", "false", "NaN", "Infinity", "-Infinity"):
        p.write_text('{"timestamp": %s, "sessions": {"s1": '
                     '{"iface": "wan1", "state": "UP"}}}' % ts)
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


def test_bootcount(tmp_path):
    d, _ = dish(tmp_path, [(0, STATUS_IDLE)])
    assert d.bootcount() == 1321


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


def test_update_staged_rejects_bool_seconds(tmp_path):
    # secondsUntilSwupdateRebootPossible: true must not satisfy the ">= 0" check
    d, _ = dish(tmp_path, [(0, STATUS_BOOL_FIELDS)])
    assert d.update_staged() is False


# float() accepts "nan"/"inf"/"-inf"/"Infinity" and only raises ValueError on
# genuinely non-numeric strings, so these quoted forms parse successfully and
# must be caught by an explicit finiteness check, not by the string-to-float
# conversion itself.
NON_FINITE_STRINGS = ("nan", "inf", "-inf", "Infinity")

# json.dumps emits bareword NaN/Infinity/-Infinity (no quotes) for these
# Python floats by default, and json.loads parses that bareword form back
# into the same non-finite floats, so a device could in principle send this
# shape too; _as_number must reject it exactly like the quoted-string form.
NON_FINITE_FLOATS = (float("nan"), float("inf"), float("-inf"))


def _status_with_non_finite(value):
    return _json.dumps({"dishGetStatus": {
        "deviceInfo": {"bootcount": value},
        "deviceState": {"uptimeS": value},
        "softwareUpdateState": "IDLE",
        "secondsUntilSwupdateRebootPossible": value,
    }})


def test_bootcount_none_on_non_finite_strings(tmp_path):
    # CONFIRMED bug: float("nan")/float("inf") succeed, so bootcount()'s
    # int(n) used to raise ValueError (NaN) or OverflowError (inf); this pins
    # that bootcount() instead returns None and never raises
    for s in NON_FINITE_STRINGS:
        d, _ = dish(tmp_path, [(0, _status_with_non_finite(s))])
        assert d.bootcount() is None


def test_bootcount_none_on_non_finite_float_values(tmp_path):
    # same guard, but for the bareword-float form (json.loads("NaN")) rather
    # than the quoted-string form
    for v in NON_FINITE_FLOATS:
        d, _ = dish(tmp_path, [(0, _status_with_non_finite(v))])
        assert d.bootcount() is None


def test_update_staged_false_on_non_finite_seconds_strings(tmp_path):
    # CONFIRMED bug: secondsUntilSwupdateRebootPossible "inf" >= 0 is True in
    # Python, so update_staged() used to wrongly report a staged update; this
    # pins that a non-finite seconds value must never be treated as staged
    for s in NON_FINITE_STRINGS:
        d, _ = dish(tmp_path, [(0, _status_with_non_finite(s))])
        assert d.update_staged() is False


def test_update_staged_false_on_non_finite_seconds_float_values(tmp_path):
    # same guard, but for the bareword-float form
    for v in NON_FINITE_FLOATS:
        d, _ = dish(tmp_path, [(0, _status_with_non_finite(v))])
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


# A payload whose nested objects are not objects. This bug class (".get() on a
# thing that turned out to be a list") has already shipped three times in this
# repo, one level up each time; every level is device-supplied and every level
# must be checked.
MALFORMED_NESTING = (
    _json.dumps({"dishGetStatus": []}),
    _json.dumps({"dishGetStatus": "nope"}),
    _json.dumps({"dishGetStatus": {"deviceInfo": [], "deviceState": []}}),
    _json.dumps({"dishGetStatus": {"deviceInfo": "x", "deviceState": 3}}),
)


def test_bootcount_none_on_malformed_nesting(tmp_path):
    # AttributeError out of bootcount() would abort the run mid-sequence
    for body in MALFORMED_NESTING:
        d, _ = dish(tmp_path, [(0, body)])
        assert d.bootcount() is None


def test_update_flags_false_on_malformed_nesting(tmp_path):
    # a malformed payload is UNAVAILABLE, never "no update staged, go ahead"
    for body in MALFORMED_NESTING:
        d, _ = dish(tmp_path, [(0, body)])
        assert d.update_staged() is False
        d, _ = dish(tmp_path, [(0, body)])
        assert d.update_in_flight() is False


def test_status_none_when_dish_get_status_is_not_an_object(tmp_path):
    # a non-dict dishGetStatus must read as "terminal unavailable", not as a
    # truthy status object that every accessor then indexes into
    d, _ = dish(tmp_path, [(0, _json.dumps({"dishGetStatus": [1, 2]}))])
    assert d.status() is None


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


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeDish:
    """Scriptable stand-in for DishClient, so the leg-2 paths can be driven
    without a device and without a real await_up sleep."""

    def __init__(self, boot=1321, staged=False,
                 in_flight=False, st=None, reboot_ok=True, apply_ok=True,
                 reboot_bumps=True, apply_bumps=True):
        self.boot = boot
        self._staged = staged
        self._in_flight = in_flight
        self._st = {} if st is None else st
        self.reboot_ok = reboot_ok
        self.apply_ok = apply_ok
        self.reboot_bumps = reboot_bumps
        self.apply_bumps = apply_bumps
        self.calls = []

    def status(self):
        return self._st

    def update_in_flight(self, st=None):
        return self._in_flight

    def bootcount(self, st=None):
        return self.boot

    def update_staged(self, st=None):
        return self._staged

    def _bump(self):
        if self.boot is not None:
            self.boot += 1

    def reboot(self):
        self.calls.append("reboot")
        if self.reboot_ok and self.reboot_bumps:
            self._bump()
        return self.reboot_ok

    def apply_update(self):
        self.calls.append("apply_update")
        if self.apply_ok and self.apply_bumps:
            self._bump()
        return self.apply_ok


BOTH_UP = {"wan1": "UP", "wan2": "UP"}
WAN1_DOWN = {"wan1": "DOWN", "wan2": "UP"}
WAN2_DOWN = {"wan1": "UP", "wan2": "DOWN"}


def both_up(*a, **k):
    return dict(BOTH_UP)


class FakeLink:
    """read_wan_states stand-in backed by a mutable BFD view, so a test can flip
    a WAN's state mid-leg — a reboot that really does take the link off the air,
    rather than a link that is politely constant while we pretend to reboot it."""

    def __init__(self, **state):
        self.state = dict(BOTH_UP)
        self.state.update(state)

    def __call__(self, *a, **k):
        return dict(self.state)


class DishThatDropsTheLink(FakeDish):
    """A terminal whose reboot (or staged update) genuinely takes wan2 down —
    and never brings it back. `on` picks which call does it."""

    def __init__(self, link, on="reboot", **kw):
        super().__init__(**kw)
        self.link = link
        self.on = on

    def _maybe_drop(self, call):
        if call == self.on:
            self.link.state["wan2"] = "DOWN"

    def reboot(self):
        out = super().reboot()
        self._maybe_drop("reboot")
        return out

    def apply_update(self):
        out = super().apply_update()
        self._maybe_drop("apply_update")
        return out


class StateTimeline:
    """read_wan_states stand-in that returns one frame per call, repeating the
    last frame forever. Lets a test drive a realistic UP -> DOWN -> UP timeline
    instead of pretending BFD state is constant."""

    def __init__(self, *frames):
        self.frames = list(frames)
        self.calls = 0

    def __call__(self, *a, **k):
        frame = self.frames[min(self.calls, len(self.frames) - 1)]
        self.calls += 1
        return dict(frame)


def recovered(*a, **k):
    return M.LegResult(True, M.Outcome.RECOVERED, "ok")


def instant(clk):
    """sleep() that advances a FakeClock instead of blocking: no test may ever
    wait out the real 600s recovery deadline."""
    return lambda s: clk.advance(s)


def test_window_file_round_trip_and_close(tmp_path):
    cfg = write_cfg(tmp_path)
    M.open_window(cfg, "wan1", now=1000.0, ttl_s=600)
    w = _json.loads(Path(cfg.window_path).read_text())
    assert w["wan"] == "wan1"
    assert w["until"] == 1600.0
    M.close_window(cfg)
    assert not Path(cfg.window_path).exists()


def test_close_window_is_idempotent(tmp_path):
    # closing a window that was never opened (or already closed) must not raise
    cfg = write_cfg(tmp_path)
    M.close_window(cfg)
    M.close_window(cfg)
    assert not Path(cfg.window_path).exists()


def test_notify_never_raises_when_the_notifier_is_missing(tmp_path):
    # a failed notification must never abort a reboot sequence
    cfg = write_cfg(tmp_path, dry_run=False,
                    notify_bin=str(tmp_path / "does-not-exist"))
    M.notify(cfg, "t", "high", "m")


def test_notify_logs_the_return_code_when_the_notifier_fails(tmp_path, caplog):
    # the spool helper exits 0 even when it only spooled the message, so a
    # NON-zero exit means something local is broken. Discarding the
    # CompletedProcess made a dropped page indistinguishable from a delivered
    # one, and left no trace at all.
    import logging
    cfg = write_cfg(tmp_path, dry_run=False, notify_bin="/bin/false")
    with caplog.at_level(logging.WARNING, logger="maintenance-reboot"):
        M.notify(cfg, "t", "high", "m")     # still must not raise
    assert any("notify exited 1" in r.getMessage() for r in caplog.records)


def test_notify_is_silent_when_the_notifier_succeeds(tmp_path, caplog):
    # ...and the happy path must not cry wolf
    import logging
    cfg = write_cfg(tmp_path, dry_run=False, notify_bin="/bin/true")
    with caplog.at_level(logging.WARNING, logger="maintenance-reboot"):
        M.notify(cfg, "t", "high", "m")
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_window_ttls_cover_the_whole_leg_not_just_the_recovery_poll(tmp_path):
    # the window must outlive the leg. recovery_deadline_s + settle_s did not:
    # leg 1 can also sit in the watchdog subprocess for its full timeout, and
    # leg 2 spends several 30s gRPC round-trips outside its two deadlines. A
    # window that expires mid-leg un-suppresses exactly the post-reboot BFD
    # flap it exists to hide.
    cfg = write_cfg(tmp_path)
    assert M.leg1_window_ttl(cfg) > (M.WATCHDOG_TIMEOUT_S
                                     + cfg.recovery_deadline_s + cfg.settle_s)
    assert M.leg2_window_ttl(cfg) > (2 * cfg.recovery_deadline_s + cfg.settle_s
                                     + 4 * M.GRPC_CALL_S)
    # and the watchdog subprocess is bounded by the same constant the TTL uses
    assert M.GRPC_CALL_S > M.GRPC_TIMEOUT_S


def test_reboot_wan1_bounds_the_watchdog_with_the_ttl_constant(
        tmp_path, monkeypatch):
    # if the subprocess timeout and the window TTL ever drift apart, the window
    # stops covering the leg again — pin them to the same constant
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    r = FakeRunner([(2, "skipped")])
    M.reboot_wan1(cfg, now=1000.0, runner=r)
    assert r.kwargs[0]["timeout"] == M.WATCHDOG_TIMEOUT_S


def test_run_once_windows_cover_their_legs(tmp_path, monkeypatch):
    # the TTL actually written to the window file, not just the helper
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    seen = []

    def peek(*a, **k):
        seen.append(_json.loads(Path(cfg.window_path).read_text()))
        return recovered()
    monkeypatch.setattr(M, "reboot_wan1", peek)
    monkeypatch.setattr(M, "reboot_wan2", peek)
    import time
    t0 = time.time()                    # leg 2's window is opened off time.time()
    M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert seen[0]["until"] >= 1000.0 + M.leg1_window_ttl(cfg)
    assert seen[1]["until"] >= t0 + M.leg2_window_ttl(cfg)


# -- the run lock -------------------------------------------------------


def run_main(tmp_path, monkeypatch, *extra):
    """Drive main() with a real argv, restoring the SIGTERM handler it
    installs so it cannot leak into the rest of the suite."""
    import signal
    import sys
    prev = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(sys, "argv", [
        "maintenance_reboot.py", "-c", str(tmp_path / "maintenance.json"),
        *extra])
    try:
        return M.main()
    finally:
        signal.signal(signal.SIGTERM, prev)


def test_acquire_lock_is_exclusive(tmp_path):
    # flock is per open-file-description, so a second acquire — from this
    # process or any other — must be refused rather than blocking
    cfg = write_cfg(tmp_path)
    first = M.acquire_lock(cfg.lock_path)
    assert first is not None
    try:
        assert M.acquire_lock(cfg.lock_path) is None
    finally:
        M.release_lock(first)
    second = M.acquire_lock(cfg.lock_path)      # released: available again
    assert second is not None
    M.release_lock(second)


def test_acquire_lock_returns_none_when_the_lock_file_cannot_be_created(tmp_path):
    # we cannot prove we are the only run, and an absent observation must never
    # AUTHORIZE an irreversible act: no lock, no run
    cfg = write_cfg(tmp_path, lock_path=str(tmp_path / "state.json" / "x.lock"))
    (tmp_path / "state.json").write_text("i am a file, not a directory")
    assert M.acquire_lock(cfg.lock_path) is None


def test_main_does_nothing_at_all_when_another_run_holds_the_lock(
        tmp_path, monkeypatch):
    # THE BOTH-WANS-DOWN RACE: an operator's `--now` (which bypasses the
    # schedule gate entirely) racing the timer's run. The second run must not
    # reboot, must not peer-check, and must not delete the live run's window —
    # deleting it would un-suppress the very outage it is about to cause.
    cfg = write_cfg(tmp_path, dry_run=False)
    held = M.acquire_lock(cfg.lock_path)
    assert held is not None
    try:
        M.open_window(cfg, "wan1", now=1000.0, ttl_s=600)   # run A's window
        calls = []
        monkeypatch.setattr(M, "run_once",
                            lambda *a, **k: calls.append("run_once"))
        monkeypatch.setattr(M, "close_window",
                            lambda *a, **k: calls.append("close_window"))
        monkeypatch.setattr(M, "read_wan_states",
                            lambda *a, **k: calls.append("peer_check") or {})
        rc = run_main(tmp_path, monkeypatch, "--now")
        assert rc == 0                       # a run we declined is a success
        assert calls == []                   # nothing happened at all
        assert Path(cfg.window_path).exists()   # run A's window is intact
    finally:
        M.release_lock(held)


def test_main_runs_and_releases_the_lock(tmp_path, monkeypatch):
    # the lock must not leak: the next run (the next hour's timer) has to be
    # able to take it
    cfg = write_cfg(tmp_path, dry_run=False)
    calls = []
    monkeypatch.setattr(M, "run_once",
                        lambda *a, **k: (calls.append("run_once"), 0)[1])
    assert run_main(tmp_path, monkeypatch, "--now") == 0
    assert calls == ["run_once"]
    after = M.acquire_lock(cfg.lock_path)
    assert after is not None
    M.release_lock(after)


def test_main_releases_the_lock_when_the_run_explodes(tmp_path, monkeypatch):
    # released on EVERY exit path, including an unexpected exception — a leaked
    # lock would disable the nightly reboot until the next boot
    import pytest
    cfg = write_cfg(tmp_path, dry_run=False)

    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(M, "run_once", boom)
    with pytest.raises(RuntimeError):
        run_main(tmp_path, monkeypatch, "--now")
    after = M.acquire_lock(cfg.lock_path)
    assert after is not None
    M.release_lock(after)


def test_await_up_returns_true_once_bfd_reports_up(tmp_path):
    cfg = write_cfg(tmp_path)
    clk = FakeClock()
    states = [{}, {}, {"wan1": "UP"}]

    def fake_states(*a, **k):
        return states.pop(0) if states else {"wan1": "UP"}

    slept = []
    ok = M.await_up(cfg, "wan1", deadline_s=600, clock=clk,
                    sleep=lambda s: (slept.append(s), clk.advance(s)),
                    states_fn=fake_states)
    assert ok is True
    assert slept                       # it polled rather than busy-waiting


def test_await_up_times_out(tmp_path):
    cfg = write_cfg(tmp_path)
    clk = FakeClock()
    ok = M.await_up(cfg, "wan1", deadline_s=60, clock=clk,
                    sleep=lambda s: clk.advance(s),
                    states_fn=lambda *a, **k: {"wan1": "DOWN"})
    assert ok is False


def test_await_up_honours_extra_ok_predicate(tmp_path):
    # wan2 must satisfy BFD *and* a bumped bootcount before we call it recovered.
    cfg = write_cfg(tmp_path)
    clk = FakeClock()
    bumped = iter([False, False, True])
    ok = M.await_up(cfg, "wan2", deadline_s=600, clock=clk,
                    sleep=lambda s: clk.advance(s),
                    states_fn=lambda *a, **k: {"wan2": "UP"},
                    extra_ok=lambda: next(bumped))
    assert ok is True


def test_await_up_gives_up_when_extra_ok_never_passes(tmp_path):
    # BFD UP forever but the receipt never arrives: bounded by the deadline,
    # never an infinite wait.
    cfg = write_cfg(tmp_path)
    clk = FakeClock()
    ok = M.await_up(cfg, "wan2", deadline_s=60, clock=clk,
                    sleep=lambda s: clk.advance(s),
                    states_fn=lambda *a, **k: {"wan2": "UP"},
                    extra_ok=lambda: False)
    assert ok is False


def test_await_up_treats_an_absent_wan_as_not_up(tmp_path):
    # read_wan_states returns {} on any problem; absent must never read as UP
    cfg = write_cfg(tmp_path)
    clk = FakeClock()
    ok = M.await_up(cfg, "wan1", deadline_s=60, clock=clk,
                    sleep=lambda s: clk.advance(s),
                    states_fn=lambda *a, **k: {})
    assert ok is False


def test_await_up_requires_an_observed_down_when_asked(tmp_path):
    # a WAN with no reboot receipt: the observed DOWN *is* the receipt, so a
    # pre-reboot UP that is still on the wire must not be credited as recovery
    cfg = write_cfg(tmp_path)
    clk = FakeClock()
    states = StateTimeline(BOTH_UP, BOTH_UP,      # stale pre-reboot UP
                           WAN1_DOWN,             # the carrier finally drops
                           BOTH_UP)               # and only now is it back
    ok = M.await_up(cfg, "wan1", deadline_s=600, clock=clk,
                    sleep=instant(clk), states_fn=states,
                    require_down_first=True)
    assert ok is True
    assert states.calls == 4            # it did not stop at the stale UP


def test_await_up_does_not_forge_a_down_from_an_unreadable_read(tmp_path):
    # {} means the read FAILED (file missing, sbfd restarting, stale past its
    # freshness window, clock stepped) — the ABSENCE of an observation, not an
    # observation of DOWN. Crediting it forges the observed-down that IS wan1's
    # only reboot receipt, and a receipt you can forge by not looking is no
    # receipt: the stale pre-reboot UP that follows must still not count.
    cfg = write_cfg(tmp_path)
    clk = FakeClock()
    states = StateTimeline({},            # unreadable: we know NOTHING
                           BOTH_UP)       # stale pre-reboot UP, forever
    ok = M.await_up(cfg, "wan1", deadline_s=60, clock=clk,
                    sleep=instant(clk), states_fn=states,
                    require_down_first=True)
    assert ok is False


def test_await_up_still_counts_a_fresh_read_with_no_session_as_down(tmp_path):
    # the flip side: a fresh read in which wan1's session is simply absent IS an
    # observation of not-UP, and must still satisfy require_down_first
    cfg = write_cfg(tmp_path)
    clk = FakeClock()
    states = StateTimeline({"wan2": "UP"},   # fresh, but wan1 has no session
                           BOTH_UP)
    ok = M.await_up(cfg, "wan1", deadline_s=60, clock=clk,
                    sleep=instant(clk), states_fn=states,
                    require_down_first=True)
    assert ok is True


def test_reboot_wan1_does_not_forge_a_down_from_an_unreadable_read(
        tmp_path, monkeypatch):
    # THE STRANDING BUG, re-opened by an unreadable read: wan1's carrier has not
    # dropped yet, one BFD read fails, and the stale UP behind it gets credited
    # as recovery — clearing the way to reboot wan2 while wan1 is still on its
    # way down. An unreadable read must credit nothing.
    cfg = write_cfg(tmp_path, dry_run=False, recovery_deadline_s=30)
    states = StateTimeline(BOTH_UP,      # the peer re-check
                           {},           # t+5: BFD state unreadable
                           BOTH_UP)      # t+10: stale pre-reboot UP, forever
    monkeypatch.setattr(M, "read_wan_states", states)
    clk = FakeClock()
    res = M.reboot_wan1(cfg, now=1000.0,
                        runner=FakeRunner([(0, "reboot issued")]),
                        sleep=instant(clk), clock=clk)
    assert res.ok is False                        # NOT "recovered"
    assert res.status is M.Outcome.NOT_ISSUED     # no down was ever observed
    assert clk.t - 1000.0 >= cfg.recovery_deadline_s


def test_reboot_wan1_refuses_when_peer_is_down(tmp_path, monkeypatch):
    # mechanism (b): each leg re-checks its peer immediately before rebooting,
    # catching a peer that died on its own mid-run
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states",
                        lambda *a, **k: {"wan1": "UP", "wan2": "DOWN"})
    r = FakeRunner([(0, "")])
    res = M.reboot_wan1(cfg, now=1000.0, runner=r)
    assert res.ok is False
    assert res.status is M.Outcome.SKIPPED
    assert r.calls == []               # the watchdog was never even invoked


def test_reboot_wan1_invokes_the_watchdog_one_shot_and_awaits_recovery(
        tmp_path, monkeypatch):
    # recovery is an OBSERVED down and then a return: the watchdog's one-shot
    # comes back when the admin API ACCEPTS the reboot, not when wan1 drops
    cfg = write_cfg(tmp_path, dry_run=False)
    states = StateTimeline(BOTH_UP,                 # the peer re-check
                           WAN1_DOWN, WAN1_DOWN,    # wan1 really goes down
                           BOTH_UP)                 # and comes back
    monkeypatch.setattr(M, "read_wan_states", states)
    clk = FakeClock()
    r = FakeRunner([(0, "reboot issued")])
    res = M.reboot_wan1(cfg, now=1000.0, runner=r,
                        sleep=instant(clk), clock=clk)
    assert res.ok is True
    assert res.status is M.Outcome.RECOVERED
    argv = r.calls[0]
    assert argv[0] == "/opt/sbfd-ctl/hotspot_watchdog.py"
    assert "--scheduled-reboot" in argv


def test_reboot_wan1_does_not_credit_a_stale_up_as_recovery(tmp_path, monkeypatch):
    # THE STRANDING BUG: the hotspot ACKs the reboot POST before its carrier
    # drops, so the first poll can still see the pre-reboot UP. Crediting that
    # stale UP as recovery is what lets the sequencer go on and reboot wan2
    # while wan1 is still on its way down — both WANs off the air at once.
    cfg = write_cfg(tmp_path, dry_run=False)
    states = StateTimeline(BOTH_UP,               # the peer re-check
                           BOTH_UP, BOTH_UP,      # STALE pre-reboot UP
                           WAN1_DOWN, WAN1_DOWN,  # the carrier drops at last
                           BOTH_UP)               # and only now is wan1 back
    monkeypatch.setattr(M, "read_wan_states", states)
    clk = FakeClock()
    res = M.reboot_wan1(cfg, now=1000.0,
                        runner=FakeRunner([(0, "reboot issued")]),
                        sleep=instant(clk), clock=clk)
    assert res.ok is True
    # ...but only after wan1 was seen DOWN. Success off the first (stale) poll
    # would have taken a single POLL_S; this took five.
    assert clk.t - 1000.0 == 5 * M.POLL_S


def test_reboot_wan1_fails_when_wan1_never_goes_down(tmp_path, monkeypatch):
    # wan1 UP for the whole deadline means the reboot never took. Report a
    # failure — no reboot was confirmed — rather than a success off a WAN that
    # never left, which would clear the way for wan2's reboot.
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    clk = FakeClock()
    res = M.reboot_wan1(cfg, now=1000.0,
                        runner=FakeRunner([(0, "reboot issued")]),
                        sleep=instant(clk), clock=clk)
    assert res.ok is False
    assert res.status is M.Outcome.NOT_ISSUED     # the link was never disturbed
    assert clk.t - 1000.0 >= cfg.recovery_deadline_s


def test_reboot_wan1_guard_skip_is_not_a_failure(tmp_path, monkeypatch):
    # watchdog exit 2 = skipped by a guard; tonight's reboot is never worth
    # risking the link, so a skip is reported as a skip, not a failure
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    res = M.reboot_wan1(cfg, now=1000.0,
                        runner=FakeRunner([(2, "peer is not UP")]))
    assert res.ok is False
    assert res.status is M.Outcome.SKIPPED


def test_reboot_wan1_fails_when_wan1_never_returns(tmp_path, monkeypatch):
    # wan1 went down and stayed down: this, and only this, is the outage that
    # pages the operator
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", lambda *a, **k: dict(WAN1_DOWN))
    clk = FakeClock()
    res = M.reboot_wan1(cfg, now=1000.0,
                        runner=FakeRunner([(0, "reboot issued")]),
                        sleep=instant(clk), clock=clk)
    assert res.ok is False
    assert res.status is M.Outcome.NOT_RETURNED
    assert "did not return" in res.reason


def test_reboot_wan1_rejected_request_is_not_reported_as_a_lost_wan(
        tmp_path, monkeypatch):
    # the watchdog refusing the reboot (rc != 0) means wan1 never went down;
    # that must be distinguishable from wan1 failing to come back
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    res = M.reboot_wan1(cfg, now=1000.0,
                        runner=FakeRunner([(1, "admin API rejected it")]))
    assert res.ok is False
    assert res.status is M.Outcome.NOT_ISSUED


def test_reboot_wan1_survives_a_missing_watchdog_binary(tmp_path, monkeypatch):
    # an OSError from the watchdog invocation must be a failed leg, not a
    # traceback that skips the finally-close of the window
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    res = M.reboot_wan1(cfg, now=1000.0,
                        runner=RaisingRunner(OSError("nope")))
    assert res.ok is False


def test_reboot_wan1_rejected_request_pages_when_wan1_is_down(
        tmp_path, monkeypatch):
    # THE BUG: a non-zero exit from the watchdog is not proof wan1 stayed up —
    # rebooting over the admin API tears down the connection as the device
    # goes down, so this exit is equally consistent with the reboot having
    # succeeded and wan1 never coming back. A real outage must still page.
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", lambda *a, **k: dict(WAN1_DOWN))
    res = M.reboot_wan1(cfg, now=1000.0,
                        runner=FakeRunner([(1, "admin API rejected it")]))
    assert res.ok is False
    assert res.status is M.Outcome.NOT_RETURNED
    sent = []
    monkeypatch.setattr(M, "notify", lambda cfg, t, p, m, actions=None: sent.append((t, p)))
    M.report_leg(cfg, "wan1", res)
    assert any(p == "high" for _t, p in sent)


def test_reboot_wan1_rejected_request_does_not_page_when_wan1_stayed_up(
        tmp_path, monkeypatch):
    # the flip side of the same fix: wan1 genuinely never left, so a rejected
    # request must stay informational, not escalate to a high-priority page
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    res = M.reboot_wan1(cfg, now=1000.0,
                        runner=FakeRunner([(1, "admin API rejected it")]))
    assert res.ok is False
    assert res.status is M.Outcome.NOT_ISSUED
    sent = []
    monkeypatch.setattr(M, "notify", lambda cfg, t, p, m, actions=None: sent.append((t, p)))
    M.report_leg(cfg, "wan1", res)
    assert not any(p == "high" for _t, p in sent)


def test_reboot_wan1_watchdog_invocation_error_pages_when_wan1_is_down(
        tmp_path, monkeypatch):
    # an OSError/TimeoutExpired from the invocation itself is just as
    # consistent with a reboot that succeeded and took wan1 off the air for
    # good as it is with a harmless invocation glitch — ask the link
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", lambda *a, **k: dict(WAN1_DOWN))
    res = M.reboot_wan1(cfg, now=1000.0,
                        runner=RaisingRunner(OSError("nope")))
    assert res.ok is False
    assert res.status is M.Outcome.NOT_RETURNED
    sent = []
    monkeypatch.setattr(M, "notify", lambda cfg, t, p, m, actions=None: sent.append((t, p)))
    M.report_leg(cfg, "wan1", res)
    assert any(p == "high" for _t, p in sent)


def test_classify_by_link_pages_on_an_unreadable_bfd_read(tmp_path, monkeypatch):
    # classify_by_link is the single decision point for whether a human gets
    # woken. An unreadable/stale read (read_wan_states returning {}) is the
    # ABSENCE of an observation, not an observation of "still UP", and an
    # absent observation must never SILENCE a page.
    cfg = write_cfg(tmp_path)
    monkeypatch.setattr(M, "read_wan_states", lambda *a, **k: {})
    res = M.classify_by_link(cfg, "wan1", M.Outcome.NOT_ISSUED,
                             "up reason", "down reason")
    assert res.status is M.Outcome.NOT_RETURNED
    assert res.reason == "down reason"


def test_reboot_wan2_reboots_a_young_but_reachable_dish(tmp_path, monkeypatch):
    # the uptime guard is gone: a dish that was power-cycled recently (the
    # operator unplugs Starlink at building stops, so its uptime is routinely
    # low at the maintenance hour) must still get its reboot, not be skipped
    # for being "too young"
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    clk = FakeClock()
    d = FakeDish()  # reachable, IDLE, bootcount readable
    res = M.reboot_wan2(cfg, now=1000.0, client=d,
                        sleep=instant(clk), clock=clk)
    assert res.ok is True
    assert res.status is M.Outcome.RECOVERED
    assert d.calls == ["reboot"]


def test_reboot_wan2_skips_when_an_update_is_in_flight(tmp_path):
    # interrupting a firmware write is how terminals get bricked
    cfg = write_cfg(tmp_path, dry_run=False)
    d = FakeDish(in_flight=True)
    res = M.reboot_wan2(cfg, now=1000.0, client=d)
    assert res.ok is False
    assert res.status is M.Outcome.SKIPPED
    assert d.calls == []


def test_reboot_wan2_refuses_when_peer_is_down(tmp_path, monkeypatch):
    # mechanism (b) for leg 2: re-check wan1 immediately before issuing
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", lambda *a, **k: dict(WAN1_DOWN))
    d = FakeDish()
    res = M.reboot_wan2(cfg, now=1000.0, client=d)
    assert res.ok is False
    assert res.status is M.Outcome.SKIPPED
    assert d.calls == []               # never disturb the last standing WAN


def test_reboot_wan2_recovers_when_bootcount_bumps(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    clk = FakeClock()
    d = FakeDish()
    res = M.reboot_wan2(cfg, now=1000.0, client=d,
                        sleep=instant(clk), clock=clk)
    assert res.ok is True
    assert res.status is M.Outcome.RECOVERED
    assert d.calls == ["reboot"]


def test_reboot_wan2_requires_a_bootcount_bump_not_just_bfd(tmp_path, monkeypatch):
    # BFD returning only proves the PATH recovered; a reboot request that was
    # silently dropped must not be reported as a successful reboot
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    clk = FakeClock()
    d = FakeDish(reboot_bumps=False)
    res = M.reboot_wan2(cfg, now=1000.0, client=d,
                        sleep=instant(clk), clock=clk)
    assert res.ok is False
    assert d.calls == ["reboot"]
    # ...and BFD stayed UP the whole time, so the link was never disturbed: a
    # dropped reboot is a failed maintenance, not a WAN that did not come back
    assert res.status is M.Outcome.NOT_ISSUED


def test_reboot_wan2_rejected_request_is_not_reported_as_a_lost_wan(
        tmp_path, monkeypatch):
    # THE FALSE PAGE: a rejected reboot request means the terminal never went
    # down, so it must never be classified as "wan2 did not return"
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    clk = FakeClock()
    d = FakeDish(reboot_ok=False)
    res = M.reboot_wan2(cfg, now=1000.0, client=d,
                        sleep=instant(clk), clock=clk)
    assert res.ok is False
    assert res.status is M.Outcome.NOT_ISSUED
    assert res.status is not M.Outcome.NOT_RETURNED


def test_reboot_wan2_skips_when_bootcount_is_unreadable(tmp_path, monkeypatch):
    # no readable receipt => no way to prove a reboot happened; fail safe by
    # skipping rather than rebooting blind and calling BFD-up a success
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    d = FakeDish(boot=None)
    res = M.reboot_wan2(cfg, now=1000.0, client=d)
    assert res.ok is False
    assert res.status is M.Outcome.SKIPPED
    assert d.calls == []


def test_reboot_wan2_applies_a_staged_update_instead_of_a_plain_reboot(
        tmp_path, monkeypatch):
    # a plain reboot DISCARDS a staged update, so the next night would find it
    # staged again, forever
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    clk = FakeClock()
    d = FakeDish(staged=True)
    res = M.reboot_wan2(cfg, now=1000.0, client=d,
                        sleep=instant(clk), clock=clk)
    assert res.ok is True
    assert d.calls == ["apply_update"]


def test_reboot_wan2_falls_back_to_exactly_one_plain_reboot(tmp_path, monkeypatch):
    # a staged update that does not apply falls back to ONE plain reboot, then
    # verifies again
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    clk = FakeClock()
    d = FakeDish(staged=True, apply_bumps=False)
    res = M.reboot_wan2(cfg, now=1000.0, client=d,
                        sleep=instant(clk), clock=clk)
    assert res.ok is True
    assert d.calls == ["apply_update", "reboot"]


def test_reboot_wan2_rechecks_the_peer_before_the_fallback_reboot(
        tmp_path, monkeypatch):
    # the fallback is a SECOND chance to take out the last standing WAN, and a
    # whole recovery deadline has passed since the first peer check: if wan1
    # died meanwhile, the fallback reboot must not be issued
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    peer = {"state": "UP"}
    monkeypatch.setattr(M, "read_wan_states",
                        lambda *a, **k: {"wan1": peer["state"], "wan2": "UP"})

    class DishWhoseUpdateKillsThePeer(FakeDish):
        def apply_update(self):
            out = super().apply_update()
            peer["state"] = "DOWN"     # wan1 dies on its own, mid-leg
            return out

    clk = FakeClock()
    d = DishWhoseUpdateKillsThePeer(staged=True, apply_bumps=False)
    res = M.reboot_wan2(cfg, now=1000.0, client=d,
                        sleep=instant(clk), clock=clk)
    assert res.ok is False
    assert res.status is M.Outcome.SKIPPED
    assert d.calls == ["apply_update"]   # the fallback reboot was NOT issued


def test_reboot_wan2_dropped_fallback_reboot_is_not_a_lost_wan(tmp_path, monkeypatch):
    # THE FALSE PAGE, fallback edition: neither the update nor the plain reboot
    # bumped the bootcount while BFD stayed UP throughout — the silently-dropped
    # reboot the receipt exists to catch. wan2 never left, so it must not page
    # "wan2 did not return". (This test previously pinned exactly that bug.)
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    clk = FakeClock()
    d = FakeDish(staged=True, apply_bumps=False, reboot_bumps=False)
    res = M.reboot_wan2(cfg, now=1000.0, client=d,
                        sleep=instant(clk), clock=clk)
    assert res.ok is False
    assert res.status is M.Outcome.NOT_ISSUED
    assert res.status is not M.Outcome.NOT_RETURNED
    assert d.calls == ["apply_update", "reboot"]   # exactly one fallback


def test_reboot_wan2_pages_when_a_bricked_terminal_refuses_the_fallback(
        tmp_path, monkeypatch):
    # THE MISSED PAGE: the staged update took the terminal down for good, and
    # the fallback request fails BECAUSE the device is dead. A failed request is
    # not evidence the link is fine — only BFD is — so this is an outage, not a
    # quiet "wan2 was not rebooted tonight".
    cfg = write_cfg(tmp_path, dry_run=False)
    link = FakeLink()
    monkeypatch.setattr(M, "read_wan_states", link)
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    clk = FakeClock()
    d = DishThatDropsTheLink(link, on="apply_update", staged=True,
                             apply_bumps=False, reboot_ok=False)
    res = M.reboot_wan2(cfg, now=1000.0, client=d,
                        sleep=instant(clk), clock=clk)
    assert res.ok is False
    assert res.status is M.Outcome.NOT_RETURNED
    assert d.calls == ["apply_update", "reboot"]   # exactly one fallback


def test_reboot_wan2_pages_when_the_terminal_never_comes_back(
        tmp_path, monkeypatch):
    # the plain-reboot outage: the terminal went down on command and stayed
    # down. This, and only this, is what pages the operator.
    cfg = write_cfg(tmp_path, dry_run=False)
    link = FakeLink()
    monkeypatch.setattr(M, "read_wan_states", link)
    clk = FakeClock()
    d = DishThatDropsTheLink(link, on="reboot")
    res = M.reboot_wan2(cfg, now=1000.0, client=d,
                        sleep=instant(clk), clock=clk)
    assert res.ok is False
    assert res.status is M.Outcome.NOT_RETURNED
    assert "did not return" in res.reason


def test_reboot_wan2_rejected_request_on_a_dead_terminal_is_an_outage(
        tmp_path, monkeypatch):
    # the terminal died on its own mid-run and then refused the reboot request.
    # The request result says "not issued"; the link says wan2 is off the air.
    # The link wins — a request failing because the device is dead must page.
    cfg = write_cfg(tmp_path, dry_run=False)
    link = FakeLink(wan2="DOWN")       # wan1 still UP, so the peer check passes
    monkeypatch.setattr(M, "read_wan_states", link)
    clk = FakeClock()
    d = FakeDish(reboot_ok=False)
    res = M.reboot_wan2(cfg, now=1000.0, client=d,
                        sleep=instant(clk), clock=clk)
    assert res.ok is False
    assert res.status is M.Outcome.NOT_RETURNED
    assert d.calls == ["reboot"]


def test_reboot_wan2_declined_fallback_on_a_down_link_still_pages(
        tmp_path, monkeypatch):
    # declining the second reboot while the peer is down is right; calling the
    # leg "skipped" (silent, rc 0) while wan2 is itself off the air is not
    cfg = write_cfg(tmp_path, dry_run=False)
    link = FakeLink()
    monkeypatch.setattr(M, "read_wan_states", link)
    sent = []
    monkeypatch.setattr(M, "notify", lambda c, t, p, m, actions=None: sent.append((t, p)))

    class DishWhoseUpdateKillsBoth(DishThatDropsTheLink):
        def apply_update(self):
            out = super().apply_update()
            link.state["wan1"] = "DOWN"    # the peer dies on its own, mid-leg
            return out

    clk = FakeClock()
    d = DishWhoseUpdateKillsBoth(link, on="apply_update", staged=True,
                                 apply_bumps=False)
    res = M.reboot_wan2(cfg, now=1000.0, client=d,
                        sleep=instant(clk), clock=clk)
    assert res.ok is False
    assert res.status is M.Outcome.NOT_RETURNED
    assert d.calls == ["apply_update"]     # the fallback reboot was NOT issued
    assert sent == []                      # ...and was never promised, either


class Unreachable(FakeDish):
    def status(self):
        return None


def test_reboot_wan2_skips_when_the_terminal_is_unreachable(tmp_path, monkeypatch):
    # a terminal we cannot reach while wan2 is verifiably UP was never
    # disturbed: a silent skip is honest here
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    d = Unreachable()
    res = M.reboot_wan2(cfg, now=1000.0, client=d)
    assert res.ok is False
    assert res.status is M.Outcome.SKIPPED
    assert d.calls == []


def test_reboot_wan2_unreachable_terminal_on_a_down_link_pages(
        tmp_path, monkeypatch):
    # THE MASKED OUTAGE: the terminal may be unreachable BECAUSE IT IS DOWN. A
    # skip is silent and exits 0, so classifying this by the request's result
    # would bury a real wan2 outage. Only the link decides.
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", lambda *a, **k: dict(WAN2_DOWN))
    d = Unreachable()
    res = M.reboot_wan2(cfg, now=1000.0, client=d)
    assert res.status is M.Outcome.NOT_RETURNED
    assert d.calls == []
    sent = []
    monkeypatch.setattr(M, "notify", lambda c, t, p, m, actions=None: sent.append((t, p)))
    M.report_leg(cfg, "wan2", res)
    assert any(p == "high" for _t, p in sent)


def test_reboot_wan2_unreadable_bootcount_on_a_down_link_pages(
        tmp_path, monkeypatch):
    # same masked outage via the other precheck: a bootcount we cannot read
    # because the terminal is dying must not be reported as a silent skip
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", lambda *a, **k: dict(WAN2_DOWN))
    d = FakeDish(boot=None)
    res = M.reboot_wan2(cfg, now=1000.0, client=d)
    assert res.status is M.Outcome.NOT_RETURNED
    assert d.calls == []


def test_reboot_wan2_rereads_status_immediately_before_rebooting(
        tmp_path, monkeypatch):
    # THE STALE SNAPSHOT: the in-flight/staged/bootcount decision used to come
    # from one status read taken several seconds and several round-trips before
    # the reboot was issued. A firmware update that starts APPLYING in that gap
    # must still be caught — interrupting a firmware write is how terminals get
    # bricked.
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)

    class UpdateStartsMidLeg(FakeDish):
        def status(self):
            self.calls.append("status")
            return self._st

        def update_in_flight(self, st=None):
            # idle on the first (precheck) snapshot; APPLYING by the time the
            # decision snapshot is taken
            return self.calls.count("status") > 1

    clk = FakeClock()
    d = UpdateStartsMidLeg()
    res = M.reboot_wan2(cfg, now=1000.0, client=d,
                        sleep=instant(clk), clock=clk)
    assert res.status is M.Outcome.SKIPPED
    assert "in flight" in res.reason
    assert "reboot" not in d.calls and "apply_update" not in d.calls


def test_reboot_wan2_takes_the_bootcount_baseline_from_the_final_snapshot(
        tmp_path, monkeypatch):
    # the baseline must be as fresh as the decision: a bootcount read early and
    # compared late would credit a reboot that happened in the gap (or that we
    # never caused) as OUR reboot's receipt
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    seen = []

    class D(FakeDish):
        def bootcount(self, st=None):
            seen.append(st)
            return self.boot

    clk = FakeClock()
    M.reboot_wan2(cfg, now=1000.0, client=D(), sleep=instant(clk), clock=clk)
    # the first call passes the decision snapshot; the await_up receipt polls
    # pass none, because they must be fresh reads
    assert seen[0] is not None
    assert seen[1:] and all(s is None for s in seen[1:])


def test_run_once_skips_when_peer_is_down(tmp_path, monkeypatch):
    # A fresh read in which a WAN is DOWN is a real OBSERVATION, not an absence:
    # skip it, but do NOT page — a WAN merely down at midnight is not an
    # operator emergency, and paging on it would cry wolf. (Contrast the
    # unreadable-state case below, which DOES page.)
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states",
                        lambda *a, **k: {"wan1": "UP", "wan2": "DOWN"})
    calls = []
    monkeypatch.setattr(M, "reboot_wan1", lambda *a, **k: calls.append("w1"))
    monkeypatch.setattr(M, "reboot_wan2", lambda *a, **k: calls.append("w2"))
    sent = []
    monkeypatch.setattr(M, "notify",
                        lambda cfg, t, p, m, actions=None: sent.append((t, p)))
    rc = M.run_once(cfg, now=1000.0)
    assert rc == 0                     # a skip is a success, not a failure
    assert calls == []                 # never disturb the last standing WAN
    assert sent == []                  # an observed-down WAN is not a page


def test_run_once_pages_when_the_state_file_is_unreadable(tmp_path, monkeypatch):
    # {} from read_wan_states means "unknown" — an ABSENT observation, not a WAN
    # we can see is down. It must never read as UP, and (the rule this whole
    # sequencer turns on) it must never be a SILENT skip: a state path that
    # quietly went missing is exactly how the nightly reboot no-op'd, unnoticed.
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", lambda *a, **k: {})
    calls = []
    monkeypatch.setattr(M, "reboot_wan1", lambda *a, **k: calls.append("w1"))
    sent = []
    monkeypatch.setattr(M, "notify",
                        lambda cfg, t, p, m, actions=None: sent.append((t, p)))
    rc = M.run_once(cfg, now=1000.0)
    assert rc == 0
    assert calls == []                          # never disturb the last WAN
    assert any(p == "high" for _t, p in sent)   # but it PAGES, not silent


def test_run_once_aborts_before_wan2_if_wan1_never_returns(tmp_path, monkeypatch):
    # The invariant: a failed leg 1 stops the run. It must NOT "carry on with
    # the other WAN" — that is exactly how both WANs end up down.
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "reboot_wan1", lambda *a, **k: M.LegResult(
        False, M.Outcome.NOT_RETURNED, "wan1 did not return within 600s"))
    called = []
    monkeypatch.setattr(M, "reboot_wan2", lambda *a, **k: called.append("w2"))
    sent = []
    monkeypatch.setattr(M, "notify", lambda cfg, t, p, m, actions=None: sent.append((t, p)))
    rc = M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert rc == 1
    assert called == []
    assert any(p == "high" for _t, p in sent)   # it pages


def test_run_once_continues_to_wan2_when_leg_one_was_never_issued(
        tmp_path, monkeypatch):
    # NOT_ISSUED means, by classify_by_link's own definition, that wan1 is
    # VERIFIABLY still UP and was never disturbed (the admin password rotated,
    # say). Aborting on it would mean wan2 never gets rebooted again either —
    # silently reinstating the exact problem this feature exists to solve, for
    # as long as wan1's reboot stays broken. Report it; carry on. Leg 2's own
    # peer re-check is what keeps the never-both-down invariant.
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "reboot_wan1", lambda *a, **k: M.LegResult(
        False, M.Outcome.NOT_ISSUED, "reboot failed: admin API rejected it"))
    called = []
    monkeypatch.setattr(M, "reboot_wan2",
                        lambda *a, **k: (called.append("w2"), recovered())[1])
    sent = []
    monkeypatch.setattr(M, "notify",
                        lambda cfg, t, p, m, actions=None: sent.append((t, p)))
    rc = M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert called == ["w2"]                      # leg 2 still ran
    # ...and the run is NOT a failure: exit 1 is reserved for an OUTAGE (a WAN
    # we took down that did not come back). wan1 never left, so marking the unit
    # `failed` in systemctl status would train the operator to ignore red.
    assert rc == 0
    # ...but it IS reported, at informational priority — never as "did not
    # return", which would be a lie about a WAN that never left.
    assert [p for _t, p in sent] == ["default"]
    assert "did not return" not in sent[0][0]


def test_run_once_aborts_before_wan2_when_leg_one_did_not_return(
        tmp_path, monkeypatch):
    # the invariant NOT_ISSUED must not weaken: a wan1 that went DOWN and
    # stayed down still stops the run dead, because rebooting wan2 now would
    # take the last standing WAN off the air
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "reboot_wan1", lambda *a, **k: M.LegResult(
        False, M.Outcome.NOT_RETURNED, "wan1 did not return within 600s"))
    called = []
    monkeypatch.setattr(M, "reboot_wan2", lambda *a, **k: called.append("w2"))
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    rc = M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert rc == 1
    assert called == []


def test_run_once_continues_to_wan2_after_a_skipped_leg_one(tmp_path, monkeypatch):
    # a SKIPPED leg 1 also proceeds — but NOT because a skip proves wan1 is up
    # (the watchdog's `no carrier` guard exits 2 precisely BECAUSE wan1 is
    # broken). Leg 2's own peer_is_up() re-check is the guard that matters.
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "reboot_wan1", lambda *a, **k: M.LegResult(
        False, M.Outcome.SKIPPED, "skipped by guard: no carrier"))
    called = []
    monkeypatch.setattr(M, "reboot_wan2",
                        lambda *a, **k: (called.append("w2"), recovered())[1])
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    rc = M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert called == ["w2"]
    assert rc == 0                     # a skip is a silent success


def test_run_once_does_not_page_high_when_the_request_was_rejected(
        tmp_path, monkeypatch):
    # THE FALSE PAGE: "reboot request failed" means the grpcurl POST was
    # rejected, so wan2 never went down. Paging "wan2 did not return from
    # maintenance reboot" at high would be alarming and untrue.
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "reboot_wan1", recovered)
    monkeypatch.setattr(M, "reboot_wan2", lambda *a, **k: M.LegResult(
        False, M.Outcome.NOT_ISSUED, "reboot request failed"))
    sent = []
    monkeypatch.setattr(M, "notify",
                        lambda cfg, t, p, m, actions=None: sent.append((t, p)))
    rc = M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert rc == 0        # link untouched => reported, but not a unit failure
    assert not any(p == "high" for _t, p in sent)
    assert not any("did not return" in t for t, _p in sent)


def test_run_once_reboots_wan2_only_after_wan1_recovers(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    order = []
    monkeypatch.setattr(M, "reboot_wan1",
                        lambda *a, **k: (order.append("w1"), recovered())[1])
    monkeypatch.setattr(M, "reboot_wan2",
                        lambda *a, **k: (order.append("w2"), recovered())[1])
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    rc = M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert rc == 0
    assert order == ["w1", "w2"]


def test_run_once_settles_after_each_leg(tmp_path, monkeypatch):
    # the settle is injectable so no test ever waits 30 real seconds for it,
    # and it separates the legs: wan2 is not touched until wan1 has settled
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    log_ = []
    monkeypatch.setattr(M, "reboot_wan1",
                        lambda *a, **k: (log_.append("w1"), recovered())[1])
    monkeypatch.setattr(M, "reboot_wan2",
                        lambda *a, **k: (log_.append("w2"), recovered())[1])
    M.run_once(cfg, now=1000.0, sleep=lambda s: log_.append(("sleep", s)))
    assert log_ == ["w1", ("sleep", cfg.settle_s),
                    "w2", ("sleep", cfg.settle_s)]


def test_run_once_keeps_the_window_open_across_the_settle(tmp_path, monkeypatch):
    # the settle exists BECAUSE the link is still settling: a WAN that has just
    # booted flaps UP/DOWN/UP as it finishes coming up, and a window closed at
    # the first UP would let exactly those transitions fire alerts
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    monkeypatch.setattr(M, "reboot_wan1", recovered)
    monkeypatch.setattr(M, "reboot_wan2", recovered)
    open_during_settle = []

    def watch(_s):
        w = Path(cfg.window_path)
        open_during_settle.append(
            _json.loads(w.read_text())["wan"] if w.exists() else None)

    rc = M.run_once(cfg, now=1000.0, sleep=watch)
    assert rc == 0
    assert open_during_settle == ["wan1", "wan2"]
    assert not Path(cfg.window_path).exists()   # and closed once settled


def test_run_once_opens_a_window_naming_the_wan_being_disturbed(
        tmp_path, monkeypatch):
    # the window is what keeps the alerts quiet; it must name the WAN in flight
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    seen = []

    def peek(*a, **k):
        seen.append(_json.loads(Path(cfg.window_path).read_text()))
        return recovered()
    monkeypatch.setattr(M, "reboot_wan1", peek)
    monkeypatch.setattr(M, "reboot_wan2", peek)
    M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert [w["wan"] for w in seen] == ["wan1", "wan2"]
    # the TTL is a backstop: suppression expires on its own if we are killed
    assert seen[0]["until"] > 1000.0


def test_run_once_always_closes_the_window(tmp_path, monkeypatch):
    # A window left open would suppress that WAN's alerts indefinitely — the
    # one failure mode that could hide a real outage.
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(M, "reboot_wan1", boom)
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    import pytest
    with pytest.raises(RuntimeError):
        M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert not Path(cfg.window_path).exists()


def test_run_once_closes_the_window_when_leg_two_explodes(tmp_path, monkeypatch):
    # same guarantee on the wan2 leg: the finally must cover both windows
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    monkeypatch.setattr(M, "reboot_wan1", recovered)

    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(M, "reboot_wan2", boom)
    import pytest
    with pytest.raises(RuntimeError):
        M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert not Path(cfg.window_path).exists()


def test_run_once_closes_the_window_on_a_sigterm(tmp_path, monkeypatch):
    # SIGTERM (systemd `stop`) raises SystemExit, which is a BaseException: the
    # finally must cover it too, or the window outlives us until its TTL
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)

    def terminated(*a, **k):
        raise SystemExit(143)
    monkeypatch.setattr(M, "reboot_wan1", terminated)
    import pytest
    with pytest.raises(SystemExit):
        M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert not Path(cfg.window_path).exists()


def test_sigterm_handler_raises_systemexit_rather_than_killing_us(tmp_path):
    # the default SIGTERM action kills the process outright, skipping the
    # finally that closes the window; the handler must unwind the stack instead
    import pytest
    import signal
    previous = signal.getsignal(signal.SIGTERM)
    try:
        M.install_signal_handlers()
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        with pytest.raises(SystemExit):
            handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_run_once_clears_a_window_left_behind_by_a_killed_run(
        tmp_path, monkeypatch):
    # a SIGKILLed run leaves its window file behind, and an early skip returns
    # before the try/finally that would close it — so the stale window would go
    # on suppressing that WAN's alerts, run after run, until its TTL expired
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states",
                        lambda *a, **k: {"wan1": "UP", "wan2": "DOWN"})
    M.open_window(cfg, "wan1", now=1000.0, ttl_s=630)
    rc = M.run_once(cfg, now=1000.0)
    assert rc == 0                                    # still an early skip
    assert not Path(cfg.window_path).exists()


def test_run_once_does_not_page_for_a_skipped_leg(tmp_path, monkeypatch):
    # a skipped leg is a success (exit 0): tonight's reboot is never worth
    # risking the link, and skips are logged, not alerted
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "reboot_wan1", lambda *a, **k: M.LegResult(
        False, M.Outcome.SKIPPED, "skipped by guard: no carrier"))
    monkeypatch.setattr(M, "reboot_wan2", lambda *a, **k: M.LegResult(
        False, M.Outcome.SKIPPED, "skipping: terminal unreachable"))
    sent = []
    monkeypatch.setattr(M, "notify", lambda cfg, t, p, m, actions=None: sent.append((t, p)))
    rc = M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert rc == 0
    assert sent == []


def test_run_once_pages_when_wan2_does_not_return(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "reboot_wan1", recovered)
    monkeypatch.setattr(M, "reboot_wan2", lambda *a, **k: M.LegResult(
        False, M.Outcome.NOT_RETURNED, "wan2 did not return within 600s"))
    sent = []
    monkeypatch.setattr(M, "notify", lambda cfg, t, p, m, actions=None: sent.append((t, p)))
    rc = M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert rc == 1
    assert any(p == "high" for _t, p in sent)


def drive_leg2(cfg, monkeypatch, link, dish_):
    """Run the real reboot_wan2 end-to-end through run_once, with leg 1 already
    recovered, and report (outcome, exit code, priorities the operator saw)."""
    monkeypatch.setattr(M, "read_wan_states", link)
    monkeypatch.setattr(M, "reboot_wan1", recovered)
    sent = []
    monkeypatch.setattr(M, "notify", lambda c, t, p, m, actions=None: sent.append((t, p)))
    clk = FakeClock()
    real = M.reboot_wan2
    seen = []

    def leg2(c, now, **k):
        res = real(c, now, client=dish_, sleep=instant(clk), clock=clk)
        seen.append(res)
        return res

    monkeypatch.setattr(M, "reboot_wan2", leg2)
    rc = M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    return seen[0].status, rc, [p for _t, p in sent]


def test_e2e_wan2_bricked_by_the_update_pages_high(tmp_path, monkeypatch):
    # scenario 1: update applied, terminal bricked, fallback request fails —
    # a real outage, and the request's failure must not hide it
    cfg = write_cfg(tmp_path, dry_run=False)
    link = FakeLink()
    status, rc, prio = drive_leg2(cfg, monkeypatch, link, DishThatDropsTheLink(
        link, on="apply_update", staged=True, apply_bumps=False,
        reboot_ok=False))
    assert (status, rc) == (M.Outcome.NOT_RETURNED, 1)
    assert "high" in prio


def test_e2e_wan2_rejected_request_on_a_live_link_does_not_page(
        tmp_path, monkeypatch):
    # scenario 2: the request was rejected and wan2 is verifiably still UP —
    # failed maintenance, not an outage: exit 1, informational, no page
    cfg = write_cfg(tmp_path, dry_run=False)
    link = FakeLink()
    status, rc, prio = drive_leg2(cfg, monkeypatch, link,
                                  FakeDish(reboot_ok=False))
    # rc 0: the link was never disturbed, so this is a report, not a unit
    # failure. Exit 1 is reserved for a WAN we took down that stayed down.
    assert (status, rc) == (M.Outcome.NOT_ISSUED, 0)
    assert prio == ["default"]


def test_e2e_wan2_dropped_reboot_does_not_page(tmp_path, monkeypatch):
    # scenario 3: BFD stayed UP and the bootcount never bumped — the reboot was
    # silently dropped. The link never left; paging "did not return" is a lie.
    cfg = write_cfg(tmp_path, dry_run=False)
    link = FakeLink()
    status, rc, prio = drive_leg2(cfg, monkeypatch, link,
                                  FakeDish(reboot_bumps=False))
    # rc 0: the link was never disturbed, so this is a report, not a unit
    # failure. Exit 1 is reserved for a WAN we took down that stayed down.
    assert (status, rc) == (M.Outcome.NOT_ISSUED, 0)
    assert prio == ["default"]


def test_e2e_wan2_down_and_never_back_pages_high(tmp_path, monkeypatch):
    # scenario 4: the terminal went down on command and stayed down
    cfg = write_cfg(tmp_path, dry_run=False)
    link = FakeLink()
    status, rc, prio = drive_leg2(cfg, monkeypatch, link,
                                  DishThatDropsTheLink(link, on="reboot"))
    assert (status, rc) == (M.Outcome.NOT_RETURNED, 1)
    assert "high" in prio


# == THE STRANDING BUG, re-entered through the NOT_ISSUED door =================
#
# hotspot_watchdog --scheduled-reboot used to exit 1 for FOUR different reasons.
# Three of them genuinely never touched the hotspot. The fourth — the restart
# POST got no usable response — is what a SUCCESSFUL reboot looks like from
# inside the process: the hotspot accepts the restart and tears the connection
# down as it goes. Its carrier then takes ~45s to actually drop.
#
# So reboot_wan1 classified off BFD a few seconds later, read the STALE
# pre-reboot UP, called it NOT_ISSUED — and NOT_ISSUED continues to leg 2. Leg
# 2's peer check re-read the same stale UP and rebooted the terminal:
#
#   t+0    watchdog POSTs the restart; the hotspot takes it; the answer is lost
#   t+8    classify_by_link reads wan1 "UP" (STALE)  -> NOT_ISSUED
#   t+9    leg 2's peer_is_up reads wan1 "UP" (STALE) -> REBOOTS THE TERMINAL
#   t+14   wan2 DOWN
#   t+45   wan1's carrier finally drops   *** BOTH WANS DOWN — stranded ***
#
# The watchdog now exits 3 for that case, and reboot_wan1 treats 3 exactly like
# 0: it goes and WATCHES the link. These tests hold that shut.


class Vehicle:
    """Both WANs on one clock, with the hotspot's CARRIER LAG modelled.

    The lag is the whole bug. A restart POSTed to the hotspot does not take
    wan1 down when the POST returns; the carrier drops `lag` polls later. Every
    BFD read in between is a stale pre-reboot UP, and believing one is what
    authorises the terminal's reboot into a wan1 that is already dying.

    Any moment at which BOTH WANs are down is recorded in `stranded`. That list
    must stay empty: it is the vehicle's connectivity."""

    def __init__(self, lag=3, wan1_down_polls=4, wan1_returns=True,
                 wan2_down_polls=2):
        self.wan1 = "UP"
        self.wan2 = "UP"
        self.polls = 0
        self.lag = lag
        self.wan1_down_polls = wan1_down_polls
        self.wan1_returns = wan1_returns
        self.wan2_down_polls = wan2_down_polls
        self.w1_drop_at = None
        self.w1_return_at = None
        self.w2_return_at = None
        self.stranded = []          # poll indices at which both WANs were down
        self.terminal_reboots = 0

    def hotspot_took_the_restart(self):
        """The hotspot ACCEPTED the restart (which is why the answer was lost).
        Its carrier holds up for `lag` more polls before it actually drops."""
        self.w1_drop_at = self.polls + self.lag
        if self.wan1_returns:
            self.w1_return_at = self.w1_drop_at + self.wan1_down_polls

    def terminal_rebooted(self):
        """The terminal, unlike the hotspot, drops the moment it is told to."""
        self.terminal_reboots += 1
        self.wan2 = "DOWN"
        self.w2_return_at = self.polls + self.wan2_down_polls
        self._check()

    def _check(self):
        if self.wan1 == "DOWN" and self.wan2 == "DOWN":
            self.stranded.append(self.polls)

    def read(self, *a, **k):
        """read_wan_states: one BFD poll."""
        self.polls += 1
        if self.w1_drop_at is not None and self.polls > self.w1_drop_at:
            self.wan1 = "DOWN"
        if self.w1_return_at is not None and self.polls > self.w1_return_at:
            self.wan1 = "UP"
        if self.w2_return_at is not None and self.polls > self.w2_return_at:
            self.wan2 = "UP"
        self._check()
        return {"wan1": self.wan1, "wan2": self.wan2}

    def peek_wan1(self):
        """wan1's TRUE state, without consuming a poll."""
        return self.wan1


class VehicleDish(FakeDish):
    """The terminal, wired into the Vehicle: its reboot really does take wan2
    off the air (and bumps the bootcount, which is its reboot receipt)."""

    def __init__(self, vehicle, **kw):
        super().__init__(**kw)
        self.vehicle = vehicle

    def reboot(self):
        out = super().reboot()
        if out:
            self.vehicle.terminal_rebooted()
        return out


class HotspotThatTakesTheRestart:
    """The watchdog one-shot, as a subprocess: it POSTs the restart, the hotspot
    takes it, and the response dies with the connection. Exit 3 — attempted,
    outcome unknown — NOT exit 1."""

    def __init__(self, vehicle, rc=M.WD_ATTEMPTED_UNKNOWN, takes_it=True):
        self.vehicle = vehicle
        self.rc = rc
        self.takes_it = takes_it
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if self.takes_it:
            self.vehicle.hotspot_took_the_restart()

        class P:
            returncode = self.rc
            stdout = "reboot POST was attempted but its outcome is UNKNOWN"
            stderr = ""
        return P()


def drive_the_night(cfg, monkeypatch, vehicle, watchdog, dish=None):
    """A whole maintenance run, end to end, through the REAL reboot_wan1 and the
    REAL reboot_wan2 — nothing about the leg logic is stubbed. Returns
    (rc, wan1_at_leg2_entry, notifications, leg statuses)."""
    monkeypatch.setattr(M, "read_wan_states", vehicle.read)
    dish = dish or FakeDish()
    sent, legs, at_leg2 = [], {}, []
    monkeypatch.setattr(M, "notify", lambda c, t, p, m, actions=None: sent.append((t, p, m)))
    clk = FakeClock()

    real1, real2 = M.reboot_wan1, M.reboot_wan2

    def leg1(c, now, **k):
        legs["wan1"] = res = real1(c, now, runner=watchdog,
                                   sleep=instant(clk), clock=clk)
        return res

    def leg2(c, now, **k):
        # what wan1 TRULY is at the instant leg 2 begins — the question the
        # whole invariant turns on
        at_leg2.append(vehicle.peek_wan1())
        legs["wan2"] = res = real2(c, now, client=dish,
                                   sleep=instant(clk), clock=clk)
        return res

    monkeypatch.setattr(M, "reboot_wan1", leg1)
    monkeypatch.setattr(M, "reboot_wan2", leg2)
    rc = M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    return rc, (at_leg2[0] if at_leg2 else None), sent, legs


def test_stranding_scenario_cannot_happen(tmp_path, monkeypatch):
    # THE SCENARIO ITSELF. The watchdog reports "attempted, unknown"; the
    # hotspot has in fact taken the restart, and wan1 reads a STALE UP for the
    # first several polls before its carrier drops. wan1 does come back.
    cfg = write_cfg(tmp_path, dry_run=False)
    v = Vehicle(lag=3, wan1_down_polls=4)
    wd = HotspotThatTakesTheRestart(v)
    dish = VehicleDish(v)              # a terminal reboot really drops wan2

    rc, wan1_at_leg2, sent, legs = drive_the_night(cfg, monkeypatch, v, wd,
                                                   dish=dish)

    # 1. The vehicle was NEVER off the air on both WANs. This is the invariant.
    assert v.stranded == []
    # 2. Leg 1 was not fooled by the stale UP: it watched wan1 go down and come
    #    back, which is the only thing that can prove the reboot landed.
    assert legs["wan1"].status is M.Outcome.RECOVERED
    # 3. Leg 2 was not entered until wan1 was TRULY back up.
    assert wan1_at_leg2 == "UP"
    assert v.terminal_reboots == 1        # ...and only then was it disturbed
    # 4. No false success and no false page: a night that worked is silent.
    assert rc == 0
    assert not any(p == "high" for _t, p, _m in sent)
    assert not any("not rebooted tonight" in t for t, _p, _m in sent)


def test_stranding_scenario_leg2_is_never_reached_while_wan1_is_down(
        tmp_path, monkeypatch):
    # The same reboot, but wan1 NEVER COMES BACK. The run must abort: NOT_RETURNED
    # still stops the night dead, and the terminal is never touched.
    cfg = write_cfg(tmp_path, dry_run=False)
    v = Vehicle(lag=3, wan1_returns=False)
    wd = HotspotThatTakesTheRestart(v)
    dish = FakeDish()

    rc, wan1_at_leg2, sent, legs = drive_the_night(cfg, monkeypatch, v, wd,
                                                   dish=dish)

    assert legs["wan1"].status is M.Outcome.NOT_RETURNED
    assert "wan2" not in legs                 # leg 2 was never reached...
    assert wan1_at_leg2 is None
    assert dish.calls == []                   # ...and the terminal never touched
    assert v.stranded == []
    assert rc == 1
    assert any(p == "high" for _t, p, _m in sent)     # and it pages


def test_attempted_unknown_that_really_never_landed_still_reaches_leg2(
        tmp_path, monkeypatch):
    # The flip side, and the reason exit 3 may not simply ABORT: an "attempted,
    # unknown" whose restart genuinely never landed leaves wan1 continuously UP
    # for the whole deadline. That IS a NOT_ISSUED — the link was never
    # disturbed — so the night must go on and reboot the terminal, or a hotspot
    # whose admin API is permanently confused would mean wan2 is never rebooted
    # again.
    cfg = write_cfg(tmp_path, dry_run=False, recovery_deadline_s=60)
    v = Vehicle()
    wd = HotspotThatTakesTheRestart(v, takes_it=False)   # wan1 never goes down
    dish = VehicleDish(v)

    rc, wan1_at_leg2, sent, legs = drive_the_night(cfg, monkeypatch, v, wd,
                                                   dish=dish)

    assert legs["wan1"].status is M.Outcome.NOT_ISSUED
    assert wan1_at_leg2 == "UP"
    assert legs["wan2"].status is M.Outcome.RECOVERED   # leg 2 DID run
    assert "reboot" in dish.calls
    assert v.stranded == []
    # wan1 never left, so this is neither a page nor a unit failure — just a
    # report. Exit 1 is reserved for a WAN we took down that did not come back.
    assert rc == 0
    assert not any(p == "high" for _t, p, _m in sent)


# -- reboot_wan1's handling of the new exit code, in isolation ----------------


def test_reboot_wan1_awaits_recovery_on_attempted_unknown(tmp_path, monkeypatch):
    # exit 3 must be handled EXACTLY like exit 0: enter await_up with
    # require_down_first=True. It must NOT classify immediately off a stale UP.
    cfg = write_cfg(tmp_path, dry_run=False)
    states = StateTimeline(BOTH_UP,               # the peer re-check
                           BOTH_UP, BOTH_UP,      # STALE pre-reboot UP
                           WAN1_DOWN, WAN1_DOWN,  # the carrier drops at last
                           BOTH_UP)               # and wan1 comes back
    monkeypatch.setattr(M, "read_wan_states", states)
    clk = FakeClock()
    res = M.reboot_wan1(cfg, now=1000.0,
                        runner=FakeRunner([(M.WD_ATTEMPTED_UNKNOWN, "unknown")]),
                        sleep=instant(clk), clock=clk)
    assert res.status is M.Outcome.RECOVERED
    assert res.ok is True
    # it waited for the DOWN rather than crediting the first (stale) UP
    assert clk.t - 1000.0 == 5 * M.POLL_S


def test_reboot_wan1_attempted_unknown_pages_when_wan1_never_returns(
        tmp_path, monkeypatch):
    # the reboot landed and the hotspot never came back: a real outage, and the
    # one leg-1 outcome that aborts the night
    cfg = write_cfg(tmp_path, dry_run=False)
    states = StateTimeline(BOTH_UP,                  # the peer re-check
                           BOTH_UP,                  # stale UP
                           WAN1_DOWN)                # down, and stays down
    monkeypatch.setattr(M, "read_wan_states", states)
    clk = FakeClock()
    res = M.reboot_wan1(cfg, now=1000.0,
                        runner=FakeRunner([(M.WD_ATTEMPTED_UNKNOWN, "unknown")]),
                        sleep=instant(clk), clock=clk)
    assert res.status is M.Outcome.NOT_RETURNED
    assert "did not return" in res.reason


def test_reboot_wan1_attempted_unknown_is_not_issued_if_wan1_never_drops(
        tmp_path, monkeypatch):
    # continuously UP for the whole deadline: the restart really did not land
    cfg = write_cfg(tmp_path, dry_run=False, recovery_deadline_s=30)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    clk = FakeClock()
    res = M.reboot_wan1(cfg, now=1000.0,
                        runner=FakeRunner([(M.WD_ATTEMPTED_UNKNOWN, "unknown")]),
                        sleep=instant(clk), clock=clk)
    assert res.status is M.Outcome.NOT_ISSUED
    assert clk.t - 1000.0 >= cfg.recovery_deadline_s   # it really did watch


def test_reboot_wan1_untouched_exit_does_not_await(tmp_path, monkeypatch):
    # exit 1 keeps its old meaning and its old speed: the watchdog never POSTed
    # anything, so there is no reboot in flight for BFD to be stale about, and
    # the link can be read straight away.
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    clk = FakeClock()
    res = M.reboot_wan1(cfg, now=1000.0,
                        runner=FakeRunner([(M.WD_UNTOUCHED, "login rejected")]),
                        sleep=instant(clk), clock=clk)
    assert res.status is M.Outcome.NOT_ISSUED
    assert clk.t == 1000.0                    # no await_up, no polls


def test_reboot_wan1_treats_an_unknown_exit_code_as_untouched(tmp_path,
                                                              monkeypatch):
    # a watchdog from the future exiting 7 must not be mistaken for a success:
    # anything not in (0, 3) is classified off the link, and 2 is the guard skip
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    res = M.reboot_wan1(cfg, now=1000.0, runner=FakeRunner([(7, "???")]))
    assert res.status is M.Outcome.NOT_ISSUED
    assert res.ok is False


def test_watchdog_exit_codes_match_the_watchdog(tmp_path):
    # the contract is on the wire between two processes; the two copies of it
    # must not drift apart
    import hotspot_watchdog as W
    assert (M.WD_ISSUED, M.WD_UNTOUCHED, M.WD_SKIPPED, M.WD_ATTEMPTED_UNKNOWN) \
        == (W.EXIT_ISSUED, W.EXIT_UNTOUCHED, W.EXIT_SKIPPED,
            W.EXIT_ATTEMPTED_UNKNOWN)


def test_run_once_only_wan1_skips_wan2(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path)
    # both WANs UP so the start-gate passes
    (tmp_path / "sbfd.json").write_text(_json.dumps(
        {"timestamp": 0, "sessions": {
            "s1": {"iface": "wan1", "state": "UP"},
            "s2": {"iface": "wan2", "state": "UP"}}}))
    calls = []
    monkeypatch.setattr(M, "reboot_wan1",
                        lambda c, n: (calls.append("wan1"), M._leg(M.Outcome.SKIPPED, "x"))[1])
    monkeypatch.setattr(M, "reboot_wan2",
                        lambda c, n: (calls.append("wan2"), M._leg(M.Outcome.SKIPPED, "x"))[1])
    rc = M.run_once(cfg, 0.0, sleep=lambda *_: None, legs=("wan1",))
    assert calls == ["wan1"]          # wan2 leg never ran
    assert rc == 0


def test_run_once_only_wan2_skips_wan1(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path)
    (tmp_path / "sbfd.json").write_text(_json.dumps(
        {"timestamp": 0, "sessions": {
            "s1": {"iface": "wan1", "state": "UP"},
            "s2": {"iface": "wan2", "state": "UP"}}}))
    calls = []
    monkeypatch.setattr(M, "reboot_wan1",
                        lambda c, n: (calls.append("wan1"), M._leg(M.Outcome.SKIPPED, "x"))[1])
    monkeypatch.setattr(M, "reboot_wan2",
                        lambda c, n: (calls.append("wan2"), M._leg(M.Outcome.SKIPPED, "x"))[1])
    rc = M.run_once(cfg, 0.0, sleep=lambda *_: None, legs=("wan2",))
    assert calls == ["wan2"]
    assert rc == 0


def test_run_once_only_wan1_refuses_when_the_peer_is_down(tmp_path, monkeypatch):
    # The safety property this feature exists to guarantee: a single-leg reboot
    # of wan1 must still refuse to disturb it while wan2 — the WAN that would be
    # left standing — is down. `required = set(legs) | {w1, w2}` includes wan2.
    cfg = write_cfg(tmp_path)
    (tmp_path / "sbfd.json").write_text(_json.dumps(
        {"timestamp": 0, "sessions": {
            "s1": {"iface": "wan1", "state": "UP"},
            "s2": {"iface": "wan2", "state": "DOWN"}}}))
    calls = []
    monkeypatch.setattr(M, "reboot_wan1",
                        lambda c, n: (calls.append("wan1"), M._leg(M.Outcome.SKIPPED, "x"))[1])
    monkeypatch.setattr(M, "reboot_wan2",
                        lambda c, n: (calls.append("wan2"), M._leg(M.Outcome.SKIPPED, "x"))[1])
    rc = M.run_once(cfg, 0.0, sleep=lambda *_: None, legs=("wan1",))
    assert calls == []                 # never disturb the last standing WAN
    assert rc == 0                     # a skip is a success, not a failure


def test_run_once_only_wan2_refuses_when_the_peer_is_down(tmp_path, monkeypatch):
    # Symmetric: a single-leg reboot of wan2 must refuse while wan1 is down.
    cfg = write_cfg(tmp_path)
    (tmp_path / "sbfd.json").write_text(_json.dumps(
        {"timestamp": 0, "sessions": {
            "s1": {"iface": "wan1", "state": "DOWN"},
            "s2": {"iface": "wan2", "state": "UP"}}}))
    calls = []
    monkeypatch.setattr(M, "reboot_wan1",
                        lambda c, n: (calls.append("wan1"), M._leg(M.Outcome.SKIPPED, "x"))[1])
    monkeypatch.setattr(M, "reboot_wan2",
                        lambda c, n: (calls.append("wan2"), M._leg(M.Outcome.SKIPPED, "x"))[1])
    rc = M.run_once(cfg, 0.0, sleep=lambda *_: None, legs=("wan2",))
    assert calls == []
    assert rc == 0


def test_main_maps_only_flag_to_legs(tmp_path, monkeypatch):
    # --only wanX must reach run_once as legs=(wanX,); no --only is the full
    # cycle legs=("wan1", "wan2"). Capture run_once's kwargs to prove the wiring.
    write_cfg(tmp_path, dry_run=False)
    seen = []
    monkeypatch.setattr(M, "run_once",
                        lambda *a, **k: (seen.append(k.get("legs")), 0)[1])

    assert run_main(tmp_path, monkeypatch, "--now", "--only", "wan1") == 0
    assert run_main(tmp_path, monkeypatch, "--now", "--only", "wan2") == 0
    assert run_main(tmp_path, monkeypatch, "--now") == 0
    assert seen == [("wan1",), ("wan2",), ("wan1", "wan2")]


def test_report_leg_button_targets_wan(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path, control_topic="ctl-x9",
                    ntfy_auth_path=str(tmp_path / "auth"))
    (tmp_path / "auth").write_text(
        'NTFY_USER=u\nNTFY_PASS=p\nNTFY_BASE=https://h.example\n')
    seen = {}
    monkeypatch.setattr(M, "notify",
        lambda c, t, p, m, actions=None: seen.update(title=t, actions=actions))
    M.report_leg(cfg, "wan1", M._leg(M.Outcome.NOT_ISSUED, "no reboot observed"))
    assert "was not rebooted" in seen["title"]
    assert "reboot-wan1" in seen["actions"]
    assert "https://h.example/ctl-x9" in seen["actions"]
    assert "Authorization=Basic " in seen["actions"]


def test_report_leg_no_button_without_control_topic(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path)  # control_topic defaults to ""
    seen = {}
    monkeypatch.setattr(M, "notify",
        lambda c, t, p, m, actions=None: seen.update(actions=actions))
    M.report_leg(cfg, "wan2", M._leg(M.Outcome.NOT_ISSUED, "x"))
    assert seen["actions"] is None


def test_report_leg_no_button_when_auth_unreadable(tmp_path, monkeypatch):
    # control_topic is configured, but the auth file does not exist — the
    # button must fail open (no button, no crash), not raise.
    cfg = write_cfg(tmp_path, control_topic="ctl-x9",
                    ntfy_auth_path=str(tmp_path / "missing-auth"))
    seen = {}
    monkeypatch.setattr(M, "notify",
        lambda c, t, p, m, actions=None: seen.update(actions=actions))
    M.report_leg(cfg, "wan1", M._leg(M.Outcome.NOT_ISSUED, "x"))
    assert seen["actions"] is None


def test_report_leg_no_button_when_auth_missing_a_key(tmp_path, monkeypatch):
    # control_topic is configured and the auth file EXISTS, but it lacks the
    # NTFY_PASS / NTFY_BASE keys — the KeyError branch must fail open (page
    # still sent, no button), exactly like a missing file.
    cfg = write_cfg(tmp_path, control_topic="ctl-x9",
                    ntfy_auth_path=str(tmp_path / "auth"))
    (tmp_path / "auth").write_text('NTFY_USER=u\n')  # no PASS, no BASE
    seen = {}
    monkeypatch.setattr(M, "notify",
        lambda c, t, p, m, actions=None: seen.update(fired=True, actions=actions))
    M.report_leg(cfg, "wan1", M._leg(M.Outcome.NOT_ISSUED, "x"))
    assert seen["fired"] is True        # the page is still sent
    assert seen["actions"] is None      # but with no button


def test_report_leg_no_button_when_base_has_delimiter(tmp_path, monkeypatch):
    # control_topic is configured and the auth file EXISTS with all keys, but
    # NTFY_BASE contains a comma (operator typo) — interpolating it verbatim
    # would corrupt the ntfy Actions header, so the button must fail open (page
    # still sent, no button), like the other auth-fail-open cases.
    cfg = write_cfg(tmp_path, control_topic="ctl-x9",
                    ntfy_auth_path=str(tmp_path / "auth"))
    (tmp_path / "auth").write_text(
        'NTFY_USER=u\nNTFY_PASS=p\nNTFY_BASE=https://h,evil\n')
    seen = {}
    monkeypatch.setattr(M, "notify",
        lambda c, t, p, m, actions=None: seen.update(fired=True, actions=actions))
    M.report_leg(cfg, "wan1", M._leg(M.Outcome.NOT_ISSUED, "x"))
    assert seen["fired"] is True        # the page is still sent
    assert seen["actions"] is None      # but with no button
