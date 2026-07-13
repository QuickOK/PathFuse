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


def test_uptime_s_none_on_non_finite_strings(tmp_path):
    # CONFIRMED bug: a NaN uptime silently bypassed the min-uptime "just
    # rebooted" guard, since nan < x and nan >= x are both False; this pins
    # that uptime_s() returns None (never nan) so the guard cannot be skipped
    for s in NON_FINITE_STRINGS:
        d, _ = dish(tmp_path, [(0, _status_with_non_finite(s))])
        assert d.uptime_s() is None


def test_uptime_s_none_on_non_finite_float_values(tmp_path):
    # same guard, but for the bareword-float form
    for v in NON_FINITE_FLOATS:
        d, _ = dish(tmp_path, [(0, _status_with_non_finite(v))])
        assert d.uptime_s() is None


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

    def __init__(self, boot=1321, uptime=90000.0, staged=False,
                 in_flight=False, st=None, reboot_ok=True, apply_ok=True,
                 reboot_bumps=True, apply_bumps=True):
        self.boot = boot
        self._uptime = uptime
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

    def uptime_s(self):
        return self._uptime

    def bootcount(self):
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


def both_up(*a, **k):
    return {"wan1": "UP", "wan2": "UP"}


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


def test_reboot_wan1_refuses_when_peer_is_down(tmp_path, monkeypatch):
    # mechanism (b): each leg re-checks its peer immediately before rebooting,
    # catching a peer that died on its own mid-run
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states",
                        lambda *a, **k: {"wan1": "UP", "wan2": "DOWN"})
    r = FakeRunner([(0, "")])
    issued, why = M.reboot_wan1(cfg, now=1000.0, runner=r)
    assert issued is False
    assert why.startswith("skipped")
    assert r.calls == []               # the watchdog was never even invoked


def test_reboot_wan1_invokes_the_watchdog_one_shot_and_awaits_recovery(
        tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    clk = FakeClock()
    r = FakeRunner([(0, "reboot issued")])
    issued, why = M.reboot_wan1(cfg, now=1000.0, runner=r,
                                sleep=instant(clk), clock=clk)
    assert issued is True
    argv = r.calls[0]
    assert argv[0] == "/opt/sbfd-ctl/hotspot_watchdog.py"
    assert "--scheduled-reboot" in argv


def test_reboot_wan1_guard_skip_is_not_a_failure(tmp_path, monkeypatch):
    # watchdog exit 2 = skipped by a guard; tonight's reboot is never worth
    # risking the link, so a skip is reported as a skip, not a failure
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    issued, why = M.reboot_wan1(cfg, now=1000.0,
                                runner=FakeRunner([(2, "peer is not UP")]))
    assert issued is False
    assert why.startswith("skipped")


def test_reboot_wan1_fails_when_wan1_never_returns(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states",
                        lambda *a, **k: {"wan1": "DOWN", "wan2": "UP"})
    clk = FakeClock()
    issued, why = M.reboot_wan1(cfg, now=1000.0,
                                runner=FakeRunner([(0, "reboot issued")]),
                                sleep=instant(clk), clock=clk)
    assert issued is False
    assert "did not return" in why


def test_reboot_wan1_survives_a_missing_watchdog_binary(tmp_path, monkeypatch):
    # an OSError from the watchdog invocation must be a failed leg, not a
    # traceback that skips the finally-close of the window
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    issued, why = M.reboot_wan1(cfg, now=1000.0,
                                runner=RaisingRunner(OSError("nope")))
    assert issued is False


def test_wan2_guard_skips_when_uptime_too_low(tmp_path):
    cfg = write_cfg(tmp_path, dry_run=False)

    class D:
        def status(self):
            return {"deviceState": {"uptimeS": 60}, "softwareUpdateState": "IDLE"}

        def update_in_flight(self, st=None):
            return False

        def uptime_s(self):
            return 60.0
    issued, why = M.reboot_wan2(cfg, now=1000.0, client=D())
    assert issued is False
    assert "uptime" in why


def test_reboot_wan2_skips_when_an_update_is_in_flight(tmp_path):
    # interrupting a firmware write is how terminals get bricked
    cfg = write_cfg(tmp_path, dry_run=False)
    d = FakeDish(in_flight=True)
    issued, why = M.reboot_wan2(cfg, now=1000.0, client=d)
    assert issued is False
    assert d.calls == []


def test_reboot_wan2_refuses_when_peer_is_down(tmp_path, monkeypatch):
    # mechanism (b) for leg 2: re-check wan1 immediately before issuing
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states",
                        lambda *a, **k: {"wan1": "DOWN", "wan2": "UP"})
    d = FakeDish()
    issued, why = M.reboot_wan2(cfg, now=1000.0, client=d)
    assert issued is False
    assert why.startswith("skipping")
    assert d.calls == []               # never disturb the last standing WAN


def test_reboot_wan2_recovers_when_bootcount_bumps(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    clk = FakeClock()
    d = FakeDish()
    issued, why = M.reboot_wan2(cfg, now=1000.0, client=d,
                                sleep=instant(clk), clock=clk)
    assert issued is True
    assert d.calls == ["reboot"]


def test_reboot_wan2_requires_a_bootcount_bump_not_just_bfd(tmp_path, monkeypatch):
    # BFD returning only proves the PATH recovered; a reboot request that was
    # silently dropped must not be reported as a successful reboot
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    clk = FakeClock()
    d = FakeDish(reboot_bumps=False)
    issued, why = M.reboot_wan2(cfg, now=1000.0, client=d,
                                sleep=instant(clk), clock=clk)
    assert issued is False
    assert d.calls == ["reboot"]


def test_reboot_wan2_skips_when_bootcount_is_unreadable(tmp_path, monkeypatch):
    # no readable receipt => no way to prove a reboot happened; fail safe by
    # skipping rather than rebooting blind and calling BFD-up a success
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    d = FakeDish(boot=None)
    issued, why = M.reboot_wan2(cfg, now=1000.0, client=d)
    assert issued is False
    assert why.startswith("skipping")
    assert d.calls == []


def test_reboot_wan2_applies_a_staged_update_instead_of_a_plain_reboot(
        tmp_path, monkeypatch):
    # a plain reboot DISCARDS a staged update, so the next night would find it
    # staged again, forever
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    clk = FakeClock()
    d = FakeDish(staged=True)
    issued, why = M.reboot_wan2(cfg, now=1000.0, client=d,
                                sleep=instant(clk), clock=clk)
    assert issued is True
    assert d.calls == ["apply_update"]


def test_reboot_wan2_falls_back_to_exactly_one_plain_reboot(tmp_path, monkeypatch):
    # a staged update that does not apply falls back to ONE plain reboot, then
    # verifies again
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    clk = FakeClock()
    d = FakeDish(staged=True, apply_bumps=False)
    issued, why = M.reboot_wan2(cfg, now=1000.0, client=d,
                                sleep=instant(clk), clock=clk)
    assert issued is True
    assert d.calls == ["apply_update", "reboot"]


def test_reboot_wan2_fails_after_a_failed_fallback_reboot(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    clk = FakeClock()
    d = FakeDish(staged=True, apply_bumps=False, reboot_bumps=False)
    issued, why = M.reboot_wan2(cfg, now=1000.0, client=d,
                                sleep=instant(clk), clock=clk)
    assert issued is False
    assert d.calls == ["apply_update", "reboot"]   # exactly one fallback


def test_reboot_wan2_skips_when_the_terminal_is_unreachable(tmp_path):
    cfg = write_cfg(tmp_path, dry_run=False)

    class Unreachable(FakeDish):
        def status(self):
            return None
    d = Unreachable()
    issued, why = M.reboot_wan2(cfg, now=1000.0, client=d)
    assert issued is False
    assert d.calls == []


def test_run_once_skips_when_peer_is_down(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states",
                        lambda *a, **k: {"wan1": "UP", "wan2": "DOWN"})
    calls = []
    monkeypatch.setattr(M, "reboot_wan1", lambda *a, **k: calls.append("w1"))
    monkeypatch.setattr(M, "reboot_wan2", lambda *a, **k: calls.append("w2"))
    rc = M.run_once(cfg, now=1000.0)
    assert rc == 0                     # a skip is a success, not a failure
    assert calls == []                 # never disturb the last standing WAN


def test_run_once_skips_when_the_state_file_is_unreadable(tmp_path, monkeypatch):
    # {} from read_wan_states means "unknown", which must never read as UP
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", lambda *a, **k: {})
    calls = []
    monkeypatch.setattr(M, "reboot_wan1", lambda *a, **k: calls.append("w1"))
    rc = M.run_once(cfg, now=1000.0)
    assert rc == 0
    assert calls == []


def test_run_once_aborts_before_wan2_if_wan1_never_returns(tmp_path, monkeypatch):
    # The invariant: a failed leg 1 stops the run. It must NOT "carry on with
    # the other WAN" — that is exactly how both WANs end up down.
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "reboot_wan1", lambda *a, **k: (False, "no return"))
    called = []
    monkeypatch.setattr(M, "reboot_wan2", lambda *a, **k: called.append("w2"))
    sent = []
    monkeypatch.setattr(M, "notify", lambda cfg, t, p, m: sent.append((t, p)))
    rc = M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert rc == 1
    assert called == []
    assert any(p == "high" for _t, p in sent)   # it pages


def test_run_once_reboots_wan2_only_after_wan1_recovers(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    order = []
    monkeypatch.setattr(M, "reboot_wan1",
                        lambda *a, **k: (order.append("w1"), (True, "ok"))[1])
    monkeypatch.setattr(M, "reboot_wan2",
                        lambda *a, **k: (order.append("w2"), (True, "ok"))[1])
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    rc = M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert rc == 0
    assert order == ["w1", "w2"]


def test_run_once_settles_between_the_legs(tmp_path, monkeypatch):
    # the settle is injectable so no test ever waits 30 real seconds for it
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    log_ = []
    monkeypatch.setattr(M, "reboot_wan1",
                        lambda *a, **k: (log_.append("w1"), (True, "ok"))[1])
    monkeypatch.setattr(M, "reboot_wan2",
                        lambda *a, **k: (log_.append("w2"), (True, "ok"))[1])
    M.run_once(cfg, now=1000.0, sleep=lambda s: log_.append(("sleep", s)))
    assert log_ == ["w1", ("sleep", cfg.settle_s), "w2"]


def test_run_once_opens_a_window_naming_the_wan_being_disturbed(
        tmp_path, monkeypatch):
    # the window is what keeps the alerts quiet; it must name the WAN in flight
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "notify", lambda *a, **k: None)
    seen = []

    def peek(*a, **k):
        seen.append(_json.loads(Path(cfg.window_path).read_text()))
        return True, "ok"
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
    monkeypatch.setattr(M, "reboot_wan1", lambda *a, **k: (True, "ok"))

    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(M, "reboot_wan2", boom)
    import pytest
    with pytest.raises(RuntimeError):
        M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert not Path(cfg.window_path).exists()


def test_run_once_does_not_page_for_a_skipped_leg(tmp_path, monkeypatch):
    # a skipped leg is a success (exit 0): tonight's reboot is never worth
    # risking the link, and skips are logged, not alerted
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "reboot_wan1",
                        lambda *a, **k: (False, "skipped by guard: no carrier"))
    monkeypatch.setattr(M, "reboot_wan2",
                        lambda *a, **k: (False, "skipping: terminal unreachable"))
    sent = []
    monkeypatch.setattr(M, "notify", lambda cfg, t, p, m: sent.append((t, p)))
    rc = M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert rc == 0
    assert sent == []


def test_run_once_pages_when_wan2_does_not_return(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path, dry_run=False)
    monkeypatch.setattr(M, "read_wan_states", both_up)
    monkeypatch.setattr(M, "reboot_wan1", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(M, "reboot_wan2",
                        lambda *a, **k: (False, "wan2 did not return within 600s"))
    sent = []
    monkeypatch.setattr(M, "notify", lambda cfg, t, p, m: sent.append((t, p)))
    rc = M.run_once(cfg, now=1000.0, sleep=lambda s: None)
    assert rc == 1
    assert any(p == "high" for _t, p in sent)
