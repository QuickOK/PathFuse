import notify


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def ev(kind="wan_switch", title="t", message="m", priority="high"):
    return notify.Event(kind=kind, title=title, message=message, priority=priority)


def test_first_event_admitted_immediately():
    rl = notify.RateLimiter(30.0, clock=FakeClock())
    assert rl.admit(ev()) is not None


def test_second_event_within_window_is_held():
    clk = FakeClock()
    rl = notify.RateLimiter(30.0, clock=clk)
    rl.admit(ev(title="first"))
    clk.advance(5)
    assert rl.admit(ev(title="second")) is None
    assert rl.next_deadline() == 1030.0   # first send at t=1000 + 30s window


def test_flush_due_emits_summary_with_count():
    clk = FakeClock()
    rl = notify.RateLimiter(30.0, clock=clk)
    rl.admit(ev(title="first"))
    clk.advance(5)
    rl.admit(ev(title="second", message="m2"))
    rl.admit(ev(title="third", message="m3"))
    assert rl.flush_due() == []           # window not over yet
    clk.advance(25)
    out = rl.flush_due()
    assert len(out) == 1
    assert out[0].kind == "wan_switch"
    assert "third" in out[0].title and "×2" in out[0].title
    assert out[0].message == "m3"
    assert rl.next_deadline() is None


def test_single_held_event_flushes_unmodified():
    clk = FakeClock()
    rl = notify.RateLimiter(30.0, clock=clk)
    rl.admit(ev(title="first"))
    clk.advance(5)
    rl.admit(ev(title="second"))
    clk.advance(30)
    out = rl.flush_due()
    assert len(out) == 1
    assert out[0].title == "second"       # no ×N suffix for a single event


def test_kinds_are_independent():
    clk = FakeClock()
    rl = notify.RateLimiter(30.0, clock=clk)
    rl.admit(ev(kind="wan_switch"))
    clk.advance(1)
    # A different kind is not delayed by wan_switch's window.
    assert rl.admit(ev(kind="all_wans_down", priority="max")) is not None


def test_window_reopens_after_flush():
    clk = FakeClock()
    rl = notify.RateLimiter(30.0, clock=clk)
    rl.admit(ev(title="a"))
    clk.advance(5)
    rl.admit(ev(title="b"))
    clk.advance(30)
    rl.flush_due()
    clk.advance(30)                        # a full quiet window after the summary
    assert rl.admit(ev(title="c")) is not None


def test_admit_after_quiet_window_sends_immediately():
    clk = FakeClock()
    rl = notify.RateLimiter(30.0, clock=clk)
    rl.admit(ev(title="a"))
    clk.advance(31)
    assert rl.admit(ev(title="b")) is not None


import json
import os
import stat
import time


def make_fake_spool_notify(tmp_path):
    """A fake spool-notify that appends one JSON line per invocation."""
    log = tmp_path / "sent.jsonl"
    script = tmp_path / "fake-spool-notify"
    script.write_text(
        '#!/bin/bash\n'
        f'printf \'{{"title": "%s", "priority": "%s", "message": "%s", '
        f'"topic": "%s"}}\\n\' "$1" "$2" "$3" "${{NOTIFY_TOPIC:-}}" '
        f'>> "{log}"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, log


def read_sent(log):
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def wait_for(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_notifier_sends_via_command_with_topic_env(tmp_path):
    script, log = make_fake_spool_notify(tmp_path)
    n = notify.Notifier("pathfusetest", command=str(script))
    n.start()
    try:
        n.notify(ev(kind="started", title="start", message="hello",
                    priority="low"))
        assert wait_for(lambda: len(read_sent(log)) == 1)
        sent = read_sent(log)[0]
        assert sent == {"title": "start", "priority": "low",
                        "message": "hello", "topic": "pathfusetest"}
    finally:
        n.stop()


def test_notifier_coalesces_repeats(tmp_path):
    script, log = make_fake_spool_notify(tmp_path)
    n = notify.Notifier("pathfusetest", min_interval_s=0.3, command=str(script))
    n.start()
    try:
        for i in range(4):
            n.notify(ev(title=f"switch{i}"))
        # First sends immediately; the other three coalesce into one summary.
        assert wait_for(lambda: len(read_sent(log)) == 2, timeout=5.0)
        time.sleep(0.5)
        sent = read_sent(log)
        assert len(sent) == 2
        assert sent[0]["title"] == "switch0"
        assert "switch3" in sent[1]["title"] and "×3" in sent[1]["title"]
    finally:
        n.stop()


def test_notifier_drops_oldest_on_overflow(tmp_path):
    script, log = make_fake_spool_notify(tmp_path)
    n = notify.Notifier("pathfusetest", command=str(script))
    # Do NOT start the worker: fill the buffer synchronously.
    for i in range(60):
        n.notify(ev(kind=f"k{i}", title=f"t{i}"))
    n.start()
    try:
        assert wait_for(lambda: len(read_sent(log)) == 50)
        time.sleep(0.2)
        sent = read_sent(log)
        assert len(sent) == 50
        assert sent[0]["title"] == "t10"     # t0..t9 were dropped
        assert sent[-1]["title"] == "t59"
    finally:
        n.stop()


def test_notifier_flushes_held_events_on_stop(tmp_path):
    script, log = make_fake_spool_notify(tmp_path)
    n = notify.Notifier("pathfusetest", min_interval_s=30.0,
                        command=str(script))
    n.start()
    try:
        n.notify(ev(title="first"))
        assert wait_for(lambda: len(read_sent(log)) == 1)
        # Second same-kind event lands inside the 30s window and is held.
        n.notify(ev(title="second"))
        time.sleep(0.2)
    finally:
        n.stop()
    sent = read_sent(log)
    assert len(sent) == 2
    assert sent[0]["title"] == "first"
    assert sent[1]["title"] == "second"   # single held event, unmodified


def test_notifier_survives_failing_command(tmp_path):
    script = tmp_path / "fail"
    script.write_text("#!/bin/bash\nexit 3\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log_script, log = make_fake_spool_notify(tmp_path)
    n = notify.Notifier("pathfusetest", command=str(script))
    n.start()
    try:
        n.notify(ev(kind="a", title="doomed"))
        time.sleep(0.3)
        # Worker is still alive: swap in nothing, just send another event kind.
        n.notify(ev(kind="b", title="alive"))
        time.sleep(0.3)
        assert n._thread.is_alive()
    finally:
        n.stop()


# -- EventDetector tests ------------------------------------------------


def obs(**kw):
    base = dict(
        wan_states={"wan1": "UP", "wan2": "UP"},
        wan_labels={"wan1": "Cellular", "wan2": "Satellite"},
        mode="master_backup",
        env_active=False, env_reason="",
        fec_engaged=False, fec_at_max=False,
        relay_polled=False, relay_ok=True,
        switch=None,
    )
    base.update(kw)
    return notify.Observation(**base)


def seeded(clk=None, wall=None, **kw):
    # `wall` is the wall clock the maintenance window's `until` epoch is read
    # against; `clk` is the monotonic clock the hold timers are measured on.
    d = notify.EventDetector(relay_fail_threshold=3, clock=clk or FakeClock(),
                             wall_clock=wall or FakeClock(), **kw)
    assert d.observe(obs()) == []          # first observation seeds silently
    return d


def kinds(evs):
    return [e.kind for e in evs]


def test_seed_is_silent_even_with_bad_state():
    d = notify.EventDetector()
    first = d.observe(obs(wan_states={"wan1": "DOWN", "wan2": "DOWN"},
                          fec_engaged=True, env_active=True, env_reason="wind"))
    assert first == []


def test_wan_down_and_up_edges():
    clk = FakeClock()
    d = seeded(clk)
    down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"})
    assert d.observe(down) == []           # held: not down long enough yet
    clk.advance(10)
    evs = d.observe(down)
    assert kinds(evs) == ["wan_down"]
    assert "Satellite" in evs[0].title and evs[0].priority == "high"
    assert d.observe(down) == []           # no repeat while still down
    evs = d.observe(obs())
    assert kinds(evs) == ["wan_up"]
    assert evs[0].priority == "default"


def test_wan_down_alert_waits_for_the_hold():
    clk = FakeClock()
    d = seeded(clk)
    down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"})
    for _ in range(19):                    # 19 ticks x 0.5s = 9.5s: silent
        assert d.observe(down) == []
        clk.advance(0.5)
    assert d.observe(down) == []           # t = 9.5s, still inside the hold
    clk.advance(0.5)
    evs = d.observe(down)                  # t = 10.0s
    assert kinds(evs) == ["wan_down"]
    assert "UP → DOWN" in evs[0].message


def test_wan_blip_shorter_than_hold_is_silent():
    clk = FakeClock()
    d = seeded(clk)
    assert d.observe(obs(wan_states={"wan1": "UP", "wan2": "DOWN"})) == []
    clk.advance(9)
    # Recovered before the hold expired: no down alert was sent, so no
    # recovery alert either -- the flap is invisible.
    assert d.observe(obs()) == []
    clk.advance(60)
    assert d.observe(obs()) == []


def test_wan_down_hold_restarts_after_a_recovery():
    clk = FakeClock()
    d = seeded(clk)
    down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"})
    d.observe(down)
    clk.advance(9)
    d.observe(obs())                       # brief recovery clears the timer
    clk.advance(9)
    assert d.observe(down) == []           # fresh outage, fresh 10s hold
    clk.advance(10)
    assert kinds(d.observe(down)) == ["wan_down"]


def test_unknown_down_transitions_do_not_fire():
    clk = FakeClock()
    d = seeded(clk)
    down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"})
    d.observe(down)
    clk.advance(10)
    assert kinds(d.observe(down)) == ["wan_down"]
    # DOWN -> UNKNOWN is not a recovery; UNKNOWN -> DOWN is not a new outage.
    assert d.observe(obs(wan_states={"wan1": "UP", "wan2": "UNKNOWN"})) == []
    assert d.observe(down) == []


def test_non_up_states_accumulate_toward_one_hold():
    clk = FakeClock()
    d = seeded(clk)
    # UNKNOWN for 6s then DOWN for 4s is 10s of not-UP: one alert at the end.
    assert d.observe(obs(wan_states={"wan1": "UP", "wan2": "UNKNOWN"})) == []
    clk.advance(6)
    assert d.observe(obs(wan_states={"wan1": "UP", "wan2": "DOWN"})) == []
    clk.advance(4)
    evs = d.observe(obs(wan_states={"wan1": "UP", "wan2": "DOWN"}))
    assert kinds(evs) == ["wan_down"]
    assert "UP → DOWN" in evs[0].message   # reported from the last UP state


def test_all_wans_down_fires_alongside_wan_down():
    clk = FakeClock()
    d = seeded(clk)
    d.observe(obs(wan_states={"wan1": "UP", "wan2": "DOWN"}))
    clk.advance(10)
    assert kinds(d.observe(obs(wan_states={"wan1": "UP", "wan2": "DOWN"}))) \
        == ["wan_down"]
    both = obs(wan_states={"wan1": "DOWN", "wan2": "DOWN"})
    assert d.observe(both) == []           # wan1 and all-down both held
    clk.advance(10)
    evs = d.observe(both)
    assert sorted(kinds(evs)) == ["all_wans_down", "wan_down"]
    all_down = [e for e in evs if e.kind == "all_wans_down"][0]
    assert all_down.priority == "max"
    # No repeat while still down.
    assert d.observe(both) == []


def test_all_wans_down_blip_is_silent():
    clk = FakeClock()
    d = seeded(clk)
    assert d.observe(obs(wan_states={"wan1": "DOWN", "wan2": "DOWN"})) == []
    clk.advance(5)
    assert d.observe(obs()) == []          # recovered inside the hold
    clk.advance(60)
    assert d.observe(obs()) == []


def test_wan_switch_passthrough():
    d = seeded()
    evs = d.observe(obs(switch=(["wan1", "wan2"], ["wan2"], "master down")))
    assert kinds(evs) == ["wan_switch"]
    assert "wan2" in evs[0].title
    assert "master down" in evs[0].message
    assert evs[0].priority == "high"


def test_redundancy_mode_edges():
    d = seeded()
    evs = d.observe(obs(mode="full"))
    assert kinds(evs) == ["redundancy"]
    assert "operator" in evs[0].message
    evs = d.observe(obs(mode="master_backup"))
    assert kinds(evs) == ["redundancy"]


def test_env_override_engage_and_clear():
    d = seeded()
    evs = d.observe(obs(mode="full", env_active=True, env_reason="high wind"))
    ks = kinds(evs)
    assert "env_override" in ks and "redundancy" in ks
    envev = [e for e in evs if e.kind == "env_override"][0]
    assert "high wind" in envev.message and envev.priority == "high"
    redev = [e for e in evs if e.kind == "redundancy"][0]
    assert "environmental" in redev.message
    evs = d.observe(obs(mode="master_backup"))
    assert "env_override" in kinds(evs)          # cleared


def test_fec_alerts_are_off_by_default():
    d = seeded()
    assert d.observe(obs(fec_engaged=True)) == []
    assert d.observe(obs(fec_engaged=True, fec_at_max=True)) == []
    assert d.observe(obs(fec_engaged=False, fec_at_max=False)) == []


def test_fec_engage_disengage_and_max_when_enabled():
    d = seeded(fec_alerts=True)
    evs = d.observe(obs(fec_engaged=True))
    assert kinds(evs) == ["fec"]
    assert "engaged" in evs[0].title.lower()
    evs = d.observe(obs(fec_engaged=True, fec_at_max=True))
    assert kinds(evs) == ["fec"]
    assert "max" in evs[0].title.lower()
    assert d.observe(obs(fec_engaged=True, fec_at_max=True)) == []
    evs = d.observe(obs(fec_engaged=False, fec_at_max=False))
    assert "disengaged" in evs[0].title.lower()


def test_relay_unreachable_after_threshold_and_restore():
    d = seeded()   # relay_fail_threshold=3
    assert d.observe(obs(relay_polled=True, relay_ok=False)) == []
    assert d.observe(obs(relay_polled=True, relay_ok=False)) == []
    evs = d.observe(obs(relay_polled=True, relay_ok=False))
    assert kinds(evs) == ["relay"]
    assert evs[0].priority == "high"
    # Stays quiet while still down; non-poll ticks don't count.
    assert d.observe(obs(relay_polled=False, relay_ok=False)) == []
    assert d.observe(obs(relay_polled=True, relay_ok=False)) == []
    evs = d.observe(obs(relay_polled=True, relay_ok=True))
    assert kinds(evs) == ["relay"]
    assert "restored" in evs[0].title.lower()


def test_relay_blip_below_threshold_is_silent():
    d = seeded()
    d.observe(obs(relay_polled=True, relay_ok=False))
    d.observe(obs(relay_polled=True, relay_ok=False))
    assert d.observe(obs(relay_polled=True, relay_ok=True)) == []


def test_wan_states_unknown_blip_keeps_edge_state():
    clk = FakeClock()
    d = seeded(clk)   # wan1, wan2 both UP
    unknown = obs(wan_states={"wan1": "UNKNOWN", "wan2": "UNKNOWN"})
    assert d.observe(unknown) == []
    clk.advance(10)
    evs = d.observe(unknown)
    assert sorted(kinds(evs)) == ["all_wans_down", "wan_down", "wan_down"]
    # No WAN is UP, so the alert fires; UNKNOWN-only blips don't reset state.
    evs = d.observe(obs(wan_states={"wan1": "UP", "wan2": "UP"}))
    assert kinds(evs) == ["wan_up", "wan_up"]


def test_wan_already_down_at_seed_never_alerts_but_recovery_does():
    clk = FakeClock()
    d = notify.EventDetector(clock=clk)
    assert d.observe(obs(wan_states={"wan1": "UP", "wan2": "DOWN"})) == []
    clk.advance(60)                        # a restart must not replay the outage
    assert d.observe(obs(wan_states={"wan1": "UP", "wan2": "DOWN"})) == []
    assert kinds(d.observe(obs())) == ["wan_up"]


def test_all_wans_down_at_seed_never_alerts():
    clk = FakeClock()
    d = notify.EventDetector(clock=clk)
    allbad = obs(wan_states={"wan1": "DOWN", "wan2": "DOWN"})
    assert d.observe(allbad) == []
    clk.advance(60)
    assert d.observe(allbad) == []


def test_seed_with_failing_relay_poll_counts_toward_threshold():
    d = notify.EventDetector(relay_fail_threshold=3)
    # Seed tick with a failing relay poll: seeds silently, but the failure
    # still counts (_relay_fails starts at 1, not 0).
    assert d.observe(obs(relay_polled=True, relay_ok=False)) == []
    assert d.observe(obs(relay_polled=True, relay_ok=False)) == []
    evs = d.observe(obs(relay_polled=True, relay_ok=False))
    assert kinds(evs) == ["relay"]


# -- maintenance-window suppression -------------------------------------


def test_maintenance_window_suppresses_that_wans_down_and_up():
    # (b) Down and back inside the window: no wan_down, and no spurious wan_up.
    clk, wclk = FakeClock(), FakeClock()
    d = seeded(clk, wclk)
    win = {"wan": "wan2", "until": wclk.t + 600}
    down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"}, maintenance=win)
    assert d.observe(down) == []                # tick 1 starts the down timer
    clk.advance(30)
    wclk.advance(30)
    assert d.observe(down) == []                # would be wan_down but for the
    up = obs(maintenance=win)                   # window: 30s > the 10s hold
    assert d.observe(up) == []                  # and no wan_up on the way back
    clk.advance(600)
    wclk.advance(600)
    assert d.observe(obs()) == []               # nothing deferred to after it


def test_maintenance_window_does_not_suppress_the_other_wan():
    # Suppression is per-WAN: a window on wan2 must not mute a real wan1 outage.
    clk, wclk = FakeClock(), FakeClock()
    d = seeded(clk, wclk)
    win = {"wan": "wan2", "until": wclk.t + 600}
    down = obs(wan_states={"wan1": "DOWN", "wan2": "UP"}, maintenance=win)
    assert d.observe(down) == []           # held: not down long enough yet
    clk.advance(30)
    assert kinds(d.observe(down)) == ["wan_down"]


def test_all_wans_down_always_pages_even_during_maintenance():
    # Whatever else maintenance silences, "the vehicle is offline" always pages.
    clk, wclk = FakeClock(), FakeClock()
    d = seeded(clk, wclk)
    win = {"wan": "wan2", "until": wclk.t + 600}
    both = obs(wan_states={"wan1": "DOWN", "wan2": "DOWN"}, maintenance=win)
    assert d.observe(both) == []           # held: not down long enough yet
    clk.advance(30)
    evs = d.observe(both)
    assert "all_wans_down" in kinds(evs)
    assert [e for e in evs if e.kind == "all_wans_down"][0].priority == "max"


def test_expired_maintenance_window_suppresses_nothing():
    # Fail safe: a stale window must not mute a real outage.
    clk, wclk = FakeClock(), FakeClock()
    d = seeded(clk, wclk)
    win = {"wan": "wan2", "until": wclk.t - 1}
    down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"}, maintenance=win)
    assert d.observe(down) == []           # held: not down long enough yet
    clk.advance(30)
    assert kinds(d.observe(down)) == ["wan_down"]


def test_window_expiry_is_judged_on_the_wall_clock_not_the_monotonic_one():
    # `until` is a wall-clock epoch; read against seconds-since-boot every
    # window looks open forever and a stale one would hide a real outage.
    clk = FakeClock(2_200_000.0)                 # monotonic: uptime
    wclk = FakeClock(1_780_000_000.0)            # wall clock: an epoch
    d = seeded(clk, wclk)
    win = {"wan": "wan2", "until": wclk.t - 86_400}   # expired 24h ago
    down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"}, maintenance=win)
    assert d.observe(down) == []           # held: not down long enough yet
    clk.advance(30)
    wclk.advance(30)
    assert kinds(d.observe(down)) == ["wan_down"]


def test_wan_still_down_when_the_window_expires_is_announced():
    # (a) The window ends the excuse, not the outage: the down edge is only
    # deferred, and fires once the window closes (hold restarted from close).
    clk, wclk = FakeClock(), FakeClock()
    d = seeded(clk, wclk)
    win = {"wan": "wan2", "until": wclk.t + 100}
    down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"}, maintenance=win)
    assert d.observe(down) == []           # suppressed: on purpose
    clk.advance(50)
    wclk.advance(50)
    assert d.observe(down) == []           # still inside the window
    clk.advance(60)
    wclk.advance(60)
    assert d.observe(down) == []           # window just expired: hold restarts
    clk.advance(10)
    evs = d.observe(down)
    assert kinds(evs) == ["wan_down"]      # now unexplained -- page
    assert evs[0].priority == "high"
    # And the recovery of that announced outage still reports.
    assert kinds(d.observe(obs())) == ["wan_up"]


def test_recovery_during_a_window_of_an_outage_announced_before_it():
    # (c) The operator was paged for a real outage; a window opening afterwards
    # must not swallow the "it's back" for the alert they already have.
    clk, wclk = FakeClock(), FakeClock()
    d = seeded(clk, wclk)
    down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"})
    assert d.observe(down) == []
    clk.advance(10)
    assert kinds(d.observe(down)) == ["wan_down"]      # real, announced
    win = {"wan": "wan2", "until": wclk.t + 600}
    maint_down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"},
                     maintenance=win)
    assert d.observe(maint_down) == []                 # no repeat
    assert kinds(d.observe(obs(maintenance=win))) == ["wan_up"]


def test_pre_existing_outage_inside_the_hold_alerts_after_the_window():
    # (d) An outage whose hold had not elapsed when the window opened is only
    # deferred by it, never absorbed: it alerts once the window expires.
    clk, wclk = FakeClock(), FakeClock()
    d = seeded(clk, wclk)
    down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"})
    assert d.observe(down) == []           # real outage, 5s into its 10s hold
    clk.advance(5)
    wclk.advance(5)
    win = {"wan": "wan2", "until": wclk.t + 100}
    maint_down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"},
                     maintenance=win)
    assert d.observe(maint_down) == []     # window opens over it: deferred
    clk.advance(50)
    wclk.advance(50)
    assert d.observe(maint_down) == []
    clk.advance(60)
    wclk.advance(60)                       # window expired, still down
    assert kinds(d.observe(down)) == ["wan_down"]


def test_malformed_maintenance_window_suppresses_nothing():
    clk, wclk = FakeClock(), FakeClock()
    d = seeded(clk, wclk)
    down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"},
               maintenance={"garbage": True})
    assert d.observe(down) == []           # held: not down long enough yet
    clk.advance(30)
    assert kinds(d.observe(down)) == ["wan_down"]


def test_maintenance_window_suppresses_the_switch_event():
    clk, wclk = FakeClock(), FakeClock()
    d = seeded(clk, wclk)
    win = {"wan": "wan2", "until": wclk.t + 600}
    evs = d.observe(obs(maintenance=win,
                        switch=(["wan1", "wan2"], ["wan1"], "wan2 down")))
    assert kinds(evs) == []


def test_switch_caused_by_the_other_wan_still_reports_during_a_window():
    # Only the maintained WAN *leaving* the active set is the reboot; a switch
    # it did not cause is real context and must still report.
    clk, wclk = FakeClock(), FakeClock()
    d = seeded(clk, wclk)
    win = {"wan": "wan2", "until": wclk.t + 600}
    evs = d.observe(obs(maintenance=win,
                        switch=(["wan1", "wan2"], ["wan2"], "wan1 down")))
    assert kinds(evs) == ["wan_switch"]
    assert "wan1 down" in evs[0].message


def test_maintained_wan_rejoining_the_active_set_still_reports():
    # The maintained WAN coming BACK is a switch it caused, but not the reboot.
    clk, wclk = FakeClock(), FakeClock()
    d = seeded(clk, wclk)
    win = {"wan": "wan2", "until": wclk.t + 600}
    evs = d.observe(obs(maintenance=win,
                        switch=(["wan1"], ["wan1", "wan2"], "wan2 up")))
    assert kinds(evs) == ["wan_switch"]
