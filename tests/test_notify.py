import builtins
import errno
import io
import logging
import threading
from pathlib import Path
from typing import Any, Optional

import pytest
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


# -- a page's on_sent -----------------------------------------------------------------
#
# An Event may carry on_sent. The Notifier's worker runs it once spool-notify has taken
# that page (exit 0), and never for a page that did not get there. The egress alert
# record hangs on it (see "the record follows the pages spool-notify took" below).


def _spool_notify(tmp_path, name="spool-notify", rc=0, refuse=None):
    """A spool-notify stand-in that logs the title it is handed, then exits rc, or 1
    when the title is `refuse`."""
    log = tmp_path / f"{name}.log"
    script = tmp_path / name
    refusal = f'[ "$1" = "{refuse}" ] && exit 1\n' if refuse is not None else ""
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{log}"\n{refusal}exit {rc}\n',
                      encoding="utf-8")
    script.chmod(0o755)
    return str(script), log


def _handed(log):
    """The titles a _spool_notify stand-in was handed, in order."""
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_an_events_on_sent_is_no_part_of_the_page():
    # on_sent rides along with a page. Equality and repr stay the page's own, and the
    # four fields still construct by position.
    plain = notify.Event("egress", "🧭 Egress fallback", "m", "high")
    carrying = notify.Event("egress", "🧭 Egress fallback", "m", "high", on_sent=lambda: None)
    assert plain.on_sent is None
    assert carrying == plain
    assert repr(carrying) == repr(plain)


def test_a_folded_summary_carries_the_last_held_events_on_sent():
    # The last held event is the latest state of its kind, so its on_sent is the one the
    # summary's send runs. The held events before it are out of date, and theirs never run.
    clk = FakeClock()
    rl = notify.RateLimiter(30.0, clock=clk)
    ran = []

    def page(title):
        return notify.Event("egress", title, "m", "high", on_sent=lambda: ran.append(title))

    assert rl.admit(page("first")) is not None
    assert rl.admit(page("second")) is None
    assert rl.admit(page("third")) is None
    clk.advance(30)
    [summary] = rl.flush_due()
    assert "third" in summary.title and "×2" in summary.title
    summary.on_sent()
    assert ran == ["third"]


def test_notifier_runs_on_sent_once_spool_notify_took_the_page(tmp_path):
    script, log = _spool_notify(tmp_path)
    handed_by_then, done = [], threading.Event()

    def on_sent():
        handed_by_then.append(_handed(log))
        done.set()

    n = notify.Notifier("pathfusetest", command=script)
    n.start()
    try:
        n.notify(notify.Event("egress", "fallback", "m", "high", on_sent=on_sent))
        assert done.wait(5)
    finally:
        n.stop()
    assert handed_by_then == [["fallback"]]     # once, after the page was handed over


def test_notifier_runs_on_sent_for_a_held_page_its_window_releases(tmp_path):
    # A page held in its kind's window goes out when the window closes, not only at
    # stop(), and its on_sent runs then.
    script, log = _spool_notify(tmp_path)
    released = threading.Event()
    n = notify.Notifier("pathfusetest", min_interval_s=1.0, command=script)
    n.notify(notify.Event("egress", "first", "m", "high"))
    n.notify(notify.Event("egress", "second", "m", "high", on_sent=released.set))
    n.start()                              # one batch: "first" goes out, "second" is held
    try:
        assert released.wait(5)
        assert _handed(log) == ["first", "second"]
    finally:
        n.stop()


def test_notifier_logs_an_on_sent_that_raises_and_keeps_sending(tmp_path, caplog):
    script, log = _spool_notify(tmp_path)
    after = threading.Event()

    def broken():
        raise RuntimeError("on_sent broke")

    n = notify.Notifier("pathfusetest", command=script)
    n.notify(notify.Event("egress", "first", "m", "high", on_sent=broken))
    n.notify(notify.Event("relay", "second", "m", "high", on_sent=after.set))
    n.start()                              # one batch: the rest of it must still go out
    try:
        assert after.wait(5)
    finally:
        n.stop()
    assert _handed(log) == ["first", "second"]
    logged = [r for r in caplog.records
              if r.exc_info and str(r.exc_info[1]) == "on_sent broke"]
    assert len(logged) == 1 and logged[0].levelno == logging.ERROR   # logging.exception
    assert "egress" in logged[0].getMessage()


# -- EventDetector tests ------------------------------------------------


def obs(**kw):
    base: dict[str, Any] = dict(
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
    d = seeded(switch_hold_s=0.0)   # the hold has its own tests; isolate the rule
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


def test_redundancy_mode_edge_suppressed_when_handoff_window_caused_it():
    # Enter: the window forces master_backup -> full on the SAME tick as
    # handoff_active flips on.
    d = seeded()
    evs = d.observe(obs(mode="full", handoff_active=True))
    assert evs == []
    # Leave: the window closes on the same tick the mode reverts. The new
    # observation's own handoff_active is already False here -- only the
    # PREVIOUS observation's flag (still True going in) proves the window
    # caused this transition too.
    evs = d.observe(obs(mode="master_backup", handoff_active=False))
    assert evs == []


def test_redundancy_mode_edge_still_fires_for_operator_cause():
    d = seeded()
    evs = d.observe(obs(mode="full"))
    assert kinds(evs) == ["redundancy"]
    assert "operator" in evs[0].message


def test_redundancy_mode_edge_unsticks_after_window_truly_clears():
    # window-full -> operator-holds-full -> window-clears must not fire on
    # the clear (mode never actually changes, since the operator already
    # wants full on their own) -- but the handoff_active flag must not get
    # stuck true forever: a LATER, genuinely operator-caused transition with
    # no window involved must still fire.
    d = seeded()
    evs = d.observe(obs(mode="full", handoff_active=True))      # window-full
    assert evs == []
    evs = d.observe(obs(mode="full", handoff_active=True))      # operator-holds-full
    assert evs == []
    evs = d.observe(obs(mode="full", handoff_active=False))     # window-clears
    assert evs == []
    evs = d.observe(obs(mode="master_backup", handoff_active=False))
    assert kinds(evs) == ["redundancy"]                         # genuine, later


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


def test_maintenance_window_suppresses_the_maintained_wans_FAIL_BACK():
    # The maintained WAN REJOINING the active set is the other half of its own
    # reboot, not news. A live run paged "🔀 WAN switch → wan2" on every
    # maintenance night because only its departure was suppressed.
    clk, wclk = FakeClock(), FakeClock()
    d = seeded(clk, wclk)
    win = {"wan": "wan2", "until": wclk.t + 600}
    evs = d.observe(obs(maintenance=win,
                        switch=(["wan1"], ["wan1", "wan2"], "wan2 up, fail-back")))
    assert kinds(evs) == []


def test_maintenance_window_still_reports_a_switch_the_other_wan_caused():
    # wan2 is under maintenance, but wan1 REALLY failed. That must still report.
    clk, wclk = FakeClock(), FakeClock()
    d = seeded(clk, wclk, switch_hold_s=0.0)
    win = {"wan": "wan2", "until": wclk.t + 600}
    evs = d.observe(obs(maintenance=win,
                        switch=(["wan1", "wan2"], ["wan2"], "wan1 down")))
    assert kinds(evs) == ["wan_switch"]


def test_switch_caused_by_the_other_wan_still_reports_during_a_window():
    # Only the maintained WAN *leaving* the active set is the reboot; a switch
    # it did not cause is real context and must still report.
    clk, wclk = FakeClock(), FakeClock()
    d = seeded(clk, wclk, switch_hold_s=0.0)
    win = {"wan": "wan2", "until": wclk.t + 600}
    evs = d.observe(obs(maintenance=win,
                        switch=(["wan1", "wan2"], ["wan2"], "wan1 down")))
    assert kinds(evs) == ["wan_switch"]
    assert "wan1 down" in evs[0].message


def test_maintained_wan_rejoining_the_active_set_is_also_the_reboot():
    # This test used to assert the REJOIN reports ("a switch it caused, but not
    # the reboot"). The live supervised run disproved that: the fail-back is the
    # other half of the same reboot, and it paged on every maintenance night.
    # Its departure and its return are one event; excuse both.
    clk, wclk = FakeClock(), FakeClock()
    d = seeded(clk, wclk)
    win = {"wan": "wan2", "until": wclk.t + 600}
    evs = d.observe(obs(maintenance=win,
                        switch=(["wan1"], ["wan1", "wan2"], "wan2 up")))
    assert kinds(evs) == []


def test_switch_that_removed_the_other_wan_too_still_reports():
    # `maint in frm and maint not in to` also swallowed a switch that removed
    # the maintained WAN *and* another one — a strictly WORSE event than the
    # reboot it was excusing. Only the maintained WAN being the SOLE member
    # removed from the active set is the reboot.
    clk, wclk = FakeClock(), FakeClock()
    win = {"wan": "wan2", "until": wclk.t + 600}
    for to in ([], ["wan3"]):
        d = seeded(FakeClock(), FakeClock(), switch_hold_s=0.0)
        evs = d.observe(obs(maintenance=win,
                            switch=(["wan1", "wan2"], to, "wan1 died too")))
        assert kinds(evs) == ["wan_switch"]
        assert "wan1 died too" in evs[0].message


def test_non_finite_until_suppresses_nothing():
    # `{"until": Infinity}` is REACHABLE: json.loads accepts the bareword, and
    # `self._wall_clock() < inf` is true forever — that WAN's outages would be
    # silenced permanently, the one failure mode that can hide a real outage.
    # NaN fails the other way (every comparison False) but is no more a
    # timestamp, and a bool is not one either.
    assert json.loads('{"until": Infinity}')["until"] == float("inf")
    for until in (float("inf"), float("-inf"), float("nan"), True, False):
        clk, wclk = FakeClock(), FakeClock()
        d = seeded(clk, wclk)
        win = {"wan": "wan2", "until": until}
        down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"}, maintenance=win)
        assert d.observe(down) == []       # held: not down long enough yet
        clk.advance(30)
        wclk.advance(30)
        assert kinds(d.observe(down)) == ["wan_down"]


def test_bool_until_never_suppresses_even_on_an_unsynced_clock():
    # `true` is not a timestamp — but isinstance(True, int) is True in Python,
    # and True is finite, so only an explicit bool check catches it. On a box
    # whose clock has not yet synced (no RTC: time.time() starts near the
    # epoch), `0.0 < True` is True, and the window would mute a real outage.
    clk, wclk = FakeClock(), FakeClock(0.0)
    d = seeded(clk, wclk)
    down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"},
               maintenance={"wan": "wan2", "until": True})
    assert d.observe(down) == []           # held: not down long enough yet
    clk.advance(30)
    assert kinds(d.observe(down)) == ["wan_down"]


def test_seed_under_a_window_leaves_the_down_pending_not_announced():
    # THE PERMANENT SILENCE: sbfd-ctl restarting while a WAN is down under an
    # open window used to seed it into _down_alerted ("already announced"), and
    # _wan_events skips those forever — turning a WITHHELD outage into one that
    # is never reported, even long after the window expires.
    clk, wclk = FakeClock(), FakeClock()
    d = notify.EventDetector(clock=clk, wall_clock=wclk)
    win = {"wan": "wan2", "until": wclk.t + 100}
    down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"}, maintenance=win)
    assert d.observe(down) == []           # the seed: silent, as always
    clk.advance(20)
    wclk.advance(20)
    assert d.observe(down) == []           # still inside the window: withheld
    clk.advance(90)
    wclk.advance(90)
    assert d.observe(down) == []           # window just expired: hold restarts
    clk.advance(10)
    evs = d.observe(down)
    assert kinds(evs) == ["wan_down"]      # now unexplained — page
    assert evs[0].priority == "high"
    assert kinds(d.observe(obs())) == ["wan_up"]   # and its recovery reports


def test_seed_under_a_window_that_recovers_inside_it_stays_silent():
    # the other half: a restart mid-window whose WAN comes back before the
    # window closes is a normal maintenance night — no down, and no spurious up
    clk, wclk = FakeClock(), FakeClock()
    d = notify.EventDetector(clock=clk, wall_clock=wclk)
    win = {"wan": "wan2", "until": wclk.t + 600}
    assert d.observe(obs(wan_states={"wan1": "UP", "wan2": "DOWN"},
                         maintenance=win)) == []
    clk.advance(30)
    wclk.advance(30)
    assert d.observe(obs(maintenance=win)) == []   # back up: silent
    clk.advance(600)
    wclk.advance(600)
    assert d.observe(obs()) == []                  # nothing deferred past it


def test_seed_still_announces_nothing_for_a_wan_down_outside_any_window():
    # the pre-existing invariant must not weaken: a WAN already down at seed
    # with NO window is still treated as announced, so a restart never replays
    # an outage the operator was already told about
    clk, wclk = FakeClock(), FakeClock()
    d = notify.EventDetector(clock=clk, wall_clock=wclk)
    win = {"wan": "wan1", "until": wclk.t + 600}   # window is on the OTHER wan
    down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"}, maintenance=win)
    assert d.observe(down) == []
    clk.advance(600)
    wclk.advance(600)
    assert d.observe(obs(wan_states={"wan1": "UP", "wan2": "DOWN"})) == []


# -- switch hysteresis --------------------------------------------------------

def test_a_switch_that_flaps_straight_back_is_never_announced():
    # THE BUG THIS FIXES, replayed from a real afternoon: the satellite WAN
    # dropped and failed back ~25s later, five times in six hours. Each
    # excursion produced TWO high-priority pages (away, then home) — 10 pages
    # for an outage the operator can do nothing about. A switch that reverts
    # within the hold never happened, as far as they are concerned.
    clk = FakeClock()
    d = seeded(clk, switch_hold_s=60.0)
    assert d.observe(obs(switch=(["wan2"], ["wan1"], "master (wan2) DOWN"))) == []
    clk.advance(25)                       # the observed flap interval
    assert d.observe(obs(switch=(["wan1"], ["wan2"], "master (wan2) UP, fail-back"))) == []
    clk.advance(300)                      # ...and it stays quiet forever after
    assert d.observe(obs()) == []


def test_a_switch_that_sticks_still_pages():
    # Hysteresis must not swallow a real failover — only a flap.
    clk = FakeClock()
    d = seeded(clk, switch_hold_s=60.0)
    assert d.observe(obs(switch=(["wan2"], ["wan1"], "master (wan2) DOWN"))) == []
    clk.advance(59)
    assert d.observe(obs()) == []         # still inside the hold
    clk.advance(1)
    evs = d.observe(obs())                # t = 60s: it held
    assert kinds(evs) == ["wan_switch"]
    assert evs[0].priority == "high"
    assert "wan1" in evs[0].title


def test_a_switch_is_announced_once_not_repeatedly():
    clk = FakeClock()
    d = seeded(clk, switch_hold_s=60.0)
    d.observe(obs(switch=(["wan2"], ["wan1"], "master (wan2) DOWN")))
    clk.advance(60)
    assert kinds(d.observe(obs())) == ["wan_switch"]
    clk.advance(600)
    assert d.observe(obs()) == []         # no repeat


def test_continuous_churn_stays_silent_until_it_settles():
    # While the active set is still churning there is nothing stable worth
    # announcing; each new target restarts the hold. When it finally settles,
    # the operator hears the outcome once.
    clk = FakeClock()
    d = seeded(clk, switch_hold_s=60.0)
    for _ in range(4):
        assert d.observe(obs(switch=(["wan2"], ["wan1"], "flap"))) == []
        clk.advance(20)
        assert d.observe(obs(switch=(["wan1"], ["wan2"], "flap back"))) == []
        clk.advance(20)
    assert d.observe(obs(switch=(["wan2"], ["wan1"], "master (wan2) DOWN"))) == []
    clk.advance(60)
    assert kinds(d.observe(obs())) == ["wan_switch"]


def test_a_flapping_wan_still_reports_its_own_outage():
    # The switch hold must never hide a WAN that is actually down: wan_down has
    # its own, much shorter hold and is untouched by this.
    clk = FakeClock()
    d = seeded(clk, switch_hold_s=60.0)
    down = obs(wan_states={"wan1": "UP", "wan2": "DOWN"},
               switch=(["wan2"], ["wan1"], "master (wan2) DOWN"))
    assert d.observe(down) == []
    clk.advance(15)                       # past wan_down_hold_s, inside switch hold
    evs = d.observe(obs(wan_states={"wan1": "UP", "wan2": "DOWN"}))
    assert kinds(evs) == ["wan_down"]     # the outage still pages...
    assert evs[0].priority == "high"


def test_maintenance_still_wins_over_the_switch_hold():
    # A maintained WAN's departure/return is excused outright — it must not sit
    # pending and then fire once the hold expires.
    clk, wclk = FakeClock(), FakeClock()
    d = seeded(clk, wclk, switch_hold_s=60.0)
    win = {"wan": "wan2", "until": wclk.t + 600}
    assert d.observe(obs(maintenance=win,
                         switch=(["wan1", "wan2"], ["wan1"], "wan2 down"))) == []
    clk.advance(300)
    assert d.observe(obs(maintenance=win)) == []


# -- egress fallback ----------------------------------------------------------


def _eg(status: str, selected: str = "relay_backbone", observed: Optional[str] = "relay_direct",
        ip: Optional[str] = "198.51.100.20") -> dict:
    return {"selected": selected, "observed": observed, "ip": ip, "status": status,
            "since": 1.0, "checked_at": 1.0, "error": None}


def test_egress_label_known_and_unknown():
    assert notify.egress_label("relay_backbone") == "relay Backbone"
    assert notify.egress_label("relay_direct") == "relay Direct"
    assert notify.egress_label("banana") == "banana"
    assert notify.egress_label(None) == "unknown"


def test_egress_mismatch_pages_once_then_restores():
    d = notify.EventDetector()
    assert d.observe(obs(egress=_eg("checking"))) == []          # seed
    assert d.observe(obs(egress=_eg("pending"))) == []
    evs = d.observe(obs(egress=_eg("mismatch")))
    assert len(evs) == 1 and evs[0].kind == "egress" and evs[0].priority == "high"
    assert "relay Backbone" in evs[0].message and "relay Direct" in evs[0].message
    assert "198.51.100.20" in evs[0].message
    assert d.observe(obs(egress=_eg("mismatch"))) == []          # no repeat
    assert d.observe(obs(egress=_eg("error"))) == []             # an error is not a recovery
    evs = d.observe(obs(egress=_eg("match", observed="relay_backbone")))
    assert len(evs) == 1 and "restored" in evs[0].title.lower()
    assert evs[0].priority == "default"                          # a recovery is not an alarm


def test_egress_skipped_clears_the_alert_silently():
    d = notify.EventDetector()
    d.observe(obs(egress=_eg("checking")))
    d.observe(obs(egress=_eg("mismatch")))
    assert d.observe(obs(egress=_eg("skipped", selected="local_direct"))) == []
    assert d.observe(obs(egress=_eg("match", selected="relay_direct", observed="relay_direct"))) == []


def _eg_checking(selected):
    # What the observer publishes right after the selected mode changes: the
    # evaluation restarts and nothing is known about the exit yet.
    return _eg("checking", selected=selected, observed=None, ip=None)


def test_egress_a_mode_change_clears_the_alert_silently():
    d = notify.EventDetector()
    d.observe(obs(egress=_eg("checking")))
    assert len(d.observe(obs(egress=_eg("mismatch")))) == 1      # relay Backbone fell back
    assert d.observe(obs(egress=_eg_checking("relay_direct"))) == []   # the mode changed
    # The alert was about relay Backbone, so relay Direct holding is no recovery.
    assert d.observe(obs(egress=_eg("match", selected="relay_direct", observed="relay_direct"))) == []


def test_egress_a_fallback_on_the_new_mode_pages_afresh():
    d = notify.EventDetector()
    d.observe(obs(egress=_eg("checking")))
    assert len(d.observe(obs(egress=_eg("mismatch")))) == 1      # relay Backbone fell back
    assert d.observe(obs(egress=_eg_checking("relay_vpn"))) == []
    assert d.observe(obs(egress=_eg("pending", selected="relay_vpn"))) == []
    evs = d.observe(obs(egress=_eg("mismatch", selected="relay_vpn")))
    assert len(evs) == 1 and evs[0].kind == "egress" and evs[0].priority == "high"
    assert "selected relay-VPN" in evs[0].message and "actual relay Direct" in evs[0].message


@pytest.mark.parametrize("ending", [
    pytest.param(_eg("match", observed="relay_backbone"), id="match"),
    pytest.param(_eg("skipped", selected="local_direct"), id="skipped"),
    pytest.param(_eg_checking("relay_vpn"), id="mode-change"),
])
def test_egress_a_new_mismatch_pages_again_after_the_alert_ends(ending):
    # Each ending is fed on its own, so a branch that forgets to re-arm the page
    # cannot be rescued by another one. After the mode change, the mismatch below is
    # relay Backbone's again: selected once more, it starts with nothing announced.
    d = notify.EventDetector()
    d.observe(obs(egress=_eg("checking")))
    assert len(d.observe(obs(egress=_eg("mismatch")))) == 1
    d.observe(obs(egress=ending))
    evs = d.observe(obs(egress=_eg("mismatch")))
    assert len(evs) == 1 and evs[0].kind == "egress" and evs[0].priority == "high"


def test_egress_mismatch_at_startup_counts_as_announced():
    # As with a WAN already down at startup: a restart must not page on a fallback
    # it finds in place, but the recovery from it is still news.
    d = notify.EventDetector()
    assert d.observe(obs(egress=_eg("mismatch"))) == []          # seed
    assert d.observe(obs(egress=_eg("mismatch"))) == []          # no page: already announced
    evs = d.observe(obs(egress=_eg("match", observed="relay_backbone")))
    assert len(evs) == 1 and evs[0].kind == "egress" and "restored" in evs[0].title.lower()


@pytest.mark.parametrize("first", [
    pytest.param(_eg("checking"), id="checking"),
    pytest.param(_eg("pending"), id="pending"),
    pytest.param(_eg("error"), id="error"),
    pytest.param(_eg("match", observed="relay_backbone"), id="match"),
    pytest.param(_eg("skipped", selected="local_direct"), id="skipped"),
])
def test_egress_only_a_mismatch_at_startup_counts_as_announced(first):
    # Anything short of a confirmed mismatch at startup leaves the page armed:
    # seeding it as announced would swallow the first real fallback.
    d = notify.EventDetector()
    assert d.observe(obs(egress=first)) == []                    # seed
    evs = d.observe(obs(egress=_eg("mismatch")))
    assert len(evs) == 1 and evs[0].kind == "egress" and evs[0].priority == "high"


def test_egress_none_is_ignored():
    d = notify.EventDetector()
    d.observe(obs())
    assert d.observe(obs()) == []


# -- an egress alert across a restart -----------------------------------------------
#
# With egress_alert_path set, the detector records each fallback it announces and
# removes the record when the alert ends. A restart is a second detector on the same
# path, fed what a fresh observer reports: `checking` until its first check (its settle
# alone outlasts many ticks), then its verdicts.

FALLBACK, RESTORED = "🧭 Egress fallback", "🧭 Egress restored"
ANNOUNCED_AT = 1_780_000_000.0   # a wall-clock epoch


def _titles(evs):
    return [e.title for e in evs]


def _write_record(path, selected):
    path.write_text(json.dumps({"selected": selected, "announced_at": ANNOUNCED_AT}))


def _warnings(caplog):
    # The detector runs on the test's own thread; a thread another test left running
    # must not change the count.
    me = threading.get_ident()
    return [r for r in caplog.records if r.levelno == logging.WARNING and r.thread == me]


def _sent(evs):
    """The titles of `evs`, each page handed to spool-notify and taken. The Notifier
    runs a page's on_sent once spool-notify exits 0; these tests run it here."""
    for e in evs:
        if e.on_sent is not None:
            e.on_sent()
    return _titles(evs)


def test_egress_a_fallback_announced_before_a_restart_is_not_paged_again(tmp_path):
    path = tmp_path / "egress_alert.json"
    before = notify.EventDetector(egress_alert_path=str(path))
    assert before.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert before.observe(obs(egress=_eg("pending"))) == []
    assert _sent(before.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    assert path.exists()
    after = notify.EventDetector(egress_alert_path=str(path))   # the restart
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []   # its seed
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []   # still settling
    assert after.observe(obs(egress=_eg("pending"))) == []
    assert after.observe(obs(egress=_eg("mismatch"))) == []     # paged before the restart
    assert after.observe(obs(egress=_eg("mismatch"))) == []
    evs = after.observe(obs(egress=_eg("match", observed="relay_backbone")))
    assert _sent(evs) == [RESTORED]
    assert not path.exists()
    assert after.observe(obs(egress=_eg("match", observed="relay_backbone"))) == []


def test_egress_a_fallback_standing_across_two_restarts_is_paged_once(tmp_path):
    # A deploy restart and then a reboot, both during one fallback. The restart that
    # takes the alert over must leave its record for the next one, or that one pages
    # the fallback again.
    path = tmp_path / "egress_alert.json"
    pages = []
    d = notify.EventDetector(egress_alert_path=str(path))
    for e in (_eg_checking("relay_backbone"), _eg("pending"), _eg("mismatch")):
        pages += _sent(d.observe(obs(egress=e)))
    for restart in ("the deploy", "the reboot"):
        d = notify.EventDetector(egress_alert_path=str(path))
        for e in (_eg_checking("relay_backbone"), _eg_checking("relay_backbone"),
                  _eg("pending"), _eg("mismatch"), _eg("mismatch")):
            pages += _sent(d.observe(obs(egress=e)))
        assert path.exists(), f"the fallback still stands after {restart}, so must its record"
    pages += _sent(d.observe(obs(egress=_eg("match", observed="relay_backbone"))))
    assert pages == [FALLBACK, RESTORED]
    assert not path.exists()


@pytest.mark.parametrize("seed", [
    pytest.param(_eg_checking("relay_backbone"), id="checking"),
    pytest.param(_eg("mismatch"), id="mismatch"),
])
def test_egress_a_restart_that_takes_the_alert_over_leaves_its_record_as_written(tmp_path, seed):
    # The record says when the fallback was paged. A restart that takes the alert over
    # pages nothing, and nor do the checks that find it still standing, so none of them
    # writes the record: announced_at still names the page, not the restart. A write
    # per tick would also wear the box's flash.
    path = tmp_path / "egress_alert.json"
    _write_record(path, "relay_backbone")              # paged an hour before the restart
    after = notify.EventDetector(egress_alert_path=str(path),
                                 wall_clock=FakeClock(ANNOUNCED_AT + 3600.0))
    for e in (seed, _eg("mismatch"), _eg("mismatch")):
        assert _sent(after.observe(obs(egress=e))) == []
    assert json.loads(path.read_text()) == {"selected": "relay_backbone",
                                            "announced_at": ANNOUNCED_AT}


def test_egress_a_fallback_that_began_while_down_pages_after_the_restart(tmp_path, caplog):
    path = tmp_path / "egress_alert.json"     # no record: nothing was announced
    after = notify.EventDetector(egress_alert_path=str(path))
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert after.observe(obs(egress=_eg("pending"))) == []
    assert _titles(after.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    assert after.observe(obs(egress=_eg("mismatch"))) == []
    assert _warnings(caplog) == []            # no record is the normal case, not a fault


@pytest.mark.parametrize("seed", [
    pytest.param(_eg_checking("relay_backbone"), id="checking"),
    pytest.param(_eg("match", observed="relay_backbone"), id="match"),
])
def test_egress_a_fallback_that_recovered_while_down_pages_the_restore(tmp_path, seed):
    path = tmp_path / "egress_alert.json"
    _write_record(path, "relay_backbone")     # paged before the restart, never restored
    after = notify.EventDetector(egress_alert_path=str(path))
    assert after.observe(obs(egress=seed)) == []
    evs = after.observe(obs(egress=_eg("match", observed="relay_backbone")))
    assert _sent(evs) == [RESTORED]
    assert not path.exists()
    assert after.observe(obs(egress=_eg("match", observed="relay_backbone"))) == []


def test_egress_a_record_for_another_mode_is_dropped_at_the_restart(tmp_path):
    # The selected mode changed while the controller was down. A change ends an alert
    # silently while it runs, and does so across a restart too.
    path = tmp_path / "egress_alert.json"
    _write_record(path, "relay_backbone")
    after = notify.EventDetector(egress_alert_path=str(path))
    assert after.observe(obs(egress=_eg_checking("relay_vpn"))) == []
    assert not path.exists()
    assert after.observe(obs(egress=_eg("match", selected="relay_vpn", observed="relay_vpn"))) == []
    assert _titles(after.observe(obs(egress=_eg("mismatch", selected="relay_vpn")))) == [FALLBACK]


@pytest.mark.parametrize("prior", [None, "relay_backbone"], ids=["no-record", "record-for-another-mode"])
def test_egress_a_fallback_found_at_startup_is_recorded_like_any_other(tmp_path, prior):
    path = tmp_path / "egress_alert.json"
    if prior is not None:
        _write_record(path, prior)
    first = notify.EventDetector(egress_alert_path=str(path))
    assert first.observe(obs(egress=_eg("mismatch", selected="relay_vpn"))) == []   # counted as announced
    assert json.loads(path.read_text())["selected"] == "relay_vpn"
    again = notify.EventDetector(egress_alert_path=str(path))   # so a restart keeps it announced
    assert again.observe(obs(egress=_eg_checking("relay_vpn"))) == []
    assert again.observe(obs(egress=_eg("mismatch", selected="relay_vpn"))) == []
    evs = again.observe(obs(egress=_eg("match", selected="relay_vpn", observed="relay_vpn")))
    assert _titles(evs) == [RESTORED]


def test_egress_a_mode_change_ends_the_alert_and_its_record(tmp_path):
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    assert path.exists()
    assert d.observe(obs(egress=_eg_checking("relay_vpn"))) == []
    assert not path.exists()
    # The alert was about relay Backbone, so relay-VPN holding is no recovery,
    assert d.observe(obs(egress=_eg("match", selected="relay_vpn", observed="relay_vpn"))) == []
    # and a fallback on relay-VPN is news.
    assert _sent(d.observe(obs(egress=_eg("mismatch", selected="relay_vpn")))) == [FALLBACK]
    assert json.loads(path.read_text())["selected"] == "relay_vpn"


@pytest.mark.parametrize("selected", ["local_direct", "relay_backbone"])
def test_egress_skipped_ends_the_alert_and_its_record(tmp_path, selected):
    # The observer skips its check while local_direct is selected. `skipped` ends an
    # alert even under the alert's own mode, so the rule does not lean on a mode change.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    assert path.exists()
    assert d.observe(obs(egress=_eg("skipped", selected=selected, observed=None, ip=None))) == []
    assert not path.exists()
    assert d.observe(obs(egress=_eg("match", observed="relay_backbone"))) == []   # no restore
    assert _titles(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]


@pytest.mark.parametrize("ending, want", [
    pytest.param(_eg("match", observed="relay_backbone"), [RESTORED], id="restore"),
    pytest.param(_eg_checking("relay_vpn"), [], id="mode-change"),
])
def test_egress_an_alert_whose_record_is_already_gone_ends_without_a_warning(
        tmp_path, caplog, ending, want):
    # A record already missing when its alert ends is fine: deleted by hand, or never
    # written because the write failed. The alert ends as usual and nothing is logged.
    # A warning here would say a restart may take the fallback for still standing,
    # which is false.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    path.unlink()
    assert _sent(d.observe(obs(egress=ending))) == want
    assert _warnings(caplog) == []


@pytest.mark.parametrize("body", [
    pytest.param(b'{"selected": "relay_backbone"', id="garbled"),
    pytest.param(b'{"selected": "relay_\xffbackbone"}', id="not-utf-8"),
    pytest.param(b'["relay_backbone"]', id="a-list"),
    pytest.param(b'"relay_backbone"', id="a-string"),
    pytest.param(b'{"announced_at": 1780000000.0}', id="no-selected"),
    pytest.param(b'{"selected": 5}', id="selected-not-a-string"),
    pytest.param(b'{"selected": ""}', id="selected-empty"),
])
def test_egress_a_bad_record_counts_as_no_record(tmp_path, caplog, body):
    path = tmp_path / "egress_alert.json"
    path.write_bytes(body)
    after = notify.EventDetector(egress_alert_path=str(path))
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert len(_warnings(caplog)) == 1        # said once, at the seed
    assert _titles(after.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]


def test_egress_an_unreadable_record_is_left_in_place(tmp_path, caplog, monkeypatch):
    path = tmp_path / "egress_alert.json"
    _write_record(path, "relay_vpn")          # another mode's: if it could be read, it would go
    real_open = builtins.open

    def refuse_the_record(file, *a, **kw):
        if os.fspath(file) == str(path):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", refuse_the_record)
    monkeypatch.setattr(io, "open", refuse_the_record)
    after = notify.EventDetector(egress_alert_path=str(path))
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert len(_warnings(caplog)) == 1
    assert path.exists()


@pytest.mark.parametrize("layout", ["parent-is-a-file", "path-is-a-directory"])
def test_egress_alert_io_failures_are_logged_and_never_raise(tmp_path, caplog, layout):
    if layout == "parent-is-a-file":
        (tmp_path / "state").write_text("not a directory")
        path = tmp_path / "state" / "egress_alert.json"   # reads, writes and removes all fail
    else:
        path = tmp_path / "egress_alert.json"
        path.mkdir()                                       # reads, replaces and removes fail
    d = notify.EventDetector(egress_alert_path=str(path))
    assert d.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert len(_warnings(caplog)) == 1                     # the read
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    assert len(_warnings(caplog)) == 2                     # the write
    assert _sent(d.observe(obs(egress=_eg("match", observed="relay_backbone")))) == [RESTORED]
    assert len(_warnings(caplog)) == 3                     # the remove
    left = {"parent-is-a-file": ["state"], "path-is-a-directory": ["egress_alert.json"]}[layout]
    assert sorted(p.name for p in tmp_path.iterdir()) == left   # no temp file left behind


def test_egress_alert_path_none_does_no_file_io(monkeypatch):
    # Every file operation the detector could reach fails on this thread, and is noted.
    # Each page is handed over too, so its on_sent runs here as well.
    me, attempted = threading.get_ident(), []

    def refusing(name, real):
        def call(*a, **kw):
            if threading.get_ident() != me:
                return real(*a, **kw)
            attempted.append(name)
            raise PermissionError(errno.EACCES, f"{name}: no file I/O expected")
        return call

    monkeypatch.setattr(builtins, "open", refusing("open", builtins.open))
    monkeypatch.setattr(io, "open", refusing("io.open", io.open))
    for name in ("open", "replace", "rename", "remove", "unlink", "makedirs", "mkdir", "fsync"):
        monkeypatch.setattr(os, name, refusing(f"os.{name}", getattr(os, name)))
    d = notify.EventDetector()
    pages = []
    for e in (_eg("mismatch"),                                   # the seed: counted as announced
              _eg("match", observed="relay_backbone"),           # restored
              _eg("mismatch"),                                   # a fallback
              _eg_checking("relay_vpn"),                         # a mode change ends it
              _eg("mismatch", selected="relay_vpn"),             # a fallback
              _eg("skipped", selected="local_direct", observed=None, ip=None)):
        pages += _sent(d.observe(obs(egress=e)))
    assert pages == [RESTORED, FALLBACK, FALLBACK]
    assert attempted == []


def test_notify_cfg_keeps_its_record_in_the_state_directory_by_default():
    # The detector alone keeps no record by default; the controller's config does, in
    # sbfd-ctl's StateDirectory. load_config passes the path explicitly, so its tests
    # cannot see this default.
    assert notify.DEFAULT_EGRESS_ALERT_PATH == "/var/lib/sbfd-ctl/egress_alert.json"
    assert notify.NotifyCfg(topic="t").egress_alert_path == notify.DEFAULT_EGRESS_ALERT_PATH


def test_egress_alert_record_is_written_whole_in_a_directory_made_for_it(tmp_path, monkeypatch):
    path = tmp_path / "state" / "egress_alert.json"   # the directory does not exist yet
    real_replace, replaced = os.replace, []

    def replace(src, dst, **kw):
        if os.fspath(dst) == str(path):
            replaced.append((os.fspath(src), Path(src).read_text(), path.exists()))
        return real_replace(src, dst, **kw)

    monkeypatch.setattr(os, "replace", replace)
    d = notify.EventDetector(egress_alert_path=str(path), clock=FakeClock(1000.0),
                             wall_clock=FakeClock(ANNOUNCED_AT))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    want = {"selected": "relay_backbone", "announced_at": ANNOUNCED_AT}
    # A reader never sees part of it: the record is written whole to a temp file beside
    # the path, and only then renamed over it.
    assert len(replaced) == 1
    src, body, visible_before = replaced[0]
    assert os.path.dirname(src) == str(path.parent) and src != str(path)
    assert json.loads(body) == want and not visible_before
    assert json.loads(path.read_text()) == want
    assert [p.name for p in path.parent.iterdir()] == ["egress_alert.json"]


def test_egress_alert_record_is_read_at_a_seed_with_the_check_on_and_only_then(tmp_path, caplog):
    path = tmp_path / "egress_alert.json"
    path.mkdir()                              # any read of it fails, and says so
    off = notify.EventDetector(egress_alert_path=str(path))
    assert _warnings(caplog) == []            # constructing reads nothing,
    assert off.observe(obs()) == []           # nor does a seed with the check off
    assert off.observe(obs()) == []
    assert _warnings(caplog) == []
    on = notify.EventDetector(egress_alert_path=str(path))
    assert on.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert len(_warnings(caplog)) == 1        # whereas a seed with it on reads the record


# -- the record follows the pages spool-notify took -----------------------------------
#
# The record says what the operator was last paged about, so it changes only when
# spool-notify takes a page: a fallback page's on_sent writes it, a restore page's
# removes it. A page that never got that far changes nothing, so a restart sends it
# again. Where a test needs the worker, the detector's pages go to a real Notifier with
# a stand-in spool-notify; the others run on_sent by hand.


def _broken_spool_notify(tmp_path, monkeypatch, failure):
    """A spool-notify that does not take the page: it exits 1, cannot be run at all
    (an OSError), or outlives the Notifier's timeout."""
    if failure == "exit-1":
        return _spool_notify(tmp_path, rc=1)[0]
    if failure == "oserror":
        return str(tmp_path / "no-such-spool-notify")
    monkeypatch.setattr(notify.Notifier, "SUBPROCESS_TIMEOUT_S", 0.2)
    script = tmp_path / "hung-spool-notify"
    script.write_text("#!/bin/sh\nexec sleep 30\n")
    script.chmod(0o755)
    return str(script)


def test_egress_a_fallback_page_spool_notify_took_is_recorded(tmp_path):
    path = tmp_path / "egress_alert.json"
    script, log = _spool_notify(tmp_path)
    d = notify.EventDetector(egress_alert_path=str(path), wall_clock=FakeClock(ANNOUNCED_AT))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    evs = d.observe(obs(egress=_eg("mismatch")))
    assert _titles(evs) == [FALLBACK]
    assert not path.exists()                  # raised, but not yet handed over
    n = notify.Notifier("pathfusetest", command=script)
    n.start()
    for e in evs:
        n.notify(e)
    n.stop()                                  # the worker sends what it holds, then ends
    assert _handed(log) == [FALLBACK]
    assert json.loads(path.read_text()) == {"selected": "relay_backbone",
                                            "announced_at": ANNOUNCED_AT}


def test_egress_a_fallback_page_left_in_the_buffer_is_paged_again_after_a_restart(tmp_path):
    # The controller stopped before the worker sent the page (stop() waits only 5 s), so
    # the page died with the process. The operator never heard of the fallback, and the
    # restart must page it.
    path = tmp_path / "egress_alert.json"
    script, log = _spool_notify(tmp_path)
    n = notify.Notifier("pathfusetest", command=script)
    n.start()
    n.stop()                                  # the worker is gone: nothing more goes out
    before = notify.EventDetector(egress_alert_path=str(path))
    before.observe(obs(egress=_eg_checking("relay_backbone")))
    for e in before.observe(obs(egress=_eg("mismatch"))):
        n.notify(e)
    assert _handed(log) == []
    assert not path.exists()
    after = notify.EventDetector(egress_alert_path=str(path))   # the restart
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert after.observe(obs(egress=_eg("pending"))) == []
    assert _titles(after.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]


@pytest.mark.parametrize("failure", ["exit-1", "oserror", "timeout"])
def test_egress_a_fallback_page_spool_notify_did_not_take_is_paged_again_after_a_restart(
        tmp_path, monkeypatch, caplog, failure):
    # spool-notify spools a page it cannot deliver, so a failure here is local: the
    # Notifier logs it and drops the page. The fallback was never announced.
    path = tmp_path / "egress_alert.json"
    n = notify.Notifier("pathfusetest",
                        command=_broken_spool_notify(tmp_path, monkeypatch, failure))
    n.start()
    before = notify.EventDetector(egress_alert_path=str(path))
    before.observe(obs(egress=_eg_checking("relay_backbone")))
    for e in before.observe(obs(egress=_eg("mismatch"))):
        n.notify(e)
    n.stop()
    worker = n._thread
    assert worker is not None and not worker.is_alive()
    logged = [r.levelno for r in caplog.records if r.thread == worker.ident]
    assert logged == [logging.WARNING]        # the send was tried, and failed
    assert not path.exists()
    after = notify.EventDetector(egress_alert_path=str(path))   # the restart
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert _titles(after.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]


def test_egress_a_restore_page_spool_notify_refused_is_sent_again_after_a_restart(tmp_path):
    # The alert ends in memory at once, so this run pages nothing more. The record stays
    # until a restore page goes out: the operator still believes the fallback stands, and
    # the restart that confirms the match tells them it does not.
    path = tmp_path / "egress_alert.json"
    _write_record(path, "relay_backbone")      # its fallback was paged before a restart
    script, log = _spool_notify(tmp_path, rc=1)
    d = notify.EventDetector(egress_alert_path=str(path))
    assert d.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    n = notify.Notifier("pathfusetest", command=script)
    n.start()
    for e in d.observe(obs(egress=_eg("match", observed="relay_backbone"))):
        n.notify(e)
    n.stop()
    assert _handed(log) == [RESTORED]          # handed over, and refused
    assert d.observe(obs(egress=_eg("match", observed="relay_backbone"))) == []
    assert json.loads(path.read_text()) == {"selected": "relay_backbone",
                                            "announced_at": ANNOUNCED_AT}
    after = notify.EventDetector(egress_alert_path=str(path))   # the restart
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert _titles(after.observe(obs(egress=_eg("match", observed="relay_backbone")))) == [RESTORED]


@pytest.mark.parametrize("then", [
    pytest.param([], id="restored"),
    pytest.param([_eg("mismatch")], id="restored-and-fallen-back-again"),
])
def test_egress_a_fallback_page_that_goes_out_after_its_alert_moved_on_writes_no_record(
        tmp_path, then):
    # Pages queue in the Notifier, so a page can go out after its alert has moved on.
    # Its on_sent then writes nothing: the newer page settles the record.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    [late] = d.observe(obs(egress=_eg("mismatch")))
    assert _titles(d.observe(obs(egress=_eg("match", observed="relay_backbone")))) == [RESTORED]
    for e in then:                            # the same mode falls back again
        assert _titles(d.observe(obs(egress=e))) == [FALLBACK]
    late.on_sent()
    assert not path.exists()


@pytest.mark.parametrize("then", [
    pytest.param([], id="fallen-back-again"),
    pytest.param([_eg("match", observed="relay_backbone")], id="fallen-back-again-and-restored"),
])
def test_egress_a_restore_page_that_goes_out_after_a_new_fallback_leaves_the_record(
        tmp_path, then):
    # The reverse order. A restore page that goes out late removes nothing once a newer
    # page exists: a new fallback, and perhaps its restore. The record waits for the
    # newest page.
    path = tmp_path / "egress_alert.json"
    _write_record(path, "relay_backbone")      # paged before a restart
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))   # taken over
    [late] = d.observe(obs(egress=_eg("match", observed="relay_backbone")))
    assert _titles(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    for e in then:
        assert _titles(d.observe(obs(egress=e))) == [RESTORED]
    late.on_sent()
    assert json.loads(path.read_text()) == {"selected": "relay_backbone",
                                            "announced_at": ANNOUNCED_AT}


def test_egress_a_summary_that_ends_in_a_restore_removes_the_record(tmp_path):
    # Pages of one kind held in the same window go out as one summary, which says what
    # the last of them said, so the record follows that last page. Here a restore page
    # that spool-notify refuses opens the window, so the record is still there when the
    # summary of the held [fallback, restore] goes out.
    path = tmp_path / "egress_alert.json"
    _write_record(path, "relay_backbone")      # paged before a restart
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))   # taken over
    script, log = _spool_notify(tmp_path, refuse=RESTORED)
    n = notify.Notifier("pathfusetest", min_interval_s=30.0, command=script)
    for e in (_eg("match", observed="relay_backbone"), _eg("mismatch"),
              _eg("match", observed="relay_backbone")):
        for page in d.observe(obs(egress=e)):
            n.notify(page)
    n.start()               # one batch: the first page goes out and the other two are held,
    n.stop()                # until stop() sends them as one summary
    assert _handed(log) == [RESTORED, f"{RESTORED} (×2 in 30s)"]
    assert not path.exists()


def test_egress_a_summary_that_ends_in_a_fallback_writes_the_record(tmp_path):
    # The other order, held [restore, fallback]. The fallback page that opened the window
    # is refused, so only the summary can have written the record.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    script, log = _spool_notify(tmp_path, refuse=FALLBACK)
    n = notify.Notifier("pathfusetest", min_interval_s=30.0, command=script)
    for e in (_eg("mismatch"), _eg("match", observed="relay_backbone"), _eg("mismatch")):
        for page in d.observe(obs(egress=e)):
            n.notify(page)
    n.start()
    n.stop()
    assert _handed(log) == [FALLBACK, f"{FALLBACK} (×2 in 30s)"]
    assert json.loads(path.read_text())["selected"] == "relay_backbone"


@pytest.mark.parametrize("page", ["fallback", "restore"])
def test_egress_an_on_sent_waits_for_a_transition_in_progress(tmp_path, page):
    # on_sent runs on the Notifier's thread and the transitions on the controller's. The
    # lock keeps one from landing in the middle of the other: an on_sent that had checked
    # its generation just before a mode change could otherwise write the record back
    # after the change removed it.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    [late] = d.observe(obs(egress=_eg("mismatch")))
    if page == "restore":
        late.on_sent()
        [late] = d.observe(obs(egress=_eg("match", observed="relay_backbone")))
    before = path.exists()
    with d._egress_lock:                       # a transition is in progress
        t = threading.Thread(target=late.on_sent, daemon=True)
        t.start()
        t.join(0.2)
        assert t.is_alive() and path.exists() == before
    t.join(5)
    assert not t.is_alive() and path.exists() != before


@pytest.mark.parametrize("step, want", [
    pytest.param(_eg("mismatch"), [FALLBACK], id="fallback"),
    pytest.param(_eg("match", observed="relay_backbone"), [RESTORED], id="restore"),
    pytest.param(_eg_checking("relay_vpn"), [], id="mode-change"),
    pytest.param(_eg("skipped", selected="local_direct", observed=None, ip=None), [],
                 id="skipped"),
])
def test_egress_a_transition_waits_for_an_on_sent_in_progress(tmp_path, step, want):
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    if step["status"] != "mismatch":
        assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]   # an alert stands
    got: list = []
    with d._egress_lock:                       # an on_sent is in progress
        t = threading.Thread(target=lambda: got.extend(d.observe(obs(egress=step))),
                             daemon=True)
        t.start()
        t.join(0.2)
        assert t.is_alive() and got == []
    t.join(5)
    assert not t.is_alive() and _titles(got) == want


# -- the egress alert record across a power loss ----------------------------------------
#
# A reboot is one of the restarts the record is for, and the box can lose power
# abruptly. On ext4 a new file renamed into place without an fsync can come back empty
# or missing after a power loss, and the standing fallback pages again. A removal the
# disk never saw can bring an ended alert's record back.


def _sync_recorder(monkeypatch, events):
    real_fsync = os.fsync

    def fsync(fd):
        st = os.fstat(fd)
        events.append(("fsync", (st.st_dev, st.st_ino)))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "fdatasync", fsync, raising=False)   # either syncs the data


def _file_id(p):
    st = os.stat(p)
    return st.st_dev, st.st_ino


def test_egress_alert_record_is_made_durable_around_its_rename(tmp_path, monkeypatch):
    # The record's data reaches the disk before the rename that publishes it, and the
    # rename itself after it (an fsync of the directory).
    path = tmp_path / "egress_alert.json"
    events: list = []
    _sync_recorder(monkeypatch, events)
    real_replace = os.replace

    def replace(src, dst, **kw):
        events.append(("replace", os.fspath(dst)))
        return real_replace(src, dst, **kw)

    monkeypatch.setattr(os, "replace", replace)
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    at = events.index(("replace", str(path)))
    assert ("fsync", _file_id(path)) in events[:at], events           # the data, before
    assert ("fsync", _file_id(tmp_path)) in events[at + 1:], events   # the rename, after


@pytest.mark.parametrize("failing", ["the-record", "the-directory"])
def test_egress_alert_a_failed_fsync_is_logged_and_costs_no_page(
        tmp_path, caplog, monkeypatch, failing):
    # The record only spares a repeated page. A disk that will not sync must not cost
    # the page itself or stop the tick, and leaves no temp file behind.
    path = tmp_path / "egress_alert.json"
    real_fsync = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) == (failing == "the-directory"):
            raise OSError(errno.EIO, "Input/output error")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "fdatasync", fsync, raising=False)
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    assert len(_warnings(caplog)) == 1                  # the sync that failed
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
    assert _sent(d.observe(obs(egress=_eg("match", observed="relay_backbone")))) == [RESTORED]


@pytest.mark.parametrize("ending, want", [
    pytest.param(_eg("match", observed="relay_backbone"), [RESTORED], id="restore"),
    pytest.param(_eg_checking("relay_vpn"), [], id="mode-change"),
])
def test_egress_alert_record_removal_is_made_durable(tmp_path, monkeypatch, ending, want):
    # A removal the disk never saw brings an ended alert's record back after a power
    # loss. The restart then takes the ended fallback for standing, so a new fallback on
    # that mode stays silent.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    events: list = []
    _sync_recorder(monkeypatch, events)
    real_remove = os.remove

    def remove(p, *a, **kw):
        events.append(("remove", os.fspath(p)))
        return real_remove(p, *a, **kw)

    monkeypatch.setattr(os, "remove", remove)
    monkeypatch.setattr(os, "unlink", remove)
    parent = _file_id(tmp_path)
    assert _sent(d.observe(obs(egress=ending))) == want
    assert not path.exists()
    at = events.index(("remove", str(path)))
    assert ("fsync", parent) in events[at + 1:], events   # the removal, after it
