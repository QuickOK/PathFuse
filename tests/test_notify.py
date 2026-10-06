import builtins
import errno
import gc
import io
import logging
import threading
from pathlib import Path
from typing import Any, Optional

import pytest
import notify


@pytest.fixture(autouse=True)
def detectors_closed(monkeypatch):
    """Closes each EventDetector a test makes once the test ends. A detector with a
    record path runs a record keeper thread from its first observation on, and one left
    running could log into the next test."""
    made: list = []
    real_init = notify.EventDetector.__init__

    def init(self, *a, **kw):
        real_init(self, *a, **kw)
        made.append(self)

    monkeypatch.setattr(notify.EventDetector, "__init__", init)
    yield
    for d in made:
        d.close(timeout=2.0)


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


def test_a_kinds_events_go_out_in_the_order_they_came():
    # An event whose kind has events held joins them, even once the window has run out
    # and flush_due() has not yet released them: the worker admits a batch before it
    # flushes. So a kind's events never overtake each other, nor do the on_sents they
    # carry. The egress alert record relies on that.
    clk = FakeClock()
    rl = notify.RateLimiter(30.0, clock=clk)
    assert rl.admit(ev(title="first")) is not None
    clk.advance(5)
    assert rl.admit(ev(title="second")) is None
    clk.advance(30)                           # the window has run out, nothing released yet
    assert rl.admit(ev(title="third")) is None
    [summary] = rl.flush_due()
    assert "third" in summary.title and "×2" in summary.title


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


def _spool_notify(tmp_path, name="spool-notify", rc=0, refuse=None, exit_after_s=0.0):
    """A spool-notify stand-in that logs the title it is handed, lingers `exit_after_s`
    seconds, then exits rc, or 1 when the title is `refuse`."""
    log = tmp_path / f"{name}.log"
    script = tmp_path / name
    refusal = f'[ "$1" = "{refuse}" ] && exit 1\n' if refuse is not None else ""
    linger = f"sleep {exit_after_s}\n" if exit_after_s else ""
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{log}"\n{refusal}{linger}exit {rc}\n',
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


def test_notifier_takes_a_new_page_while_an_on_sent_runs(tmp_path):
    # on_sent runs on the worker, outside the lock notify() takes, so the control loop
    # never waits for a record's write and fsyncs. Here an on_sent is stuck (a disk that
    # will not sync), and notify() must still return at once.
    script, _log = _spool_notify(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def stuck():
        entered.set()
        release.wait(10)

    n = notify.Notifier("pathfusetest", command=script)
    n.start()
    try:
        n.notify(notify.Event("egress", "first", "m", "high", on_sent=stuck))
        assert entered.wait(5)
        start = time.monotonic()
        n.notify(notify.Event("relay", "second", "m", "high"))
        took = time.monotonic() - start
    finally:
        release.set()
        n.stop()
    assert took < 2.0, f"notify() waited {took:.2f} s for an on_sent"


def test_notifier_stop_says_whether_its_worker_ended(tmp_path, monkeypatch):
    # run_controller closes the egress record keeper once stop() returns, and a page
    # spool-notify takes after that is not recorded. So stop() says whether the worker
    # ended in time: True for one with nothing to send, which ends at once, or one never
    # started; False while a send outlives the timeout. Here spool-notify hangs, and the
    # Notifier's own timeout on it (shortened) is what ends the send.
    monkeypatch.setattr(notify.Notifier, "SUBPROCESS_TIMEOUT_S", 1.0)
    sending = tmp_path / "sending"
    script = tmp_path / "hung-spool-notify"
    script.write_text(f'#!/bin/sh\ntouch "{sending}"\nexec sleep 30\n')
    script.chmod(0o755)
    assert notify.Notifier("pathfusetest", command=str(script)).stop() is True
    idle = notify.Notifier("pathfusetest", command=str(script))
    idle.start()
    assert idle.stop(timeout=5.0) is True
    assert idle._thread is not None and not idle._thread.is_alive()
    n = notify.Notifier("pathfusetest", command=str(script))
    n.start()
    n.notify(ev(kind="started", title="start", message="hello"))
    assert wait_for(sending.exists)            # the worker is inside the send
    start = time.monotonic()
    assert n.stop(timeout=0.2) is False
    assert 0.15 <= time.monotonic() - start < 1.0   # it waited its timeout, no longer
    assert n._thread is not None and n._thread.is_alive()
    n._thread.join(5)                          # the send times out, and the worker ends
    assert n.stop() is True


@pytest.mark.parametrize("exit_after_s", [0.0, 1.5],
                         ids=["prompt-stand-in", "stand-in-lingers-1.5s-after-logging"])
def test_an_idle_workers_stop_returns_at_once(tmp_path, exit_after_s):
    # run_controller gives stop() 35 s, on the ruling that an idle worker ends at once:
    # one that never had a page, and one whose last page is long sent. Neither may make
    # a normal shutdown wait. The second half starts its clock only once the page's
    # on_sent has run: the worker runs it after spool-notify exited 0, so the send is
    # over and the worker idles. The stand-in's log line alone does not say that, since
    # it is written before the stand-in exits, which the lingering stand-in shows.
    script, log = _spool_notify(tmp_path, exit_after_s=exit_after_s)
    never_paged = notify.Notifier("pathfusetest", min_interval_s=0, command=script)
    never_paged.start()
    start = time.monotonic()
    assert never_paged.stop(timeout=5.0) is True
    took = time.monotonic() - start
    assert took < 1.0, f"an idle worker took {took:.2f} s to end"

    n = notify.Notifier("pathfusetest", min_interval_s=0, command=script)
    n.start()
    sent = threading.Event()
    n.notify(notify.Event("started", "start", "hello", "high", on_sent=sent.set))
    assert sent.wait(5.0), "spool-notify did not take the page"   # the send is over; the worker idles
    assert _handed(log) == ["start"]
    start = time.monotonic()
    assert n.stop(timeout=5.0) is True
    took = time.monotonic() - start
    assert took < 1.0, f"an idle worker took {took:.2f} s to end after its last page"
    assert n._thread is not None and not n._thread.is_alive()


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


def test_egress_a_mismatch_at_startup_is_unannounced_and_pages_on_the_next_tick():
    # CodeRabbit on PR #24. Unlike a WAN already down at startup: a fallback the seed
    # finds in place, with no record saying its page went out, is one the operator
    # never heard of (it began while the controller was down, or its record was lost).
    # So the next tick pages it, once, and the recovery from it is a restore.
    d = notify.EventDetector()
    assert d.observe(obs(egress=_eg("mismatch"))) == []          # seed: silent, nothing stands
    evs = d.observe(obs(egress=_eg("mismatch")))                 # the next tick pages it
    assert len(evs) == 1 and evs[0].kind == "egress" and evs[0].priority == "high"
    assert d.observe(obs(egress=_eg("mismatch"))) == []          # once
    evs = d.observe(obs(egress=_eg("match", observed="relay_backbone")))
    assert len(evs) == 1 and evs[0].kind == "egress" and "restored" in evs[0].title.lower()


@pytest.mark.parametrize("first", [
    pytest.param(_eg("checking"), id="checking"),
    pytest.param(_eg("pending"), id="pending"),
    pytest.param(_eg("error"), id="error"),
    pytest.param(_eg("match", observed="relay_backbone"), id="match"),
    pytest.param(_eg("skipped", selected="local_direct"), id="skipped"),
    pytest.param(_eg("mismatch"), id="mismatch"),
])
def test_egress_nothing_at_startup_counts_as_announced(first):
    # Whatever the seed finds leaves the page armed, a confirmed mismatch included:
    # with no record, nothing was announced, and seeding an alert as standing would
    # swallow the first real fallback, or the one found.
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
# removes the record when the alert ends. Its record keeper does that file I/O on a
# thread of its own, so a test waits for it (drain()) before it looks at the file. A
# restart is a second detector on the same path, made once the first is closed (as
# run_controller closes it at shutdown), fed what a fresh observer reports: `checking`
# until its first check (its settle alone outlasts many ticks), then its verdicts.

FALLBACK, RESTORED = "🧭 Egress fallback", "🧭 Egress restored"
ANNOUNCED_AT = 1_780_000_000.0   # a wall-clock epoch


def _titles(evs):
    return [e.title for e in evs]


def _write_record(path, selected):
    path.write_text(json.dumps({"selected": selected, "announced_at": ANNOUNCED_AT}))


def _keeper_thread(d):
    """The thread that does `d`'s record I/O. It starts at d's first observation."""
    keeper = d._keeper
    assert keeper is not None and keeper._thread is not None
    return keeper._thread


def _warnings(caplog, *detectors):
    # The detector runs on the test's own thread and each of `detectors` does its record
    # I/O on its keeper's thread; a thread another test left running must not change
    # the count.
    threads = {threading.get_ident()} | {_keeper_thread(d).ident for d in detectors}
    return [r for r in caplog.records if r.levelno == logging.WARNING and r.thread in threads]


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
    assert before.close()
    assert path.exists()
    after = notify.EventDetector(egress_alert_path=str(path))   # the restart
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []   # its seed
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []   # still settling
    assert after.observe(obs(egress=_eg("pending"))) == []
    assert after.observe(obs(egress=_eg("mismatch"))) == []     # paged before the restart
    assert after.observe(obs(egress=_eg("mismatch"))) == []
    evs = after.observe(obs(egress=_eg("match", observed="relay_backbone")))
    assert _sent(evs) == [RESTORED]
    assert after.drain()
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
        assert d.close()
        d = notify.EventDetector(egress_alert_path=str(path))
        for e in (_eg_checking("relay_backbone"), _eg_checking("relay_backbone"),
                  _eg("pending"), _eg("mismatch"), _eg("mismatch")):
            pages += _sent(d.observe(obs(egress=e)))
        assert d.drain()
        assert path.exists(), f"the fallback still stands after {restart}, so must its record"
    pages += _sent(d.observe(obs(egress=_eg("match", observed="relay_backbone"))))
    assert d.close()
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
    assert after.drain()
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
    assert after.drain()
    assert _warnings(caplog, after) == []     # no record is the normal case, not a fault


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
    assert after.drain()
    assert not path.exists()
    assert after.observe(obs(egress=_eg("match", observed="relay_backbone"))) == []


def test_egress_a_record_for_another_mode_is_dropped_at_the_restart(tmp_path):
    # The selected mode changed while the controller was down. A change ends an alert
    # silently while it runs, and does so across a restart too.
    path = tmp_path / "egress_alert.json"
    _write_record(path, "relay_backbone")
    after = notify.EventDetector(egress_alert_path=str(path))
    assert after.observe(obs(egress=_eg_checking("relay_vpn"))) == []
    assert after.drain()
    assert not path.exists()
    assert after.observe(obs(egress=_eg("match", selected="relay_vpn", observed="relay_vpn"))) == []
    assert _titles(after.observe(obs(egress=_eg("mismatch", selected="relay_vpn")))) == [FALLBACK]


def test_egress_a_skipped_check_at_the_restart_ends_the_saved_alert(tmp_path):
    # Every `skipped` ends the saved record, the seed's as well: the check is not
    # running, so this run cannot follow the alert. The record goes unread, and a match
    # after it is no restore.
    path = tmp_path / "egress_alert.json"
    _write_record(path, "relay_backbone")
    after = notify.EventDetector(egress_alert_path=str(path))
    assert after.observe(obs(egress=_eg("skipped", observed=None, ip=None))) == []
    assert after.drain()
    assert not path.exists()
    assert after.observe(obs(egress=_eg("match", observed="relay_backbone"))) == []


@pytest.mark.parametrize("prior", [None, "relay_backbone"], ids=["no-record", "record-for-another-mode"])
def test_egress_a_fallback_found_at_startup_is_paged_and_recorded_like_any_other(tmp_path, prior):
    # Nothing says the operator heard of it: there is no record, or the record is
    # another mode's, which the seed drops. So the next tick pages it, its page's
    # on_sent records it, and a restart then keeps it announced.
    path = tmp_path / "egress_alert.json"
    if prior is not None:
        _write_record(path, prior)
    first = notify.EventDetector(egress_alert_path=str(path))
    assert first.observe(obs(egress=_eg("mismatch", selected="relay_vpn"))) == []   # unannounced
    assert first.drain()
    assert not path.exists()                                    # and no record for it, yet
    assert _sent(first.observe(obs(egress=_eg("mismatch", selected="relay_vpn")))) == [FALLBACK]
    assert first.close()
    assert json.loads(path.read_text())["selected"] == "relay_vpn"
    again = notify.EventDetector(egress_alert_path=str(path))   # so a restart keeps it announced
    assert again.observe(obs(egress=_eg_checking("relay_vpn"))) == []
    assert again.observe(obs(egress=_eg("mismatch", selected="relay_vpn"))) == []
    evs = again.observe(obs(egress=_eg("match", selected="relay_vpn", observed="relay_vpn")))
    assert _titles(evs) == [RESTORED]


def test_egress_a_mismatch_at_the_seed_with_no_record_pages_and_records_only_on_sent(tmp_path):
    # CodeRabbit on PR #24: the seed counted a confirmed mismatch with no record for it
    # as announced, and wrote the record, though no page had gone out; a restart then
    # adopted that record and silenced a fallback the operator never heard of. With no
    # record it is unannounced, whether it began while the controller was down or its
    # record was lost: the seed leaves nothing standing and writes nothing, the next
    # tick pages it, and the record follows that page's on_sent, like any other page's.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    assert d.observe(obs(egress=_eg("mismatch"))) == []          # the seed: no page
    assert d.drain()
    assert not path.exists()                                     # no record
    assert d._egress_alert_mode is None                          # nothing standing
    [page] = d.observe(obs(egress=_eg("mismatch")))              # the next tick pages it
    assert page.title == FALLBACK
    assert d.drain()
    assert not path.exists()                                     # not until spool-notify takes it
    page.on_sent()
    assert d.drain()
    assert json.loads(path.read_text())["selected"] == "relay_backbone"
    assert d.observe(obs(egress=_eg("mismatch"))) == []          # paged once
    assert d.close()
    after = notify.EventDetector(egress_alert_path=str(path))    # a restart adopts it
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert after.observe(obs(egress=_eg("mismatch"))) == []      # confirmed: stays silent
    assert after.close()


def test_egress_a_mode_change_ends_the_alert_and_its_record(tmp_path):
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    assert d.drain()
    assert path.exists()
    assert d.observe(obs(egress=_eg_checking("relay_vpn"))) == []
    assert d.drain()
    assert not path.exists()
    # The alert was about relay Backbone, so relay-VPN holding is no recovery,
    assert d.observe(obs(egress=_eg("match", selected="relay_vpn", observed="relay_vpn"))) == []
    # and a fallback on relay-VPN is news.
    assert _sent(d.observe(obs(egress=_eg("mismatch", selected="relay_vpn")))) == [FALLBACK]
    assert d.drain()
    assert json.loads(path.read_text())["selected"] == "relay_vpn"


def test_egress_an_alert_on_a_mode_other_than_the_seeds_pages_once_and_restores(tmp_path):
    # A change of the selected mode is a change from the last tick's mode, not from the
    # mode the run started on. Compared against that one, every tick after a change
    # would end the alert afresh: each mismatch on the new mode would page a fresh
    # fallback, and its match would find no alert standing and page no restore. After
    # a change, the new mode's fallback stands like any other: a run of mismatches
    # pages once, and the match pages the restore and ends the record.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    assert d.observe(obs(egress=_eg_checking("relay_backbone"))) == []   # the seed
    assert d.observe(obs(egress=_eg_checking("relay_vpn"))) == []        # the mode changes
    pages: list = []
    for _ in range(3):
        pages += _sent(d.observe(obs(egress=_eg("mismatch", selected="relay_vpn"))))
    assert pages == [FALLBACK], "a standing fallback on the new mode paged again"
    assert d.drain()
    assert json.loads(path.read_text())["selected"] == "relay_vpn"
    pages += _sent(d.observe(obs(egress=_eg("match", selected="relay_vpn",
                                            observed="relay_vpn"))))
    assert pages == [FALLBACK, RESTORED], "the new mode's restore never paged"
    assert d.drain()
    assert not path.exists()


@pytest.mark.parametrize("selected", ["local_direct", "relay_backbone"])
def test_egress_skipped_ends_the_alert_and_its_record(tmp_path, selected):
    # The observer skips its check while local_direct is selected. `skipped` ends an
    # alert even under the alert's own mode, so the rule does not lean on a mode change.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    assert d.drain()
    assert path.exists()
    assert d.observe(obs(egress=_eg("skipped", selected=selected, observed=None, ip=None))) == []
    assert d.drain()
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
    assert d.drain()
    path.unlink()
    assert _sent(d.observe(obs(egress=ending))) == want
    assert d.drain()
    assert _warnings(caplog, d) == []


def test_egress_a_mode_change_with_no_record_ends_it_without_a_warning(tmp_path, caplog):
    # Every change of the selected mode, and every `skipped`, ends the saved record
    # whether or not one exists, so most of them find nothing to remove. That is the
    # usual case and logs nothing.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    for e in (_eg_checking("relay_backbone"), _eg_checking("relay_vpn"),
              _eg_checking("local_direct"),
              _eg("skipped", selected="local_direct", observed=None, ip=None),
              _eg_checking("relay_backbone")):
        assert d.observe(obs(egress=e)) == []
    assert d.drain()
    assert not path.exists()
    assert _warnings(caplog, d) == []


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
    assert after.drain()
    assert len(_warnings(caplog, after)) == 1
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
    assert d.drain()
    assert len(_warnings(caplog, d)) == 1                  # the read
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    assert d.drain()
    assert len(_warnings(caplog, d)) == 2                  # the write
    assert _sent(d.observe(obs(egress=_eg("match", observed="relay_backbone")))) == [RESTORED]
    assert d.drain()
    assert len(_warnings(caplog, d)) == 3                  # the remove
    left = {"parent-is-a-file": ["state"], "path-is-a-directory": ["egress_alert.json"]}[layout]
    assert sorted(p.name for p in tmp_path.iterdir()) == left   # no temp file left behind


def test_egress_alert_path_none_does_no_file_io(monkeypatch):
    # Every file operation the detector could reach fails on this thread, and is noted.
    # Each page is handed over too, so its on_sent runs here as well. Nor is there a
    # record keeper, whose thread would do the I/O elsewhere.
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
    for e in (_eg("mismatch"),                                   # the seed: unannounced, silent
              _eg("mismatch"),                                   # a fallback
              _eg("match", observed="relay_backbone"),           # restored
              _eg("mismatch"),                                   # a fallback
              _eg_checking("relay_vpn"),                         # a mode change ends it
              _eg("mismatch", selected="relay_vpn"),             # a fallback
              _eg("skipped", selected="local_direct", observed=None, ip=None)):
        pages += _sent(d.observe(obs(egress=e)))
    assert pages == [FALLBACK, RESTORED, FALLBACK, FALLBACK]
    assert attempted == []
    assert d._keeper is None


def test_notify_cfg_keeps_its_record_in_the_state_directory_by_default():
    # The detector alone keeps no record by default; the controller's config does, at
    # the one fixed place in sbfd-ctl's StateDirectory. load_config passes the switch
    # explicitly, so its tests cannot see this default.
    assert notify.EGRESS_ALERT_PATH == "/var/lib/sbfd-ctl/egress_alert.json"
    assert notify.NotifyCfg(topic="t").egress_alert_record is True


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
    assert d.drain()
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
    path.mkdir()                              # any read or removal of it fails
    off = notify.EventDetector(egress_alert_path=str(path))
    assert _warnings(caplog) == []            # constructing reads nothing,
    assert off.observe(obs()) == []           # nor does a seed with the check off. That
    assert off.observe(obs()) == []           # one ends the saved alert, and quietly: the
    assert off.drain()                        # controller warns when it cannot
    assert _warnings(caplog, off) == []
    assert path.is_dir()
    on = notify.EventDetector(egress_alert_path=str(path))
    assert on.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert on.drain()
    assert len(_warnings(caplog, on)) == 1    # whereas a seed with it on reads the record


def test_egress_a_restart_with_the_check_off_ends_the_saved_alert(tmp_path):
    # Greptile P1 on PR #24: a seed with the check off left the record alone, so a later
    # restart with the check back on took it over. While the check was off the fallback
    # could end unannounced and a new one begin, or the mode leave and come back, and the
    # adopted alert then swallowed the new fallback's page.
    path = tmp_path / "egress_alert.json"
    first = notify.EventDetector(egress_alert_path=str(path))
    first.observe(obs(egress=_eg_checking("relay_backbone")))
    assert _sent(first.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    assert first.close()
    assert path.exists()
    off = notify.EventDetector(egress_alert_path=str(path))   # restarted with the check off
    assert off.observe(obs()) == []
    assert off.drain()
    assert not path.exists()
    assert off.observe(obs()) == []
    assert off.close()
    on = notify.EventDetector(egress_alert_path=str(path))    # and later with it back on
    assert on.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert on.observe(obs(egress=_eg("pending"))) == []
    assert _titles(on.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]


# -- the record follows the pages spool-notify took -----------------------------------
#
# The record says what the operator was last paged about, so it changes when
# spool-notify takes a page: a fallback page's on_sent writes it, a restore page's
# removes it. The worker hands egress pages over in the order they were made, so a page
# that goes out late still settles the record, and the pages after it settle it again.
# A page that never got that far changes nothing, so a restart sends it again. Only an
# alert that ends without a page changes the record otherwise. Where a test needs the
# worker, the detector's pages go to a real Notifier with a stand-in spool-notify; the
# others run on_sent by hand. Either way on_sent only queues the change, and the
# detector's record keeper makes it.


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
    assert d.drain()
    assert not path.exists()                  # raised, but not yet handed over
    n = notify.Notifier("pathfusetest", command=script)
    n.start()
    for e in evs:
        n.notify(e)
    n.stop()                                  # the worker sends what it holds, then ends
    assert _handed(log) == [FALLBACK]
    assert d.drain()
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
    assert before.close()
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
    assert before.close()
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
    assert d.close()
    assert json.loads(path.read_text()) == {"selected": "relay_backbone",
                                            "announced_at": ANNOUNCED_AT}
    after = notify.EventDetector(egress_alert_path=str(path))   # the restart
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert _titles(after.observe(obs(egress=_eg("match", observed="relay_backbone")))) == [RESTORED]


def test_a_mode_change_after_a_refused_restore_ends_the_saved_alert(tmp_path):
    # Found collecting the bots' round 2 on PR #24. The restore page is refused, so the
    # record keeps the fallback (by design: a restart then sends the restore again). The
    # alert has already ended in memory, and a mode change used to end the record only
    # while an alert stood. So after the mode left and came back, a restart took the
    # record over, and a NEW fallback on that mode paged nothing, though the same run
    # without the restart pages it. Every change of the selected mode now ends the
    # saved record, alert or no alert.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    [fallback] = d.observe(obs(egress=_eg("mismatch")))
    fallback.on_sent()                                         # spool-notify took it
    [_refused] = d.observe(obs(egress=_eg("match", observed="relay_backbone")))
    assert d.observe(obs(egress=_eg_checking("relay_vpn"))) == []        # the mode leaves...
    assert d.observe(obs(egress=_eg_checking("relay_backbone"))) == []   # ...and comes back
    assert d.close()
    after = notify.EventDetector(egress_alert_path=str(path))            # the restart
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert after.observe(obs(egress=_eg("pending"))) == []
    assert _titles(after.observe(obs(egress=_eg("mismatch")))) == [FALLBACK], \
        "a record the mode change should have ended swallowed the new fallback's page"


def test_a_skipped_check_after_a_refused_restore_ends_the_saved_alert(tmp_path):
    # The same with `skipped` in place of the mode change: every `skipped` ends the saved
    # record too, alert or no alert.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    [fallback] = d.observe(obs(egress=_eg("mismatch")))
    fallback.on_sent()                                         # spool-notify took it
    [_refused] = d.observe(obs(egress=_eg("match", observed="relay_backbone")))
    assert d.observe(obs(egress=_eg("skipped", observed=None, ip=None))) == []
    assert d.close()
    after = notify.EventDetector(egress_alert_path=str(path))            # the restart
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert after.observe(obs(egress=_eg("pending"))) == []
    assert _titles(after.observe(obs(egress=_eg("mismatch")))) == [FALLBACK], \
        "a record the skipped check should have ended swallowed the new fallback's page"


def test_the_same_run_without_a_restart_does_page_that_fallback(tmp_path):
    # Control: the in-run behaviour the restart should match.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    [fallback] = d.observe(obs(egress=_eg("mismatch")))
    fallback.on_sent()
    [_refused] = d.observe(obs(egress=_eg("match", observed="relay_backbone")))
    d.observe(obs(egress=_eg_checking("relay_vpn")))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    assert d.observe(obs(egress=_eg("pending"))) == []
    assert _titles(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]


@pytest.mark.parametrize("then", [
    pytest.param([], id="restored"),
    pytest.param([_eg("mismatch")], id="restored-and-fallen-back-again"),
])
def test_egress_a_fallback_page_that_goes_out_after_its_alert_moved_on_still_writes_the_record(
        tmp_path, then):
    # Pages queue in the Notifier, so a page can go out after its alert has moved on. The
    # worker hands an egress page over only after every earlier one, so the record follows
    # each page as it goes out. The late fallback page writes it: that page is now the
    # operator's latest news. Each newer page then settles the record again in turn.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    [late] = d.observe(obs(egress=_eg("mismatch")))
    newer = d.observe(obs(egress=_eg("match", observed="relay_backbone")))
    for e in then:                            # the same mode falls back again
        newer += d.observe(obs(egress=e))
    assert _titles(newer) == [RESTORED, FALLBACK][:1 + len(then)]
    late.on_sent()
    assert d.drain()
    assert path.exists() and json.loads(path.read_text())["selected"] == "relay_backbone"
    for page in newer:
        page.on_sent()
        assert d.drain()
        assert path.exists() == (page.title == FALLBACK), page.title


@pytest.mark.parametrize("then", [
    pytest.param([], id="fallen-back-again"),
    pytest.param([_eg("match", observed="relay_backbone")], id="fallen-back-again-and-restored"),
])
def test_egress_a_restore_page_that_goes_out_after_a_new_fallback_still_removes_the_record(
        tmp_path, then):
    # The reverse order. The late restore page removes the record although a new fallback
    # stands, since "restored" is now the operator's latest news. The new fallback's page
    # writes the record again when it goes out.
    path = tmp_path / "egress_alert.json"
    _write_record(path, "relay_backbone")      # paged before a restart
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))   # taken over
    [late] = d.observe(obs(egress=_eg("match", observed="relay_backbone")))
    newer = d.observe(obs(egress=_eg("mismatch")))
    for e in then:
        newer += d.observe(obs(egress=e))
    assert _titles(newer) == [FALLBACK, RESTORED][:1 + len(then)]
    late.on_sent()
    assert d.drain()
    assert not path.exists()
    for page in newer:
        page.on_sent()
        assert d.drain()
        assert path.exists() == (page.title == FALLBACK), page.title


def test_egress_a_restore_page_taken_after_the_next_fallback_leaves_that_fallback_to_a_restart(
        tmp_path):
    # The restore page goes out only after the next fallback was raised (a slow
    # spool-notify, or a backlog in the worker), and that fallback page is then lost
    # (refused, or it died with the process). The operator's last page says "restored",
    # and the fallback stands, so a restart must page it.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    [restore] = d.observe(obs(egress=_eg("match", observed="relay_backbone")))
    [_lost] = d.observe(obs(egress=_eg("mismatch")))
    restore.on_sent()                          # spool-notify takes the restore page
    assert d.close()
    after = notify.EventDetector(egress_alert_path=str(path))   # the restart
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert after.observe(obs(egress=_eg("pending"))) == []
    assert _titles(after.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]


def test_egress_a_fallback_page_taken_after_its_restore_leaves_the_restore_to_a_restart(tmp_path):
    # The reverse order: the fallback page goes out only after the restore was raised,
    # and the restore page is then lost. The operator's last page says "fallback", and
    # the exit matches again, so a restart must send the restore.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    [fallback] = d.observe(obs(egress=_eg("mismatch")))
    [_lost] = d.observe(obs(egress=_eg("match", observed="relay_backbone")))
    fallback.on_sent()                         # spool-notify takes the fallback page
    assert d.close()
    after = notify.EventDetector(egress_alert_path=str(path))   # the restart
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert _titles(after.observe(obs(egress=_eg("match", observed="relay_backbone")))) == [RESTORED]


@pytest.mark.parametrize("race", ["restore-taken-late", "fallback-taken-late"])
def test_egress_pages_queued_in_the_worker_leave_the_record_at_the_last_one_taken(tmp_path, race):
    # The two races above, through a real Notifier. The detector makes both pages before
    # the worker hands either over, and spool-notify takes the first and refuses the
    # second. The record must say what the first page said, so a restart in the state the
    # second described sends the second again.
    path = tmp_path / "egress_alert.json"
    if race == "restore-taken-late":
        _write_record(path, "relay_backbone")  # its fallback was paged before a restart
        first, second = _eg("match", observed="relay_backbone"), _eg("mismatch")
    else:
        first, second = _eg("mismatch"), _eg("match", observed="relay_backbone")
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    pages = d.observe(obs(egress=first)) + d.observe(obs(egress=second))
    script, log = _spool_notify(tmp_path, refuse=pages[1].title)
    n = notify.Notifier("pathfusetest", min_interval_s=0, command=script)
    for page in pages:
        n.notify(page)
    n.start()                                 # one batch, handed over in order
    n.stop()
    assert d.close()
    assert _handed(log) == _titles(pages)     # the first taken, the second refused
    after = notify.EventDetector(egress_alert_path=str(path))   # the restart
    assert after.observe(obs(egress=_eg_checking("relay_backbone"))) == []
    assert _titles(after.observe(obs(egress=second))) == _titles(pages[1:])


def test_egress_a_fallback_page_made_before_a_silent_end_writes_no_record(tmp_path):
    # An alert that ends without a page (here a mode change) removes the record at once,
    # with no page to confirm the removal. So a page made before the end changes the
    # record no more when it goes out. This one would bring the ended alert's record
    # back, and a restart under that mode would take a new fallback for announced.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    [late] = d.observe(obs(egress=_eg("mismatch")))
    assert d.observe(obs(egress=_eg_checking("relay_vpn"))) == []          # the silent end
    late.on_sent()
    assert d.drain()
    assert not path.exists()


def test_egress_a_restore_page_made_before_a_silent_end_removes_no_record(tmp_path):
    # The same rule for a restore page: it must not remove the record of a fallback paged
    # after the end. The rule does not lean on the order the pages go out in, so here the
    # restore page goes out last.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    [late] = d.observe(obs(egress=_eg("match", observed="relay_backbone")))
    assert _titles(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]   # it falls back again
    assert d.observe(obs(egress=_eg_checking("relay_vpn"))) == []          # the silent end
    assert _sent(d.observe(obs(egress=_eg("mismatch", selected="relay_vpn")))) == [FALLBACK]
    late.on_sent()
    assert d.drain()
    assert path.exists() and json.loads(path.read_text())["selected"] == "relay_vpn"


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
    assert d.drain()
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
    assert d.drain()
    assert json.loads(path.read_text())["selected"] == "relay_backbone"


@pytest.mark.parametrize("rc, kept", [pytest.param(0, False, id="taken"),
                                      pytest.param(1, True, id="refused")])
def test_egress_a_restore_page_removes_the_record_once_spool_notify_takes_it(
        tmp_path, rc, kept):
    # The record goes with the restore page, through the Notifier and the record keeper:
    # a restore page spool-notify takes removes it, and one it refuses leaves it, so a
    # restart sends the restore again.
    path = tmp_path / "egress_alert.json"
    _write_record(path, "relay_backbone")      # its fallback was paged before a restart
    script, log = _spool_notify(tmp_path, rc=rc)
    d = notify.EventDetector(egress_alert_path=str(path))
    assert d.observe(obs(egress=_eg_checking("relay_backbone"))) == []   # taken over
    n = notify.Notifier("pathfusetest", command=script)
    n.start()
    for e in d.observe(obs(egress=_eg("match", observed="relay_backbone"))):
        n.notify(e)
    n.stop()
    assert _handed(log) == [RESTORED]
    assert d.drain()
    assert path.exists() == kept


# -- the record keeper: the disk never holds up the control loop ---------------------
#
# Notifications must never affect failover. All of the record's file I/O runs on the
# detector's record keeper thread, one operation at a time in the order they were
# queued. After the seed's one read, observe() only queues, and so does a page's
# on_sent on the Notifier's thread, so a disk that stalls in a sync stalls the keeper
# alone. A stalled disk here is os.fsync blocking until the test lets it go.


def _stalling_fsync(monkeypatch, directories_only=False):
    """Makes os.fsync block until `release` is set, as a flash card that will not sync
    can, or only a directory's fsync when `directories_only`. `entered` is set when a
    blocked fsync starts. Returns (entered, release)."""
    real_fsync = os.fsync
    entered, release = threading.Event(), threading.Event()

    def fsync(fd):
        if not directories_only or stat.S_ISDIR(os.fstat(fd).st_mode):
            entered.set()
            release.wait(10)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "fdatasync", fsync, raising=False)
    return entered, release


# How long observe() may take while the record's disk is stalled: the one bound the
# no-wait tests below share. A stalled disk holds observe() for seconds (a stalled fsync
# waits on its test's `release`, set only after _timed_observe's 2 s join), so any bound
# under 2 s tells a stall from scheduler noise, and 0.5 s leaves a loaded runner the room
# that 50 ms did not (CodeRabbit on PR #24).
_NO_WAIT_S = 0.5


def _timed_observe(d, egress):
    """d.observe() of `egress`, on a thread of its own standing in for the control loop:
    (the seconds the call took, its events). A call still running after 2 s counts as
    taking forever, so a stalled disk cannot hang the test. The garbage collector waits
    while the call runs, so a full collection cannot land in the measurement."""
    out: dict = {}

    def run():
        gc.disable()
        try:
            start = time.monotonic()
            out["evs"] = d.observe(obs(egress=egress))
            out["took"] = time.monotonic() - start
        finally:
            gc.enable()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(2.0)
    return out.get("took", float("inf")), out.get("evs", [])


def _record_changes(monkeypatch, path):
    """Notes, in order, each rename onto `path` ("replace") and each removal of it
    ("remove"), on any thread."""
    changes: list = []
    real_replace, real_remove = os.replace, os.remove

    def replace(src, dst, *a, **kw):
        if os.fspath(dst) == str(path):
            changes.append("replace")
        return real_replace(src, dst, *a, **kw)

    def remove(p, *a, **kw):
        if os.fspath(p) == str(path):
            changes.append("remove")
        return real_remove(p, *a, **kw)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "remove", remove)
    monkeypatch.setattr(os, "unlink", remove)
    return changes


def test_egress_observe_does_not_wait_for_a_stalled_record_disk(tmp_path, monkeypatch):
    # Greptile and CodeRabbit on PR #24: a disk stalled in a sync stalled failover. Here
    # the directory sync stalls from the first fallback page's write on, and the keeper
    # with it. A fallback found at the seed (unannounced), a fallback, a restore, a
    # fallback, a silent end and a fallback on the new mode each still return at once
    # (_NO_WAIT_S), and every page still goes out: the Notifier runs each page's on_sent
    # without waiting for the disk either. The first fallback's record is on disk from
    # then on, so a removal made on the controller's thread would stall as well. Once
    # the disk recovers, the record follows all of it, in order.
    path = tmp_path / "egress_alert.json"
    entered, release = _stalling_fsync(monkeypatch, directories_only=True)
    script, log = _spool_notify(tmp_path)
    n = notify.Notifier("pathfusetest", min_interval_s=0, command=script)
    d = notify.EventDetector(egress_alert_path=str(path))
    handed: list = []
    n.start()
    try:
        for step, egress, want in [
                ("a fallback at the seed", _eg("mismatch"), []),    # no page, so no write
                ("a fallback", _eg("mismatch"), [FALLBACK]),        # its write sticks
                ("a restore", _eg("match", observed="relay_backbone"), [RESTORED]),
                ("a fallback", _eg("mismatch"), [FALLBACK]),
                ("a silent end", _eg_checking("relay_vpn"), []),
                ("a fallback on the new mode", _eg("mismatch", selected="relay_vpn"),
                 [FALLBACK])]:
            took, evs = _timed_observe(d, egress)
            assert took < _NO_WAIT_S, f"{step}: observe() took {took:.3f} s"
            assert _titles(evs) == want, step
            for e in evs:
                n.notify(e)
            handed += want
            assert wait_for(lambda: _handed(log) == handed), step
            if handed:                         # the keeper is stuck in the first fallback's write
                assert entered.wait(5), step
        n.stop()                               # and the worker has run every on_sent
        assert n._thread is not None and not n._thread.is_alive()
        assert path.exists() and not release.is_set()   # the first fallback's record, its sync stuck
    finally:
        release.set()
    assert d.close()
    assert json.loads(path.read_text())["selected"] == "relay_vpn"


@pytest.mark.parametrize("step, want", [
    pytest.param(_eg("mismatch"), [FALLBACK], id="fallback"),
    pytest.param(_eg("match", observed="relay_backbone"), [RESTORED], id="restore"),
    pytest.param(_eg_checking("relay_vpn"), [], id="mode-change"),
    pytest.param(_eg("skipped", selected="local_direct", observed=None, ip=None), [],
                 id="skipped"),
])
def test_egress_a_transition_does_not_wait_for_a_record_write_in_progress(
        tmp_path, monkeypatch, step, want):
    # CodeRabbit and Greptile on PR #24: a transition took the lock an on_sent held
    # through its write and syncs, so a disk that stalled there stalled the control loop.
    # Here spool-notify takes a fallback page, its on_sent queues the record's write, and
    # the disk stalls in that write's sync. The controller's next transition, paged or
    # silent, still returns at once with its pages. Its own record operation waits behind
    # the stalled one, and the record catches up once the disk does.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    [fallback] = d.observe(obs(egress=_eg("mismatch")))
    if step["status"] == "mismatch":
        # A new fallback needs the alert ended first: by a restore, here one refused.
        [_refused] = d.observe(obs(egress=_eg("match", observed="relay_backbone")))
    entered, release = _stalling_fsync(monkeypatch)
    try:
        threading.Thread(target=fallback.on_sent, daemon=True).start()   # the Notifier's
        assert entered.wait(5)                 # the keeper is inside the write's sync
        took, evs = _timed_observe(d, step)
        assert took < _NO_WAIT_S, f"observe() took {took:.3f} s"
        assert _titles(evs) == want
    finally:
        release.set()
    assert d.drain()
    assert path.exists() == bool(want)         # the fallback's record, unless a silent end


def test_egress_a_restore_raised_while_the_fallback_write_is_stalled_still_settles_in_order(
        tmp_path, monkeypatch):
    # CodeRabbit on PR #24. No lock orders a transition against the record's I/O any
    # more, so pin that the record still follows the pages: a restore raised while the
    # fallback page's write is stuck in its sync, then handed over after it, leaves no
    # record once the disk recovers. The write lands first, then the removal.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    [fallback] = d.observe(obs(egress=_eg("mismatch")))
    changes = _record_changes(monkeypatch, path)
    entered, release = _stalling_fsync(monkeypatch)
    try:
        threading.Thread(target=fallback.on_sent, daemon=True).start()   # the Notifier's
        assert entered.wait(5)                 # the fallback page's write is mid-flight
        took, got = _timed_observe(d, _eg("match", observed="relay_backbone"))
        assert took < _NO_WAIT_S, f"observe() took {took:.3f} s"
        [restore] = got
        restore.on_sent()                      # spool-notify takes the restore page too
    finally:
        release.set()
    assert d.drain()
    assert changes == ["replace", "remove"]
    assert not path.exists()


def test_egress_a_silent_end_leaves_its_removal_to_the_keeper(tmp_path, monkeypatch):
    # Greptile on PR #24: with no on_sent in flight at all, a mode change while an alert
    # stood removed the record and synced its directory on the controller's own thread.
    # Now the end only queues the removal, and the stalled disk holds up the keeper alone.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    assert d.drain() and path.exists()         # the record is written, nothing in flight
    entered, release = _stalling_fsync(monkeypatch)
    try:
        took, evs = _timed_observe(d, _eg_checking("relay_vpn"))
        assert took < _NO_WAIT_S, f"observe() took {took:.3f} s"
        assert evs == []
        assert entered.wait(5)                 # the keeper is in the removal's sync
    finally:
        release.set()
    assert d.drain()
    assert not path.exists()


def _file_io_by_thread(monkeypatch, root):
    """Notes each file operation on a path under `root`, and each fsync, as (the ident of
    the thread that made it, its name)."""
    calls: list = []

    def noting(name, real, any_path=False):
        def call(*a, **kw):
            p = a[0] if a else None
            if any_path or (isinstance(p, (str, os.PathLike))
                            and str(os.fspath(p)).startswith(str(root))):
                calls.append((threading.get_ident(), name))
            return real(*a, **kw)
        return call

    for name in ("open", "replace", "rename", "remove", "unlink", "makedirs", "mkdir"):
        monkeypatch.setattr(os, name, noting(f"os.{name}", getattr(os, name)))
    monkeypatch.setattr(os, "fsync", noting("os.fsync", os.fsync, any_path=True))
    monkeypatch.setattr(builtins, "open", noting("open", builtins.open))
    monkeypatch.setattr(io, "open", noting("open", io.open))
    return calls


def test_egress_record_io_happens_on_the_keeper_thread_only(tmp_path, monkeypatch):
    # The threading rule. The seed reads the record once, on the controller's thread.
    # After that the controller's thread does no record I/O at all, nor does the
    # Notifier's, which only queues each page's change. Every write, removal and sync is
    # the record keeper's.
    path = tmp_path / "egress_alert.json"
    _write_record(path, "relay_vpn")           # another mode's: the seed ends it
    calls = _file_io_by_thread(monkeypatch, tmp_path)
    controller, notifiers = threading.get_ident(), set()

    def hand_over(evs):                        # the Notifier's worker; spool-notify takes all
        def run():
            notifiers.add(threading.get_ident())
            for e in evs:
                if e.on_sent is not None:
                    e.on_sent()
        t = threading.Thread(target=run)
        t.start()
        t.join(5)

    d = notify.EventDetector(egress_alert_path=str(path))
    assert d.observe(obs(egress=_eg("mismatch"))) == []      # the seed: ends that record
    assert [name for ident, name in calls if ident == controller] == ["open"]   # its read
    for e in (_eg("mismatch"), _eg("match", observed="relay_backbone"), _eg("mismatch"),
              _eg_checking("relay_vpn"), _eg("mismatch", selected="relay_vpn"),
              _eg("skipped", selected="local_direct", observed=None, ip=None)):
        hand_over(d.observe(obs(egress=e)))
    assert d.close()
    by = [ident for ident, _name in calls]
    assert by.count(controller) == 1           # the seed's read, and nothing since
    assert not notifiers & set(by)
    assert _keeper_thread(d).ident in by


@pytest.mark.parametrize("order", ["queued-before-the-end", "queued-after-the-end"])
def test_egress_a_late_fallback_page_leaves_no_record_after_a_silent_end(
        tmp_path, monkeypatch, order):
    # A fallback page made before a silent end goes out around the end, while the disk is
    # slow. Its write reaches the keeper's queue before the end's removal, and the queue
    # runs them in that order, so the removal comes last; or after it, and the end had
    # moved the generation on before it queued anything, so by the write's turn the page
    # is out of date and the write does nothing. Either way the ended alert leaves no
    # record, and the one on disk from before is gone.
    path = tmp_path / "egress_alert.json"
    _write_record(path, "relay_backbone")      # a fallback paged before a restart
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))   # taken over
    [_refused] = d.observe(obs(egress=_eg("match", observed="relay_backbone")))
    [late] = d.observe(obs(egress=_eg("mismatch")))          # it falls back again
    entered, release = _stalling_fsync(monkeypatch)
    try:
        if order == "queued-before-the-end":
            late.on_sent()
            assert entered.wait(5)             # its write is in the keeper's hands
        _took, evs = _timed_observe(d, _eg_checking("relay_vpn"))   # the silent end
        assert evs == []
        if order == "queued-after-the-end":
            assert entered.wait(5)             # the end's removal is in the keeper's hands
            late.on_sent()
    finally:
        release.set()
    assert d.drain()
    assert not path.exists()


def test_egress_a_silent_end_moves_the_generation_on_before_it_queues_its_removal(
        tmp_path, monkeypatch):
    # The end's removal and a late page's write can reach the queue back to back, the
    # write second, and the keeper can run both before the controller's thread runs
    # another line. So the end must have moved the generation on before it queued
    # anything. Else the write still finds its page's generation and records the alert
    # that has just ended, and a restart under its mode counts a new fallback as paged.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    [late] = d.observe(obs(egress=_eg("mismatch")))
    keeper = d._keeper
    assert keeper is not None
    real_remove = keeper.remove

    def remove(*a, **kw):
        # The end's removal is queued, spool-notify takes the late page at once, and the
        # keeper runs both, all before the end runs another line.
        real_remove(*a, **kw)
        notifier = threading.Thread(target=late.on_sent)
        notifier.start()
        notifier.join(5)
        assert keeper.drain(5)

    monkeypatch.setattr(keeper, "remove", remove)
    assert d.observe(obs(egress=_eg_checking("relay_vpn"))) == []      # the silent end
    assert d.drain()
    assert not path.exists()


def test_egress_the_record_keeper_outlives_an_operation_that_raises(
        tmp_path, caplog, monkeypatch):
    # Any failure in a record operation is a warning, not only an OSError, and the
    # keeper carries on with the next operation: its thread never dies.
    path = tmp_path / "egress_alert.json"
    real_makedirs, calls = os.makedirs, []

    def makedirs(*a, **kw):
        calls.append(a)
        if len(calls) == 1:
            raise RuntimeError("the first write breaks")
        return real_makedirs(*a, **kw)

    monkeypatch.setattr(os, "makedirs", makedirs)
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    assert d.drain()
    [warning] = _warnings(caplog, d)
    assert "the first write breaks" in warning.getMessage()
    assert not path.exists()
    assert _sent(d.observe(obs(egress=_eg("match", observed="relay_backbone")))) == [RESTORED]
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    assert d.drain()
    assert json.loads(path.read_text())["selected"] == "relay_backbone"
    assert len(_warnings(caplog, d)) == 1


def test_egress_close_runs_every_queued_record_operation_before_it_returns(
        tmp_path, monkeypatch):
    # run_controller closes the detector at shutdown, after the Notifier's last pages.
    # Their record operations may still be queued behind a slow disk. close() runs them
    # all before it returns, then ends the keeper's thread, so the restart that follows
    # finds the record the last page left.
    path = tmp_path / "egress_alert.json"
    _write_record(path, "relay_backbone")      # a fallback paged before a restart
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))   # taken over
    real_fsync = os.fsync

    def slow_fsync(fd):
        time.sleep(0.05)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", slow_fsync)
    for e in (_eg("match", observed="relay_backbone"), _eg("mismatch"),
              _eg_checking("relay_vpn"), _eg("mismatch", selected="relay_vpn")):
        _sent(d.observe(obs(egress=e)))        # each page taken as soon as it is made
    assert d.close()
    assert not _keeper_thread(d).is_alive()
    assert json.loads(path.read_text())["selected"] == "relay_vpn"


def test_egress_close_gives_up_on_a_stalled_record_disk_after_its_timeout(
        tmp_path, monkeypatch):
    # A disk that never recovers must not hold up the shutdown for good: close() waits
    # for the keeper only as long as it is told to, then says it did not finish.
    path = tmp_path / "egress_alert.json"
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    entered, release = _stalling_fsync(monkeypatch)
    out: dict = {}

    def close():
        start = time.monotonic()
        out["closed"] = d.close(timeout=0.2)
        out["took"] = time.monotonic() - start

    try:
        _sent(d.observe(obs(egress=_eg("mismatch"))))
        assert entered.wait(5)                 # the keeper is stuck in the record's sync
        t = threading.Thread(target=close, daemon=True)
        t.start()
        t.join(5)
        assert out.get("closed") is False, out
        assert 0.15 <= out["took"] < 2.0, out
    finally:
        release.set()


def test_egress_the_record_keeper_is_one_daemon_thread_started_at_the_seed(tmp_path):
    # Constructing a detector starts no thread: its first observation starts exactly
    # one, the keeper's, and that one is a daemon. close() gives up on a disk that
    # never recovers (above), and run_controller then returns; a keeper that was no
    # daemon, still stuck in its sync, would hold the process at exit.
    before = set(threading.enumerate())
    d = notify.EventDetector(egress_alert_path=str(tmp_path / "egress_alert.json"))
    assert set(threading.enumerate()) - before == set()
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    started = set(threading.enumerate()) - before
    assert started == {_keeper_thread(d)}, started
    assert _keeper_thread(d).daemon, \
        "a keeper stuck in a sync must not hold the process at exit"


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
    assert d.drain()
    at = events.index(("replace", str(path)))
    assert ("fsync", _file_id(path)) in events[:at], events           # the data, before
    assert ("fsync", _file_id(tmp_path)) in events[at + 1:], events   # the rename, after


def test_egress_alert_record_data_is_in_the_file_when_it_is_synced(tmp_path, monkeypatch):
    # The record's data is flushed out of Python's buffer before its fsync. Without the
    # flush the fsync syncs an empty file, and the data reaches the file only at close,
    # unsynced: a power loss after the rename can leave an empty record.
    path = tmp_path / "egress_alert.json"
    sizes: list = []
    real_fsync = os.fsync

    def fsync(fd):
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode):
            sizes.append(st.st_size)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "fdatasync", fsync, raising=False)
    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    assert d.drain()
    assert sizes == [path.stat().st_size] and sizes[0] > 0, sizes


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
    assert d.drain()
    assert len(_warnings(caplog, d)) == 1               # the sync that failed
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []
    assert _sent(d.observe(obs(egress=_eg("match", observed="relay_backbone")))) == [RESTORED]


@pytest.mark.parametrize("op", ["write", "remove"])
def test_egress_alert_a_directory_sync_that_fails_still_closes_the_directory(
        tmp_path, monkeypatch, op):
    # The directory opened for its fsync is closed in a `finally`, so a disk that will
    # not sync a directory costs a warning each time, not a file descriptor each time.
    path = tmp_path / "egress_alert.json"
    opened: list = []
    closed: list = []
    real_open, real_close, real_fsync = os.open, os.close, os.fsync

    def open_(p, flags, *a, **kw):
        fd = real_open(p, flags, *a, **kw)
        if os.fspath(p) == str(tmp_path):
            opened.append(fd)
        return fd

    def close(fd):
        closed.append(fd)
        return real_close(fd)

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "Input/output error")
        return real_fsync(fd)

    d = notify.EventDetector(egress_alert_path=str(path))
    d.observe(obs(egress=_eg_checking("relay_backbone")))
    if op == "remove":
        assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
        assert d.drain()
        assert path.exists()
    monkeypatch.setattr(os, "open", open_)
    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "fdatasync", fsync, raising=False)
    if op == "write":
        assert _sent(d.observe(obs(egress=_eg("mismatch")))) == [FALLBACK]
    else:
        assert d.observe(obs(egress=_eg_checking("relay_vpn"))) == []   # a silent end
    assert d.drain()
    monkeypatch.undo()
    assert opened and set(opened) <= set(closed), (opened, closed)


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
    assert d.drain() and path.exists()
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
    assert d.drain()
    assert not path.exists()
    at = events.index(("remove", str(path)))
    assert ("fsync", parent) in events[at + 1:], events   # the removal, after it
