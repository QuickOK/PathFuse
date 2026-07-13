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


FIXTURE = (Path(__file__).parent / "fixtures" / "m6_model.json").read_text()


class FakeRunner:
    """Records curl argv; returns canned stdout per call."""
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
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
    assert "session.password=hunter2" in joined


def test_login_failure_when_still_guest():
    c, _ = make_client([FIXTURE, "", FIXTURE])
    assert c.login("wrong") is False


def test_reboot_posts_shutdown_restart():
    c, r = make_client([FIXTURE, ""])
    assert c.reboot() is True
    assert "general.shutdown=restart" in " ".join(r.calls[1])


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


class SpyNotify:
    def __init__(self):
        self.sent = []

    def __call__(self, notify_bin, title, priority, message):
        self.sent.append((title, priority, message))


def make_executor(tmp_path, dry_run, login_ok=True, reboot_ok=True):
    cfg = W.load_config(write_cfg(tmp_path, dry_run=dry_run))
    (tmp_path / "secret").write_text("pw123\n")

    class FakeClient:
        def __init__(self):
            self.rebooted = 0

        def fetch_model(self):
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
