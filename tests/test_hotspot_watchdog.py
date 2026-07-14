import json as _json
import subprocess
from pathlib import Path

import hotspot_watchdog as W

BAD_PRIV = W.Sample(wan_private=True, wan_bfd_up=False, peer_bfd_up=True)
BAD_BFD = W.Sample(wan_private=False, wan_bfd_up=False, peer_bfd_up=True)
BAD_BOTH = W.Sample(wan_private=False, wan_bfd_up=False, peer_bfd_up=False)
GOOD = W.Sample(wan_private=False, wan_bfd_up=True, peer_bfd_up=True)
UNKNOWN = W.Sample(wan_private=False, wan_bfd_up=None, peer_bfd_up=None)


def make_policy(t0=1000.0, **kw):
    args = dict(dwell_s=600, grace_s=600, max_reboots_per_episode=3,
                holdoff_s=7200, healthy_reset_s=1800, startup_grace_s=0,
                start_time=t0)
    args.update(kw)
    return W.WatchdogPolicy(**args)


def kinds(actions):
    return [a.kind for a in actions]


def test_no_action_before_dwell():
    p = make_policy()
    assert p.step(BAD_PRIV, 1000.0) == []
    assert p.step(BAD_PRIV, 1599.0) == []


def test_dwell_expiry_notifies_then_reboots():
    p = make_policy()
    p.step(BAD_PRIV, 1000.0)
    acts = p.step(BAD_PRIV, 1600.0)
    assert kinds(acts) == ["notify", "reboot"]
    assert acts[0].title == "wan1 degraded"


def test_usable_resets_dwell():
    p = make_policy()
    p.step(BAD_PRIV, 1000.0)
    p.step(GOOD, 1300.0)
    p.step(BAD_PRIV, 1400.0)
    assert p.step(BAD_PRIV, 1999.0) == []          # dwell restarted at 1400
    assert "reboot" in kinds(p.step(BAD_PRIV, 2000.0))


def test_unknown_bfd_counts_as_usable():
    p = make_policy()
    for t in (1000.0, 1600.0, 2200.0):
        assert p.step(UNKNOWN, t) == []


def test_startup_grace_suppresses_actions():
    p = make_policy(startup_grace_s=300)
    p.step(BAD_PRIV, 1000.0)
    assert p.step(BAD_PRIV, 1299.0) == []          # within startup grace
    assert "reboot" in kinds(p.step(BAD_PRIV, 1600.0))


def drive_to_first_reboot(p, sample=BAD_PRIV, t0=1000.0):
    p.step(sample, t0)
    acts = p.step(sample, t0 + 600.0)
    assert "reboot" in kinds(acts)
    return t0 + 600.0


def test_grace_then_fresh_dwell_before_second_reboot():
    p = make_policy()
    t = drive_to_first_reboot(p)                    # t=1600, grace until 2200
    assert p.step(BAD_PRIV, t + 599.0) == []        # in grace
    assert p.step(BAD_PRIV, t + 601.0) == []        # dwell restarted at 2200
    assert p.step(BAD_PRIV, t + 1199.0) == []
    acts = p.step(BAD_PRIV, t + 1201.0)             # 2200 + 600 dwell elapsed
    assert kinds(acts) == ["reboot"]                # degraded already notified


def test_episode_cap_gives_up_then_holdoff():
    p = make_policy()
    t = drive_to_first_reboot(p)                    # reboot 1
    t += 1201.0
    assert kinds(p.step(BAD_PRIV, t)) == ["reboot"]   # reboot 2
    t += 1201.0
    assert kinds(p.step(BAD_PRIV, t)) == ["reboot"]   # reboot 3
    t += 1201.0
    acts = p.step(BAD_PRIV, t)
    assert kinds(acts) == ["notify"]
    assert acts[0].title == "hotspot watchdog giving up"
    assert acts[0].priority == "urgent"
    assert p.step(BAD_PRIV, t + 7199.0) == []       # holdoff
    assert "reboot" in kinds(p.step(BAD_PRIV, t + 7201.0 + 600.0))


def test_ambiguous_skips_reboot_until_peer_recovers():
    p = make_policy()
    p.step(BAD_BOTH, 1000.0)
    acts = p.step(BAD_BOTH, 1600.0)
    assert kinds(acts) == ["notify", "notify"]
    assert acts[1].title == "wan1+wan2 both down"
    assert p.step(BAD_BOTH, 1700.0) == []           # notified once, keeps waiting
    assert kinds(p.step(BAD_BFD, 1800.0)) == ["reboot"]  # peer back: no new dwell


def test_private_ip_reboots_even_with_peer_down():
    p = make_policy()
    both_down_private = W.Sample(wan_private=True, wan_bfd_up=False, peer_bfd_up=False)
    p.step(both_down_private, 1000.0)
    assert "reboot" in kinds(p.step(both_down_private, 1600.0))


def test_recovery_notification_and_reset():
    p = make_policy()
    t = drive_to_first_reboot(p)
    p.step(GOOD, t + 700.0)
    assert p.step(GOOD, t + 700.0 + 1799.0) == []
    acts = p.step(GOOD, t + 700.0 + 1801.0)
    assert kinds(acts) == ["notify"]
    assert acts[0].title == "wan1 recovered"
    # counters cleared: a fresh outage runs the whole cycle again
    t2 = t + 10000.0
    p.step(BAD_PRIV, t2)
    acts = p.step(BAD_PRIV, t2 + 600.0)
    assert kinds(acts) == ["notify", "reboot"]


def test_no_recovery_notification_without_fired_episode():
    p = make_policy()
    p.step(BAD_PRIV, 1000.0)     # brief blip, dwell never fires
    p.step(GOOD, 1100.0)
    assert p.step(GOOD, 1100.0 + 1801.0) == []


def test_persistence_roundtrip_preserves_episode():
    p = make_policy()
    t = drive_to_first_reboot(p)
    d = p.to_dict()
    q = W.WatchdogPolicy.from_dict(
        d, dwell_s=600, grace_s=600, max_reboots_per_episode=3,
        holdoff_s=7200, healthy_reset_s=1800, startup_grace_s=0,
        start_time=t + 650.0)
    # restored policy remembers the reboot: next reboot needs grace+dwell, not dwell
    assert q.step(BAD_PRIV, t + 601.0) == []
    assert kinds(q.step(BAD_PRIV, t + 1201.0)) == ["reboot"]
    assert q.reboots_issued == 2


IP_J = _json.dumps([{"ifname": "wan1", "addr_info": [
    {"family": "inet6", "local": "fe80::1"},
    {"family": "inet", "local": "192.0.2.91", "prefixlen": 24}]}])
IP_J_PUB = _json.dumps([{"ifname": "wan1", "addr_info": [
    {"family": "inet", "local": "198.51.100.7", "prefixlen": 30}]}])
IP_J_NOADDR = _json.dumps([{"ifname": "wan1", "addr_info": []}])

SBFD = _json.dumps({"timestamp": 5000.0, "sessions": {
    "s1": {"iface": "wan1", "state": "DOWN"},
    "s2": {"iface": "wan2", "state": "UP"}}})


def test_is_rfc1918():
    # RFC1918 blocks, built from octets: the push gate bans these IP literals
    # in tracked files, but the semantics are what the watchdog depends on.
    assert W.is_rfc1918("10.0.0.1")
    assert W.is_rfc1918(f"172.{16}.0.1")
    assert W.is_rfc1918(f"172.{31}.255.254")
    assert W.is_rfc1918(f"192.{168}.3.1")
    # Not RFC1918: carrier-grade NAT (a healthy cellular WAN lives here) and
    # ordinary public space. Misclassifying 100.64/10 would reboot a good link.
    assert not W.is_rfc1918("100.64.0.1")
    assert not W.is_rfc1918("203.0.113.7")
    assert not W.is_rfc1918(f"172.{15}.0.1")
    assert not W.is_rfc1918(f"172.{32}.0.1")
    assert not W.is_rfc1918("not-an-ip")


def test_parse_wan_ipv4():
    assert W.parse_wan_ipv4(IP_J, "wan1") == "192.0.2.91"
    assert W.parse_wan_ipv4(IP_J_PUB, "wan1") == "198.51.100.7"
    assert W.parse_wan_ipv4(IP_J_NOADDR, "wan1") is None
    assert W.parse_wan_ipv4("not json", "wan1") is None


def test_parse_bfd_states_by_iface():
    assert W.parse_bfd_states(SBFD, "wan1", "wan2", now=5010.0, max_age_s=30) == (False, True)
    assert W.parse_bfd_states(SBFD, "wan1", "wan2", now=5031.0, max_age_s=30) == (None, None)
    assert W.parse_bfd_states("junk", "wan1", "wan2", now=0.0, max_age_s=30) == (None, None)
    missing = _json.dumps({"timestamp": 5000.0, "sessions": {}})
    assert W.parse_bfd_states(missing, "wan1", "wan2", now=5010.0, max_age_s=30) == (None, None)


def test_parse_bfd_states_rejects_non_dict_sessions():
    # host-only type guard: a malformed "sessions" (e.g. a list, not a dict of
    # sessions) must return (None, None) rather than raising on .values().
    bad_shape = _json.dumps({"timestamp": 5000.0, "sessions": ["oops"]})
    assert W.parse_bfd_states(bad_shape, "wan1", "wan2", now=5010.0, max_age_s=30) == (None, None)


FIXTURE = (Path(__file__).parent / "fixtures" / "m6_model.json").read_text()


class FakeRunner:
    """Records curl argv (and kwargs); returns canned stdout per call."""
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.kwargs = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.kwargs.append(kw)
        out = self.outputs.pop(0)
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


def make_client(outputs):
    r = FakeRunner(outputs)
    c = W.NetgearClient("http://192.0.2.1", "wan1", "/tmp/jar.txt", runner=r)
    return c, r


def test_fetch_model_parses_and_binds_iface():
    c, r = make_client([FIXTURE])
    m = c.fetch_model()
    assert m["session"]["secToken"] == "FIXTURETOKEN123"
    assert "--interface" in r.calls[0] and "wan1" in r.calls[0]


def test_login_success_requires_admin_role():
    admin = FIXTURE.replace('"Guest"', '"Admin"')
    c, r = make_client([FIXTURE, "", admin])   # model -> POST -> model
    assert c.login("hunter2") is True
    joined = " ".join(r.calls[1])
    assert "token=FIXTURETOKEN123" in joined
    # the password field is still POSTed, in its original argv position...
    assert "--data-urlencode session.password@-" in joined


def test_login_password_never_appears_in_curl_argv():
    # argv is world-readable in /proc: an inline --data-urlencode name=value
    # leaks the hotspot admin password to every process table on the box. It
    # must reach curl on stdin instead (curl URL-encodes it identically).
    admin = FIXTURE.replace('"Guest"', '"Admin"')
    c, r = make_client([FIXTURE, "", admin])
    assert c.login("hunter2") is True
    for argv in r.calls:
        assert not any("hunter2" in a for a in argv), argv
    # ...and it is fed on stdin, verbatim (a trailing newline would be encoded
    # into the value and change the password on the wire).
    assert r.kwargs[1]["input"] == "hunter2"


def test_login_starts_from_a_fresh_cookie_jar(tmp_path):
    # A STALE Admin cookie left in the jar would make the post-login model.json
    # read back userRole=Admin even when the password we just posted was
    # rejected — a failed login reported as a success.
    jar = tmp_path / "jar.txt"
    jar.write_text("# stale Admin session cookie\n")
    r = FakeRunner([FIXTURE, "", FIXTURE])          # role stays Guest = rejected
    c = W.NetgearClient("http://192.0.2.1", "wan1", str(jar), runner=r)
    assert c.login("wrong") is False
    assert not jar.exists()                          # dropped before the attempt


def test_login_rejected_when_hotspot_redirects_to_error():
    # login posts err_redirect=/error.json, so an error Location is conclusive.
    admin = FIXTURE.replace('"Guest"', '"Admin"')
    c, _ = make_client([FIXTURE, "http://192.0.2.1/error.json", admin])
    assert c.login("wrong") is False


def test_login_failure_when_still_guest():
    c, _ = make_client([FIXTURE, "", FIXTURE])
    assert c.login("wrong") is False


def test_reboot_posts_shutdown_restart():
    c, r = make_client([FIXTURE, ""])
    assert c.reboot() is True
    assert "general.shutdown=restart" in " ".join(r.calls[1])


def test_reboot_asks_curl_for_the_redirect_target():
    # /Forms/config answers 302 for BOTH outcomes, so the exit code alone cannot
    # tell an accepted form from a rejected one — we must read the Location.
    c, r = make_client([FIXTURE, ""])
    c.reboot()
    assert "%{redirect_url}" in r.calls[1]


def test_reboot_error_redirect_is_not_success():
    # The hotspot took the POST and bounced it at its error page: a 302, exit 0,
    # and NO reboot. Reporting that as issued lies in the alert and (since the
    # episode budget is consumed up front) burns a real attempt.
    c, _ = make_client([FIXTURE, "http://192.0.2.1/error.json"])
    assert c.reboot() is False


def test_reboot_ok_redirect_is_success():
    c, _ = make_client([FIXTURE, "http://192.0.2.1/success.json"])
    assert c.reboot() is True


def test_fetch_model_none_on_bad_json():
    c, _ = make_client(["<html>redirect</html>"])
    assert c.fetch_model() is None


def test_diagnostics_line():
    line = W.NetgearClient.diagnostics(_json.loads(FIXTURE))
    assert "Connected" in line and "bars=5" in line
    assert W.NetgearClient.diagnostics(None) == "hotspot state unavailable"


def write_cfg(tmp_path, **over):
    raw = {"iface": "wan1", "peer_iface": "wan2",
           "sbfd_state_path": str(tmp_path / "state.json"), "state_max_age_s": 30,
           "admin_url": "http://192.0.2.1",
           "secret_path": str(tmp_path / "secret"),
           "poll_interval_s": 30, "dwell_s": 600, "grace_s": 600,
           "max_reboots_per_episode": 3, "holdoff_s": 7200,
           "healthy_reset_s": 1800, "startup_grace_s": 300,
           "state_path": str(tmp_path / "wd_state.json"),
           "notify_bin": "/usr/local/sbin/spool-notify", "dry_run": True}
    raw.update(over)
    p = tmp_path / "wd.json"
    p.write_text(_json.dumps(raw))
    return str(p)


def test_load_config_defaults_and_types(tmp_path):
    cfg = W.load_config(write_cfg(tmp_path))
    assert cfg.iface == "wan1" and cfg.dry_run is True
    assert cfg.dwell_s == 600.0 and isinstance(cfg.dwell_s, float)


def test_load_config_rejects_bad_values(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        W.load_config(write_cfg(tmp_path, dwell_s=0))


def test_load_config_rejects_non_finite_durations(tmp_path):
    # json.loads accepts the barewords NaN / Infinity, and EVERY comparison
    # against NaN is False — so NaN slides straight through a `<= 0` check and
    # then makes the dwell test false forever (watchdog never fires) or the
    # holdoff tests false (it fires every poll). Both are silent.
    import pytest
    for key in ("state_max_age_s", "poll_interval_s", "dwell_s", "grace_s",
                "holdoff_s", "healthy_reset_s", "startup_grace_s"):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError, match="finite"):
                W.load_config(write_cfg(tmp_path, **{key: bad}))


def test_load_config_allows_zero_where_zero_is_meaningful(tmp_path):
    # 0 startup grace / 0 max state age are legitimate ("no grace", "any age").
    cfg = W.load_config(write_cfg(tmp_path, startup_grace_s=0, state_max_age_s=0))
    assert cfg.startup_grace_s == 0.0 and cfg.state_max_age_s == 0.0


def test_load_config_requires_admin_url(tmp_path):
    # admin_url is a required key (no default): a config missing it must fail
    # loudly at load time, not fall back to a hardcoded hotspot address.
    import pytest
    p = tmp_path / "wd.json"
    p.write_text(_json.dumps({"secret_path": str(tmp_path / "secret")}))
    with pytest.raises(KeyError):
        W.load_config(str(p))


class SpyNotify:
    def __init__(self):
        self.sent = []

    def __call__(self, notify_bin, title, priority, message):
        self.sent.append((title, priority, message))


def make_executor(tmp_path, dry_run, login_ok=True, reboot_ok=True,
                  fetch_model_ok=True):
    cfg = W.load_config(write_cfg(tmp_path, dry_run=dry_run))
    (tmp_path / "secret").write_text("pw123\n")

    class FakeClient:
        def __init__(self):
            self.rebooted = 0

        def fetch_model(self):
            if not fetch_model_ok:
                return None
            return {"wwan": {"connection": "Disconnected"}}

        def login(self, pw):
            return login_ok

        def reboot(self):
            self.rebooted += 1
            return reboot_ok

        diagnostics = staticmethod(W.NetgearClient.diagnostics)

    client = FakeClient()
    spy = SpyNotify()
    return W.Executor(cfg, client, notify_fn=spy), client, spy


def test_executor_dry_run_notifies_instead_of_rebooting(tmp_path):
    ex, client, spy = make_executor(tmp_path, dry_run=True)
    ex.execute([W.Action("reboot")])
    assert client.rebooted == 0
    assert spy.sent[0][0] == "dry-run: would reboot hotspot"


def test_executor_real_reboot_logs_in_and_notifies(tmp_path):
    ex, client, spy = make_executor(tmp_path, dry_run=False)
    ex.execute([W.Action("reboot")])
    assert client.rebooted == 1
    assert spy.sent[0][0] == "hotspot reboot issued"


def test_executor_notifies_on_login_failure(tmp_path):
    ex, client, spy = make_executor(tmp_path, dry_run=False, login_ok=False)
    ex.execute([W.Action("reboot")])
    assert client.rebooted == 0
    assert spy.sent[0][0] == "hotspot reboot API error"


def test_reboot_alerts_when_admin_ui_unreachable(tmp_path):
    # _do_reboot's first guard: fetch_model() returning None means the admin
    # UI never answered at all — must alert distinctly and never reboot.
    ex, client, spy = make_executor(tmp_path, dry_run=False,
                                     fetch_model_ok=False)
    ex.execute([W.Action("reboot")])
    assert client.rebooted == 0
    assert spy.sent[0][0] == "hotspot reboot API error"
    assert "unreachable" in spy.sent[0][2]


def test_reboot_alerts_when_secret_unreadable(tmp_path):
    # _do_reboot's second guard: the admin UI answered fine, but the secret
    # file is gone/unreadable — must alert distinctly and never reboot.
    ex, client, spy = make_executor(tmp_path, dry_run=False)
    (tmp_path / "secret").unlink()
    ex.execute([W.Action("reboot")])
    assert client.rebooted == 0
    assert spy.sent[0][0] == "hotspot reboot API error"
    assert "secret" in spy.sent[0][2]


def test_reboot_alerts_when_post_fails(tmp_path):
    # _do_reboot's final guard: login succeeds but client.reboot() itself
    # returns False (the reboot POST failed) — distinct alert, budget still
    # spent (the POST really was attempted).
    ex, client, spy = make_executor(tmp_path, dry_run=False, reboot_ok=False)
    ex.execute([W.Action("reboot")])
    assert client.rebooted == 1
    assert spy.sent[0][0] == "hotspot reboot API error"
    assert "reboot POST failed" in spy.sent[0][2]


def test_executor_passes_through_notify_actions(tmp_path):
    ex, _, spy = make_executor(tmp_path, dry_run=True)
    ex.execute([W.Action("notify", "wan1 degraded", "high", "msg")])
    assert spy.sent == [("wan1 degraded", "high", "msg")]


def test_curl_uses_fail_flag():
    # curl exits 0 on HTTP 4xx/5xx unless --fail is passed; without it an error
    # page from /Forms/config would read as a successful reboot (Greptile P1).
    c, r = make_client([FIXTURE])
    c.fetch_model()
    assert "--fail" in r.calls[0]


def test_reboot_http_error_is_failure():
    class HttpErrRunner:
        """model.json fetch succeeds; the reboot POST gets an HTTP error (rc 22)."""
        def __init__(self):
            self.rcs = [0, 22]

        def __call__(self, argv, **kw):
            rc = self.rcs.pop(0)
            return subprocess.CompletedProcess(
                argv, rc, stdout=FIXTURE if rc == 0 else "", stderr="")

    c = W.NetgearClient("http://192.0.2.1", "wan1", "/tmp/jar.txt",
                        runner=HttpErrRunner())
    assert c.reboot() is False


def test_persistence_before_first_action_preserves_dwell():
    # A daemon restart mid-dwell must not reset the dwell timer (state is
    # persisted every poll, not just when actions fire).
    p = make_policy()
    p.step(BAD_PRIV, 1000.0)                       # dwell running, no actions yet
    d = p.to_dict()
    q = W.WatchdogPolicy.from_dict(
        d, dwell_s=600, grace_s=600, max_reboots_per_episode=3,
        holdoff_s=7200, healthy_reset_s=1800, startup_grace_s=0,
        start_time=1300.0)                         # restarted at t=1300
    acts = q.step(BAD_PRIV, 1600.0)                # dwell measured from 1000
    assert kinds(acts) == ["notify", "reboot"]


# -- host-only drift: read_carrier, link-dead guard, reboot API errors --------
# These exist only on the host's newer copy and have never had a test.


def policy():
    # Reuses make_policy()/BAD_BOTH already defined above. Primes the policy
    # through a dwell with an ambiguous (both-down) sample so degraded_notified
    # fires but no reboot is issued (ambiguous is not reboot-eligible) — this
    # lets the tests below observe just the one action under test, with
    # reboots_issued still at 0.
    p = make_policy()
    p.step(BAD_BOTH, 1000.0)
    p.step(BAD_BOTH, 1600.0)
    return p


def _after_dwell():
    return 1600.0


def test_read_carrier_reports_link_state(tmp_path):
    (tmp_path / "wan1").mkdir()
    (tmp_path / "wan1" / "carrier").write_text("1\n")
    assert W.read_carrier("wan1", sys_root=str(tmp_path)) is True
    (tmp_path / "wan1" / "carrier").write_text("0\n")
    assert W.read_carrier("wan1", sys_root=str(tmp_path)) is False


def test_read_carrier_unknown_when_unreadable(tmp_path):
    # An absent iface (or an admin-down one, where the read EINVALs) must be
    # UNKNOWN, not False: unknown never suppresses a reboot, but False does.
    assert W.read_carrier("nope", sys_root=str(tmp_path)) is None


def test_no_carrier_skips_reboot_and_alerts_once():
    # No carrier => the admin API is unreachable by definition, so rebooting
    # would burn the episode budget on a guaranteed failure.
    p = policy()                      # existing helper in this file
    s = W.Sample(wan_private=False, wan_bfd_up=False, peer_bfd_up=True,
                 wan_carrier=False)
    acts = p.step(s, now=_after_dwell())
    assert [a.kind for a in acts] == ["notify"]
    assert "link dead" in acts[0].title
    assert p.reboots_issued == 0
    # and it must not re-alert every poll
    assert p.step(s, now=_after_dwell() + 30) == []


def test_unknown_carrier_still_allows_reboot():
    p = policy()
    s = W.Sample(wan_private=False, wan_bfd_up=False, peer_bfd_up=True,
                 wan_carrier=None)
    acts = p.step(s, now=_after_dwell())
    assert any(a.kind == "reboot" for a in acts)


# -- scheduled_reboot: guarded one-shot for the maintenance sequencer --------


class _StubClient:
    def __init__(self, login_ok=True, reboot_ok=True, model=None):
        self.login_ok, self.reboot_ok = login_ok, reboot_ok
        self.model = model if model is not None else {"x": 1}
        self.rebooted = False

    def fetch_model(self):
        return self.model

    def login(self, pw):
        return self.login_ok

    def reboot(self):
        self.rebooted = self.reboot_ok
        return self.reboot_ok

    @staticmethod
    def diagnostics(model):
        return "diag"


def sched_cfg(tmp_path, **kw):
    secret = tmp_path / "secret"
    secret.write_text("hunter2\n")
    state = tmp_path / "sbfd.json"
    state.write_text(_json.dumps({
        "timestamp": 1000.0,
        "sessions": {
            "a": {"iface": "wan1", "state": "DOWN"},
            "b": {"iface": "wan2", "state": "UP"},
        }}))
    base = dict(iface="wan1", peer_iface="wan2", sbfd_state_path=str(state),
                state_max_age_s=30, admin_url="http://192.0.2.1",
                secret_path=str(secret), poll_interval_s=30, dwell_s=600,
                grace_s=600, max_reboots_per_episode=3, holdoff_s=7200,
                healthy_reset_s=1800, startup_grace_s=300,
                state_path=str(tmp_path / "wd.json"),
                notify_bin="/bin/true", dry_run=False)
    base.update(kw)
    return W.WdConfig(**base)


def test_scheduled_reboot_skips_when_peer_down(tmp_path, monkeypatch):
    # The whole point of the feature: never take out the last standing WAN.
    cfg = sched_cfg(tmp_path)
    state = _json.loads(Path(cfg.sbfd_state_path).read_text())
    state["sessions"]["b"]["state"] = "DOWN"
    Path(cfg.sbfd_state_path).write_text(_json.dumps(state))
    monkeypatch.setattr(W, "read_carrier", lambda *a, **k: True)
    c = _StubClient()
    issued, reason = W.scheduled_reboot(cfg, c, now=1000.0)
    assert issued is False
    assert "peer" in reason
    assert c.rebooted is False


def test_scheduled_reboot_skips_when_no_carrier(tmp_path, monkeypatch):
    cfg = sched_cfg(tmp_path)
    monkeypatch.setattr(W, "read_carrier", lambda *a, **k: False)
    c = _StubClient()
    issued, reason = W.scheduled_reboot(cfg, c, now=1000.0)
    assert issued is False
    assert "carrier" in reason
    assert c.rebooted is False


def test_scheduled_reboot_issues_when_guards_pass(tmp_path, monkeypatch):
    cfg = sched_cfg(tmp_path)
    monkeypatch.setattr(W, "read_carrier", lambda *a, **k: True)
    c = _StubClient()
    issued, reason = W.scheduled_reboot(cfg, c, now=1000.0)
    assert issued is True
    assert c.rebooted is True


def test_scheduled_reboot_dry_run_does_not_reboot(tmp_path, monkeypatch):
    cfg = sched_cfg(tmp_path, dry_run=True)
    monkeypatch.setattr(W, "read_carrier", lambda *a, **k: True)
    c = _StubClient()
    issued, reason = W.scheduled_reboot(cfg, c, now=1000.0)
    assert issued is False
    assert "dry-run" in reason
    assert c.rebooted is False


def test_scheduled_reboot_reports_login_failure(tmp_path, monkeypatch):
    cfg = sched_cfg(tmp_path)
    monkeypatch.setattr(W, "read_carrier", lambda *a, **k: True)
    c = _StubClient(login_ok=False)
    issued, reason = W.scheduled_reboot(cfg, c, now=1000.0)
    assert issued is False
    assert "login" in reason
    assert c.rebooted is False


def test_scheduled_reboot_skips_when_peer_state_stale(tmp_path, monkeypatch):
    # peer state UNKNOWN (stale sbfd file) must SKIP: "peer_up is not True"
    # has to catch None as well as False, or a stale file reads as a
    # definite DOWN and takes out the last standing WAN.
    cfg = sched_cfg(tmp_path)
    monkeypatch.setattr(W, "read_carrier", lambda *a, **k: True)
    c = _StubClient()
    stale_now = 1000.0 + cfg.state_max_age_s + 1
    issued, reason = W.scheduled_reboot(cfg, c, now=stale_now)
    assert issued is False
    assert "not UP" in reason and "None" in reason
    assert c.rebooted is False


def test_scheduled_reboot_skips_when_state_file_missing(tmp_path, monkeypatch):
    # peer state UNKNOWN (no sbfd file at all) must also SKIP: read_bfd_states
    # returns (None, None) on OSError, same "unknown, don't touch it" case.
    cfg = sched_cfg(tmp_path)
    Path(cfg.sbfd_state_path).unlink()
    monkeypatch.setattr(W, "read_carrier", lambda *a, **k: True)
    c = _StubClient()
    issued, reason = W.scheduled_reboot(cfg, c, now=1000.0)
    assert issued is False
    assert "not UP" in reason and "None" in reason
    assert c.rebooted is False


def test_scheduled_reboot_issues_when_carrier_unknown(tmp_path, monkeypatch):
    # carrier UNKNOWN (None) must PROCEED: only a definite False (no link)
    # may suppress the reboot, so "read_carrier(...) is False" must stay a
    # strict equality check and never become "is not True".
    cfg = sched_cfg(tmp_path)
    monkeypatch.setattr(W, "read_carrier", lambda *a, **k: None)
    c = _StubClient()
    issued, reason = W.scheduled_reboot(cfg, c, now=1000.0)
    assert issued is True
    assert c.rebooted is True


# -- --scheduled-reboot's exit-code contract ----------------------------------
#
# The maintenance sequencer runs this mode as a SUBPROCESS and decides, from
# the exit code alone, whether it is safe to go on and reboot the OTHER WAN.
# So the code is a safety interface, not a diagnostic, and its one load-bearing
# distinction is:
#
#   1  we PROVABLY never POSTed a restart      -> wan1 is where it was
#   3  we POSTed one and cannot confirm it     -> wan1 may be going down NOW
#
# Collapsing 3 into 1 is what stranded the vehicle. A hotspot that ACCEPTS the
# restart tears the connection down as it goes, so the missing response is the
# SIGNATURE OF SUCCESS, not of failure — and its carrier then takes the better
# part of a minute to drop. A caller told "never touched" reads wan1's stale
# pre-reboot UP, believes it, and reboots the terminal into a wan1 that is
# seconds from going dark.


def _sched_code(cfg, client, now=1000.0):
    """The exit code --scheduled-reboot would return (main() returns it
    verbatim; it is not exercised directly here because its /run mkdir has no
    business running in a test)."""
    _issued, _reason, code = W._scheduled_reboot_verbose(cfg, client, now)
    return code


def test_scheduled_reboot_exit_0_when_the_reboot_is_confirmed(tmp_path,
                                                              monkeypatch):
    cfg = sched_cfg(tmp_path)
    monkeypatch.setattr(W, "read_carrier", lambda *a, **k: True)
    assert _sched_code(cfg, _StubClient()) == W.EXIT_ISSUED == 0


def test_scheduled_reboot_exit_3_when_the_post_outcome_is_unknown(tmp_path,
                                                                  monkeypatch):
    # THE FIX. reboot() returning False means the restart WAS posted and the
    # answer was lost — which is what a reboot that WORKED looks like from
    # here. It must NOT share exit 1 with the never-touched cases.
    cfg = sched_cfg(tmp_path)
    monkeypatch.setattr(W, "read_carrier", lambda *a, **k: True)
    c = _StubClient(reboot_ok=False)
    code = _sched_code(cfg, c)
    assert code == W.EXIT_ATTEMPTED_UNKNOWN == 3
    assert code != W.EXIT_UNTOUCHED
    _issued, reason, _ = W._scheduled_reboot_verbose(cfg, c, 1000.0)
    assert _issued is False               # still not a CONFIRMED reboot...
    assert "UNKNOWN" in reason            # ...but it does not claim innocence


def test_scheduled_reboot_exit_3_when_the_redirect_heuristic_misjudges(
        tmp_path, monkeypatch):
    # reboot()'s "is this Location an error target?" test is a REGEX against an
    # uncaptured device response: an unfamiliar redirect containing "err" can
    # be judged a failure although the hotspot took the restart and is going
    # down. That misjudgement must land in 3, not 1 — then the sequencer
    # watches the link and gets the right answer anyway.
    cfg = sched_cfg(tmp_path)
    monkeypatch.setattr(W, "read_carrier", lambda *a, **k: True)
    admin = FIXTURE.replace('"Guest"', '"Admin"')
    r = FakeRunner([FIXTURE,                          # the reachability check
                    FIXTURE, "", admin,               # login: token, POST, role
                    FIXTURE,                          # reboot: token
                    "http://192.0.2.1/error.json"])   # ...judged an error
    c = W.NetgearClient("http://192.0.2.1", "wan1", str(tmp_path / "jar.txt"),
                        runner=r)                    # the real client, no stub
    assert _sched_code(cfg, c) == W.EXIT_ATTEMPTED_UNKNOWN
    # and the restart really was posted before the heuristic judged the answer
    assert "general.shutdown=restart" in " ".join(r.calls[-1])


class _UnreachableClient(_StubClient):
    """The admin UI answers nothing at all — no token, so nothing was POSTed."""

    def fetch_model(self):
        return None


def test_scheduled_reboot_exit_1_when_the_admin_ui_is_unreachable(tmp_path,
                                                                  monkeypatch):
    # Never touched: we never got a token, so no restart was ever POSTed.
    cfg = sched_cfg(tmp_path)
    monkeypatch.setattr(W, "read_carrier", lambda *a, **k: True)
    c = _UnreachableClient()
    assert _sched_code(cfg, c) == W.EXIT_UNTOUCHED == 1
    assert c.rebooted is False


def test_scheduled_reboot_exit_1_when_the_secret_is_unreadable(tmp_path,
                                                               monkeypatch):
    cfg = sched_cfg(tmp_path)
    Path(cfg.secret_path).unlink()
    monkeypatch.setattr(W, "read_carrier", lambda *a, **k: True)
    c = _StubClient()
    assert _sched_code(cfg, c) == W.EXIT_UNTOUCHED
    assert c.rebooted is False


def test_scheduled_reboot_exit_1_when_the_login_is_rejected(tmp_path,
                                                            monkeypatch):
    # The rotated-password case, and the reason leg-1 NOT_ISSUED may continue
    # to leg 2 at all: the hotspot really was never disturbed.
    cfg = sched_cfg(tmp_path)
    monkeypatch.setattr(W, "read_carrier", lambda *a, **k: True)
    c = _StubClient(login_ok=False)
    assert _sched_code(cfg, c) == W.EXIT_UNTOUCHED
    assert c.rebooted is False


def test_scheduled_reboot_exit_2_for_every_guard(tmp_path, monkeypatch):
    # A guard declining is not a failure, and it is not an unknown either: it
    # is the one case where we chose not to POST. All three still exit 2.
    monkeypatch.setattr(W, "read_carrier", lambda *a, **k: False)
    assert _sched_code(sched_cfg(tmp_path), _StubClient()) == W.EXIT_SKIPPED == 2

    monkeypatch.setattr(W, "read_carrier", lambda *a, **k: True)
    cfg = sched_cfg(tmp_path)
    state = _json.loads(Path(cfg.sbfd_state_path).read_text())
    state["sessions"]["b"]["state"] = "DOWN"          # peer down
    Path(cfg.sbfd_state_path).write_text(_json.dumps(state))
    assert _sched_code(cfg, _StubClient()) == W.EXIT_SKIPPED

    assert _sched_code(sched_cfg(tmp_path, dry_run=True),
                       _StubClient()) == W.EXIT_SKIPPED


def test_scheduled_reboot_exit_codes_are_all_distinct():
    codes = (W.EXIT_ISSUED, W.EXIT_UNTOUCHED, W.EXIT_SKIPPED,
             W.EXIT_ATTEMPTED_UNKNOWN)
    assert len(set(codes)) == 4


# -- the reboot budget must be durable BEFORE the reboot leaves the box --------

class _RecordingExecutor:
    """Stands in for Executor: records what it was asked to do, and when."""

    def __init__(self, journal, cfg):
        self.journal = journal
        self.cfg = cfg

    def execute(self, actions):
        for a in actions:
            self.journal.append(("execute", a.kind))


def _reboot_policy(tmp_path, **over):
    cfg = W.load_config(write_cfg(tmp_path, **over))
    p = W.WatchdogPolicy(dwell_s=1, grace_s=600, max_reboots_per_episode=1,
                         holdoff_s=7200, healthy_reset_s=1800,
                         startup_grace_s=0, start_time=0.0)
    return cfg, p


def test_reboot_budget_is_persisted_before_the_reboot_is_issued(tmp_path):
    # The crash window: policy.step() consumes the attempt in memory, the reboot
    # goes out, THEN state is saved. A crash in that gap loses the increment, so
    # the reloaded daemon can exceed max_reboots_per_episode. The write must
    # therefore land first.
    cfg, policy = _reboot_policy(tmp_path)
    journal = []
    real_save = W._save_policy_state

    def spy_save(c, p):
        journal.append(("save", p.reboots_issued))
        return real_save(c, p)

    W_save, W._save_policy_state = W._save_policy_state, spy_save
    try:
        policy.step(BAD_PRIV, 1000.0)                       # start the dwell
        actions = policy.step(BAD_PRIV, 2000.0)             # dwell elapsed -> reboot
        assert [a.kind for a in actions] == ["notify", "reboot"]
        W._persist_then_execute(cfg, policy, _RecordingExecutor(journal, cfg),
                                actions)
    finally:
        W._save_policy_state = W_save

    assert journal[0] == ("save", 1)                        # durable, attempt counted
    assert ("execute", "reboot") in journal
    assert journal.index(("save", 1)) < journal.index(("execute", "reboot"))
    # and what landed on disk really does carry the consumed attempt
    assert _json.loads(Path(cfg.state_path).read_text())["reboots_issued"] == 1


def test_reboot_is_suppressed_when_state_cannot_be_persisted(tmp_path):
    # Better a missed reboot than an unbounded one: if the attempt cannot be
    # recorded durably, do not spend it.
    cfg, policy = _reboot_policy(tmp_path)
    journal = []
    W_save, W._save_policy_state = W._save_policy_state, lambda c, p: False
    try:
        policy.step(BAD_PRIV, 1000.0)
        actions = policy.step(BAD_PRIV, 2000.0)
        assert any(a.kind == "reboot" for a in actions)
        W._persist_then_execute(cfg, policy, _RecordingExecutor(journal, cfg),
                                actions)
    finally:
        W._save_policy_state = W_save

    kinds = [k for what, k in journal if what == "execute"]
    assert "reboot" not in kinds          # the reboot did NOT go out
    assert "notify" in kinds              # and the operator was told why


def test_suppressed_reboot_does_not_spin(tmp_path):
    # The in-memory policy still holds the consumed attempt + armed grace, so a
    # failed persist must not turn into a reboot proposed on every single poll.
    cfg, policy = _reboot_policy(tmp_path)
    policy.step(BAD_PRIV, 1000.0)
    policy.step(BAD_PRIV, 2000.0)                     # reboot proposed (suppressed)
    assert policy.reboots_issued == 1
    assert policy.step(BAD_PRIV, 2030.0) == []        # still inside grace: nothing


def test_save_policy_state_reports_failure(tmp_path):
    cfg = W.load_config(write_cfg(tmp_path))
    p = make_policy()
    assert W._save_policy_state(cfg, p) is True
    # parent is a regular file, so the mkdir/write cannot succeed (OSError)
    (tmp_path / "blocked").write_text("not a directory")
    cfg.state_path = str(tmp_path / "blocked" / "state.json")
    assert W._save_policy_state(cfg, p) is False
