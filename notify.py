#!/usr/bin/env python3
"""ntfy notifications for sbfd-ctl, delivered via the spool-notify helper.

Three units:
  RateLimiter   -- per-event-kind coalescing (pure logic, injectable clock)
  Notifier      -- bounded buffer + daemon thread that shells out to spool-notify
  EventDetector -- edge-triggered event derivation from per-tick observations

Design notes: the control loop only ever calls Notifier.notify(), which
appends to an in-memory deque and returns; all subprocess work happens on the
worker thread. Delivery reliability (spool + redeliver when the uplink is
down) is spool-notify's job, not ours.
"""
import logging
import math
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

DEFAULT_COMMAND = "/usr/local/sbin/spool-notify"


@dataclass
class NotifyCfg:
    topic: str
    min_interval_s: float = 30.0
    command: str = DEFAULT_COMMAND
    wan_down_hold_s: float = 10.0
    fec_alerts: bool = False


@dataclass
class Event:
    kind: str          # rate-limit bucket, e.g. "wan_switch"
    title: str         # ntfy Title (emoji leads; spool-notify prefixes hostname)
    message: str       # body
    priority: str      # ntfy named priority: min|low|default|high|max


class RateLimiter:
    """Per-kind coalescing: the first event of a kind sends immediately;
    further events of the same kind within min_interval_s are held and folded
    into one summary released when the window expires. Kinds are independent,
    so e.g. a flapping WAN never delays an all-WANs-down alert."""

    def __init__(self, min_interval_s: float, clock=time.monotonic):
        self.min_interval_s = float(min_interval_s)
        self._clock = clock
        self._last_sent = {}   # kind -> clock time of last real send
        self._held = {}        # kind -> [Event, ...] awaiting summary

    def admit(self, ev: Event) -> Optional[Event]:
        now = self._clock()
        last = self._last_sent.get(ev.kind)
        in_window = last is not None and (now - last) < self.min_interval_s
        if ev.kind in self._held or in_window:
            self._held.setdefault(ev.kind, []).append(ev)
            return None
        self._last_sent[ev.kind] = now
        return ev

    def next_deadline(self) -> Optional[float]:
        if not self._held:
            return None
        return min(self._last_sent[k] for k in self._held) + self.min_interval_s

    def flush_due(self) -> list:
        now = self._clock()
        out = []
        for kind in list(self._held):
            if now - self._last_sent[kind] < self.min_interval_s:
                continue
            out.append(self._summarize(kind, now))
        return out

    def flush_all(self) -> list:
        """Summarize and release ALL held events regardless of deadline.

        For shutdown: events still inside an open coalescing window must not
        be silently discarded."""
        now = self._clock()
        return [self._summarize(kind, now) for kind in list(self._held)]

    def _summarize(self, kind: str, now: float) -> Event:
        held = self._held.pop(kind)
        last = held[-1]
        if len(held) == 1:
            summary = last
        else:
            summary = Event(
                kind=kind,
                title=f"{last.title} (×{len(held)} in "
                      f"{int(self.min_interval_s)}s)",
                message=last.message,
                priority=last.priority,
            )
        # Window restarts from the actual flush time, not the theoretical
        # deadline: a late flush extends the quiet period rather than
        # immediately re-admitting the next event of this kind.
        self._last_sent[kind] = now
        return summary


class Notifier:
    """Bounded buffer drained by a daemon thread that invokes spool-notify.

    notify() never blocks and never raises: at 50 buffered entries the oldest
    is dropped (with a warning). The worker applies the RateLimiter, then
    runs `spool-notify <title> <priority> <message>` with NOTIFY_TOPIC set.
    A nonzero exit is logged and the message dropped — spool-notify itself
    spools on delivery failure, so nonzero means something *local* is wrong,
    and notifications must never affect failover behavior."""

    BUFFER_MAX = 50
    SUBPROCESS_TIMEOUT_S = 30.0

    def __init__(self, topic: str, min_interval_s: float = 30.0,
                 command: str = DEFAULT_COMMAND, clock=time.monotonic):
        self._topic = topic
        self._command = command
        self._clock = clock
        self._limiter = RateLimiter(min_interval_s, clock=clock)
        self._buf = deque()
        self._cond = threading.Condition()
        self._stopping = False
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="notify",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        with self._cond:
            self._stopping = True
            self._cond.notify()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def notify(self, ev: Event):
        with self._cond:
            if len(self._buf) >= self.BUFFER_MAX:
                dropped = self._buf.popleft()
                logging.warning("notify buffer full; dropped oldest (%s)",
                                dropped.kind)
            self._buf.append(ev)
            self._cond.notify()

    # -- worker thread --------------------------------------------------

    def _run(self):
        while True:
            with self._cond:
                deadline = self._limiter.next_deadline()
                if not self._buf and not self._stopping:
                    timeout = (None if deadline is None
                               else max(0.0, deadline - self._clock()))
                    self._cond.wait(timeout=timeout)
                stopping = self._stopping and not self._buf
                if not stopping:
                    batch = list(self._buf)
                    self._buf.clear()
            if stopping:
                # Don't discard events still held in an open coalescing
                # window: summarize and send them before exiting.
                try:
                    for summary in self._limiter.flush_all():
                        self._send(summary)
                except Exception:
                    logging.exception("notify worker error during shutdown")
                return
            try:
                for ev in batch:
                    sendable = self._limiter.admit(ev)
                    if sendable is not None:
                        self._send(sendable)
                for summary in self._limiter.flush_due():
                    self._send(summary)
            except Exception:
                logging.exception("notify worker error (continuing)")

    def _send(self, ev: Event):
        env = dict(os.environ, NOTIFY_TOPIC=self._topic)
        try:
            res = subprocess.run(
                [self._command, ev.title, ev.priority, ev.message],
                env=env, capture_output=True,
                timeout=self.SUBPROCESS_TIMEOUT_S)
            if res.returncode != 0:
                logging.warning(
                    "spool-notify exited %d for %s: %s", res.returncode, ev.kind,
                    res.stderr.decode(errors="replace").strip())
        except (OSError, subprocess.TimeoutExpired) as e:
            logging.warning("spool-notify invocation failed for %s: %s",
                            ev.kind, e)


@dataclass
class Observation:
    """One control-loop tick's worth of state, as seen by sbfd-ctl."""
    wan_states: dict     # wan -> "UP"|"DOWN"|"UNKNOWN" (merged effective)
    wan_labels: dict     # wan -> human label
    mode: str            # effective mode AFTER env override
    env_active: bool
    env_reason: str
    fec_engaged: bool
    fec_at_max: bool
    relay_polled: bool
    relay_ok: bool
    switch: Optional[tuple]   # (from_list, to_list, reason) or None
    # Set while a WAN is being rebooted on purpose. Suppresses that WAN's
    # down/up/switch events -- never all_wans_down. A missing, malformed, or
    # expired window suppresses nothing: the failure mode of this feature must
    # be a spurious page, never a missed one.
    maintenance: Optional[dict] = None


class EventDetector:
    """Turns per-tick observations into edge-triggered Events. The first
    observation seeds comparison state silently, so a controller restart
    never replays the current status as a notification storm.

    A WAN must be continuously not-UP for wan_down_hold_s before it alerts;
    the same hold gates the all-WANs-down alert. Anything shorter is a blip
    and stays invisible — including the recovery, since alerting "✅ up" for
    an outage that was never announced would be noise on its own."""

    def __init__(self, relay_fail_threshold: int = 10,
                 wan_down_hold_s: float = 10.0, fec_alerts: bool = False,
                 clock=time.monotonic, wall_clock=time.time):
        self.relay_fail_threshold = max(1, int(relay_fail_threshold))
        self.wan_down_hold_s = max(0.0, float(wan_down_hold_s))
        self.fec_alerts = bool(fec_alerts)
        # Two clocks on purpose: durations (the hold timers) are measured on a
        # monotonic clock, which cannot jump; the maintenance window's `until`
        # is a wall-clock epoch written by another process, so it can only be
        # judged against a wall clock. Comparing an epoch against monotonic
        # seconds-since-boot would make every window look open forever.
        self._clock = clock
        self._wall_clock = wall_clock
        self._seeded = False
        self._wan_states = {}
        self._down_since = {}      # wan -> clock time it stopped being UP
        self._down_from = {}       # wan -> last UP-or-seeded state before that
        self._down_alerted = set()  # wans whose outage has been announced
        self._maint_suppressed = set()  # wans whose down we withheld (window)
        self._all_down_since = None
        self._all_down_alerted = False
        self._mode = None
        self._env_active = False
        self._fec_engaged = False
        self._fec_at_max = False
        self._relay_fails = 0
        self._relay_alerted = False

    def observe(self, obs: Observation) -> list:
        evs = []
        if self._seeded:
            evs.extend(self._wan_events(obs))
            evs.extend(self._switch_events(obs))
            evs.extend(self._mode_events(obs))
            evs.extend(self._env_events(obs))
            if self.fec_alerts:
                evs.extend(self._fec_events(obs))
            evs.extend(self._relay_events(obs))
        else:
            self._seed(obs)
        self._wan_states = dict(obs.wan_states)
        self._mode = obs.mode
        self._env_active = obs.env_active
        self._fec_engaged = obs.fec_engaged
        self._fec_at_max = obs.fec_at_max
        return evs

    def _seed(self, obs):
        # Whatever is already broken at startup is treated as announced: no
        # alert now, and none once the hold expires either. A later recovery
        # still reports, which is the useful half of the edge.
        #
        # ...unless it is broken under an open maintenance window, and that
        # exception is the whole point of consulting obs.maintenance here. A
        # WAN down under a window has NOT been announced — its down edge was
        # deliberately WITHHELD and is still PENDING. Seeding it as "already
        # announced" would turn pending into never-sent: _wan_events skips any
        # WAN in _down_alerted, so the outage would be swallowed forever, even
        # long after the window expired. It belongs in _maint_suppressed, where
        # a recovery inside the window stays silent and an outage that outlives
        # the window still pages.
        maint = self._maint_wan(obs)
        for wan, state in obs.wan_states.items():
            if state == "UP":
                continue
            if wan == maint:
                self._maint_suppressed.add(wan)
            else:
                self._down_alerted.add(wan)
        if obs.wan_states and not any(s == "UP"
                                      for s in obs.wan_states.values()):
            self._all_down_since = self._clock()
            self._all_down_alerted = True
        if obs.relay_polled and not obs.relay_ok:
            self._relay_fails = 1
        self._seeded = True

    # -- per-category edges ----------------------------------------------

    def _maint_wan(self, obs) -> Optional[str]:
        """The WAN currently under maintenance, or None. Fails open.

        `until` is an absolute wall-clock epoch (the window is written by
        another process and must survive a restart of this one), so it is
        judged against the wall clock, NOT against self._clock() — that one is
        monotonic (seconds since boot) and would leave every window looking
        open, muting a WAN's alerts indefinitely.

        `until` must also be FINITE, and must not be a bool. json.loads accepts
        bareword Infinity, and `now < inf` is true forever: a window file
        carrying one would silence that WAN's outages permanently, which is the
        single failure mode that can hide a real outage. (NaN fails the other
        way — every comparison is False, so it suppresses nothing — but it is
        no more a timestamp than Infinity is, and is rejected here too rather
        than left to work by accident.)"""
        m = obs.maintenance
        if not isinstance(m, dict):
            return None
        wan, until = m.get("wan"), m.get("until")
        if not isinstance(wan, str) or isinstance(until, bool):
            return None
        if not isinstance(until, (int, float)) or not math.isfinite(until):
            return None
        return wan if self._wall_clock() < until else None

    def _wan_events(self, obs):
        now = self._clock()
        maint = self._maint_wan(obs)
        evs = []
        for wan, state in obs.wan_states.items():
            label = obs.wan_labels.get(wan, wan)
            if wan == maint:
                # Rebooting on purpose: emit nothing. Crucially, a withheld
                # outage is NOT recorded as announced — the down edge stays
                # PENDING, so a WAN still down when the window expires is
                # reported then (hold restarted from the window's close).
                if state == "UP":
                    self._down_since.pop(wan, None)
                    self._down_from.pop(wan, None)
                    if wan in self._maint_suppressed:
                        # We withheld the down, so withhold the up too.
                        self._down_alerted.discard(wan)
                        self._maint_suppressed.discard(wan)
                        continue
                    # Otherwise this is a REAL outage that was already
                    # announced before the window opened: fall through to the
                    # normal UP path, because whoever was paged must be told
                    # it is back.
                elif wan not in self._down_alerted:
                    self._maint_suppressed.add(wan)
                    continue
                else:
                    continue    # real, already-announced outage: no repeat
            # Not suppressed (or no longer): drop any stale suppression flag.
            self._maint_suppressed.discard(wan)
            if state == "UP":
                self._down_since.pop(wan, None)
                self._down_from.pop(wan, None)
                if wan in self._down_alerted:
                    self._down_alerted.discard(wan)
                    prev = self._wan_states.get(wan, "DOWN")
                    evs.append(Event("wan_up", f"✅ {label} up",
                                     f"{wan} {prev} → {state}", "default"))
                continue
            if wan in self._down_alerted:
                continue
            if wan not in self._down_since:
                self._down_since[wan] = now
                self._down_from[wan] = self._wan_states.get(wan, "UP")
            held = now - self._down_since[wan]
            if held >= self.wan_down_hold_s:
                self._down_alerted.add(wan)
                evs.append(Event(
                    "wan_down", f"⚠️ {label} down",
                    f"{wan} {self._down_from.get(wan, 'UP')} → {state} "
                    f"(down {int(held)}s)", "high"))
        evs.extend(self._all_down_events(obs, now))
        return evs

    def _all_down_events(self, obs, now):
        if not obs.wan_states:
            return []
        if any(s == "UP" for s in obs.wan_states.values()):
            self._all_down_since = None
            self._all_down_alerted = False
            return []
        if self._all_down_alerted:
            return []
        if self._all_down_since is None:
            self._all_down_since = now
        if now - self._all_down_since < self.wan_down_hold_s:
            return []
        self._all_down_alerted = True
        detail = ", ".join(f"{w}={s}" for w, s in
                           sorted(obs.wan_states.items()))
        return [Event("all_wans_down", "🚨 All WANs down", detail, "max")]

    def _switch_events(self, obs):
        if obs.switch is None:
            return []
        frm, to, reason = obs.switch
        maint = self._maint_wan(obs)
        removed = set(frm) - set(to)
        if maint is not None and removed == {maint}:
            # The maintained WAN, and ONLY it, dropping out of the active set
            # IS the reboot, not news. Anything else still reports: a switch it
            # did not cause (the other WAN really failed, or the maintained WAN
            # rejoining), and — the case a plain `maint in frm and maint not in
            # to` got wrong — a switch that removed the maintained WAN AND
            # another WAN at the same time, which is a strictly WORSE event
            # than the one we are excusing.
            return []
        return [Event("wan_switch",
                      f"🔀 WAN switch → {','.join(to)}",
                      f"active {','.join(frm)} → {','.join(to)}\n"
                      f"reason: {reason}", "high")]

    def _mode_events(self, obs):
        if self._mode == obs.mode:
            return []
        was_full = self._mode == "full"
        is_full = obs.mode == "full"
        if not (was_full or is_full):
            return []
        cause = (f"environmental override: {obs.env_reason}"
                 if obs.env_active else "operator/policy")
        return [Event("redundancy",
                      f"🛡 Mode {self._mode} → {obs.mode}",
                      cause, "default")]

    def _env_events(self, obs):
        if obs.env_active and not self._env_active:
            return [Event("env_override",
                          "🌩 Environmental override: full redundancy",
                          obs.env_reason or "(no reason given)", "high")]
        if self._env_active and not obs.env_active:
            return [Event("env_override",
                          "🌩 Environmental override cleared",
                          "back to operator/policy mode", "high")]
        return []

    def _fec_events(self, obs):
        evs = []
        if obs.fec_engaged and not self._fec_engaged:
            evs.append(Event("fec", "📶 FEC engaged",
                             "packet loss detected; parity streams on",
                             "default"))
        elif self._fec_engaged and not obs.fec_engaged:
            evs.append(Event("fec", "📶 FEC disengaged",
                             "loss cleared; parity streams off", "default"))
        if obs.fec_at_max and not self._fec_at_max:
            evs.append(Event("fec", "📶 FEC at max level",
                             "loss beyond the top of the table", "default"))
        return evs

    def _relay_events(self, obs):
        if not obs.relay_polled:
            return []
        if obs.relay_ok:
            self._relay_fails = 0
            if self._relay_alerted:
                self._relay_alerted = False
                return [Event("relay", "🔌 Relay restored",
                              "relay /state reachable again", "high")]
            return []
        self._relay_fails += 1
        if (self._relay_fails >= self.relay_fail_threshold
                and not self._relay_alerted):
            self._relay_alerted = True
            return [Event("relay", "🔌 Relay unreachable",
                          f"{self._relay_fails} consecutive failed polls",
                          "high")]
        return []
