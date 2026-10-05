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
import functools
import json
import logging
import math
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

DEFAULT_COMMAND = "/usr/local/sbin/spool-notify"
# sbfd-ctl's StateDirectory: it survives a service restart and a reboot alike.
DEFAULT_EGRESS_ALERT_PATH = "/var/lib/sbfd-ctl/egress_alert.json"

# What the UI calls each egress mode; used in egress fallback messages.
EGRESS_LABELS = {
    "relay_vpn": "relay-VPN",
    "relay_backbone": "relay Backbone",
    "relay_direct": "relay Direct",
    "local_direct": "Local Direct",
}


def egress_label(mode) -> str:
    return EGRESS_LABELS.get(mode, mode or "unknown")


@dataclass
class NotifyCfg:
    topic: str
    min_interval_s: float = 30.0
    command: str = DEFAULT_COMMAND
    wan_down_hold_s: float = 10.0
    # A switch must hold for this long before it is announced. The satellite WAN
    # routinely drops for ~25s and fails back, and each excursion produced TWO
    # high-priority pages (away, then back) — 10 in six hours on one observed
    # afternoon. That is the noise that teaches an operator to ignore their
    # phone. Same "blip vs outage" logic wan_down_hold_s already applies to WAN
    # down events; switches never got it.
    switch_hold_s: float = 60.0
    fec_alerts: bool = False
    # Where the egress fallback the operator was paged about is recorded, so a
    # restart in the middle of it does not page it again (see EventDetector). The
    # record follows the last egress page spool-notify accepted. None keeps no record.
    # Only a run with notifications on, the actual-exit check on and a path keeps it
    # up to date. Any other run removes it at startup, at this path or else at
    # DEFAULT_EGRESS_ALERT_PATH (sbfd_ctl.end_saved_egress_alert).
    egress_alert_path: Optional[str] = DEFAULT_EGRESS_ALERT_PATH


@dataclass
class Event:
    kind: str          # rate-limit bucket, e.g. "wan_switch"
    title: str         # ntfy Title (emoji leads; spool-notify prefixes hostname)
    message: str       # body
    priority: str      # ntfy named priority: min|low|default|high|max
    # Run by the Notifier's worker once spool-notify has accepted this page (exit 0),
    # and never for a page that did not get that far. It is not part of the page:
    # equality and repr leave it out.
    on_sent: Optional[Callable[[], None]] = field(default=None, compare=False, repr=False)


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
                # The last held event is the latest state of its kind, so the
                # summary's send is its handoff. The earlier ones' never runs.
                on_sent=last.on_sent,
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
    and notifications must never affect failover behavior.

    Once spool-notify has accepted a page (exit 0), the worker runs the page's
    on_sent, if it has one. A page that fails, or never reaches spool-notify
    (dropped from a full buffer, or still buffered when the process ends; stop()
    waits only so long), never runs it. An on_sent that raises is logged, and the
    worker carries on with the next page."""

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
                        self._deliver(summary)
                except Exception:
                    logging.exception("notify worker error during shutdown")
                return
            try:
                for ev in batch:
                    sendable = self._limiter.admit(ev)
                    if sendable is not None:
                        self._deliver(sendable)
                for summary in self._limiter.flush_due():
                    self._deliver(summary)
            except Exception:
                logging.exception("notify worker error (continuing)")

    def _deliver(self, ev: Event) -> None:
        """Send `ev`, then run its on_sent if spool-notify accepted it."""
        if not self._send(ev) or ev.on_sent is None:
            return
        try:
            ev.on_sent()
        except Exception:
            # A callback must never stop the worker, nor the pages after this one.
            logging.exception("notify: on_sent for %s failed", ev.kind)

    def _send(self, ev: Event) -> bool:
        """Run spool-notify for `ev`. True only when it exits 0."""
        env = dict(os.environ, NOTIFY_TOPIC=self._topic)
        try:
            res = subprocess.run(
                [self._command, ev.title, ev.priority, ev.message],
                env=env, capture_output=True,
                timeout=self.SUBPROCESS_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            logging.warning("spool-notify invocation failed for %s: %s",
                            ev.kind, e)
            return False
        if res.returncode != 0:
            logging.warning(
                "spool-notify exited %d for %s: %s", res.returncode, ev.kind,
                res.stderr.decode(errors="replace").strip())
            return False
        return True


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
    # True while a cellular handoff duplication window is forcing full mode.
    # Suppresses the mode-transition event (never the window's own INFO logs
    # or the published duplication block) so routine fringe ping-pong doesn't
    # misattribute a window-caused switch to "operator/policy" and page on it.
    handoff_active: bool = False
    # The actual-exit check's snapshot (egress_observer.ExitTracker.snapshot()),
    # or None when the check is off. Drives the egress fallback page.
    egress: Optional[dict] = None


def _fsync_parent(path: str) -> None:
    """fsync the directory that holds `path`, so a rename into it or a removal from
    it survives a power loss."""
    fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_egress_alert_record(path: str) -> bool:
    """Remove the egress alert record at `path` (see EventDetector) durably: the
    removal is synced into its directory, or a power loss could bring an ended
    alert back. False when there was no record; any other failure raises OSError."""
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    _fsync_parent(path)
    return True


class EventDetector:
    """Turns per-tick observations into edge-triggered Events. The first
    observation seeds comparison state silently, so a controller restart
    never replays the current status as a notification storm.

    A WAN must be continuously not-UP for wan_down_hold_s before it alerts;
    the same hold gates the all-WANs-down alert. Anything shorter is a blip
    and stays invisible — including the recovery, since alerting "✅ up" for
    an outage that was never announced would be noise on its own.

    An egress fallback can outlive the process that announced it, and the seed
    cannot see one: the actual-exit check starts over at `checking`. So with
    egress_alert_path set, the record there follows the last egress page
    spool-notify accepted. A fallback page's on_sent writes it and a restore
    page's removes it, both run by the Notifier once spool-notify has the page.
    A page that never got that far (refused, or still buffered when the process
    ended) leaves the record as it was, so a restart pages that fallback again,
    or sends that restore again. An on_sent that runs after the alert has moved
    on does nothing: the newer page settles the record. An alert that ends
    without a page (a mode change, `skipped`) removes the record at once. The
    alert itself changes at once in every case, so what a run pages does not
    wait for the Notifier.

    A restart reads the record at the seed: a fallback paged before the restart
    is not paged again and its restore still is, while one that began during the
    restart pages as usual. With egress_alert_path None the detector does no
    file I/O at all, and a restart in the middle of a fallback pages it again."""

    def __init__(self, relay_fail_threshold: int = 10,
                 wan_down_hold_s: float = 10.0, fec_alerts: bool = False,
                 switch_hold_s: float = 60.0,
                 egress_alert_path: Optional[str] = None,
                 clock=time.monotonic, wall_clock=time.time):
        self.relay_fail_threshold = max(1, int(relay_fail_threshold))
        self.wan_down_hold_s = max(0.0, float(wan_down_hold_s))
        self.switch_hold_s = max(0.0, float(switch_hold_s))
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
        # Switch hysteresis. _announced_active is the active set the operator
        # currently believes in; _pending_switch is a switch waiting out the
        # hold. A switch that reverts to _announced_active before the hold
        # expires never happened as far as they are concerned.
        self._announced_active = None
        self._pending_switch = None
        self._all_down_since = None
        self._all_down_alerted = False
        self._mode = None
        self._handoff_active = False
        self._env_active = False
        self._fec_engaged = False
        self._fec_at_max = False
        self._relay_fails = 0
        self._relay_alerted = False
        # The selected mode whose fallback has been announced; None when no
        # egress alert stands. _egress_gen counts its transitions, so a page's
        # on_sent can tell whether the alert has moved on since the page was made.
        # _egress_lock covers every transition and every on_sent: the transitions
        # run on the controller's thread, the on_sents on the Notifier's.
        self._egress_alert_mode: Optional[str] = None
        self._egress_gen = 0
        self._egress_lock = threading.Lock()
        self._egress_alert_path = egress_alert_path

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
            evs.extend(self._egress_events(obs))
        else:
            self._seed(obs)
        self._wan_states = dict(obs.wan_states)
        self._mode = obs.mode
        self._handoff_active = obs.handoff_active
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
        # An egress fallback standing at a restart is invisible here: a fresh
        # observer reports `checking` until its first check. The record follows the
        # last egress page spool-notify accepted, so it tells a fallback the
        # operator was paged about from one that began during the restart.
        #   - A record for the selected mode: its fallback page went out and no
        #     restore page has. The alert stands, so a confirmed mismatch stays
        #     silent and a match sends the restore. The record stays as written.
        #   - A record for another mode: the selected mode changed while the
        #     controller was down, which ends an alert silently. Drop it.
        #   - No usable record: nothing stands, so a fallback pages as usual.
        # An exit already confirmed wrong at startup counts as announced too. No
        # page goes out for it, so it is recorded at once. A `pending` is not yet a
        # fallback, so it stays armed.
        # With the check off there is no egress, and this run cannot follow a saved
        # alert: the fallback could end, or a new one begin, unseen. So it ends the
        # alert and removes the record, unread, or a later run with the check on
        # would take a stale alert over. Quietly: the controller has already tried
        # to remove it at startup, and warned if it could not.
        if obs.egress:
            selected = obs.egress.get("selected")
            recorded = self._read_egress_alert()
            if recorded is not None:
                if recorded == selected:
                    self._adopt_egress_alert(recorded)
                else:
                    self._end_egress_alert()
            if obs.egress.get("status") == "mismatch" and self._egress_alert_mode is None:
                self._count_egress_alert_as_announced(selected)
        else:
            self._end_egress_alert(failure_level=logging.DEBUG)
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
        """Announce a switch only once the new active set has HELD.

        A satellite WAN that drops for ~25s and fails back produces two switch
        edges — away, then home — and announcing both means two high-priority
        pages for an excursion the operator can do nothing about. Holding the
        announcement means a flap that reverts within switch_hold_s is never
        mentioned at all, while a switch that sticks still pages. A genuinely
        dead WAN is not hidden by this: its own `wan_down` fires on its own,
        much shorter, hold."""
        now = self._clock()

        if obs.switch is not None:
            frm, to, reason = obs.switch
            frm_s, to_s = frozenset(frm), frozenset(to)
            if self._announced_active is None:
                # First switch we have ever seen: the operator implicitly
                # believes in the set we were in before it.
                self._announced_active = frm_s
            maint = self._maint_wan(obs)
            if maint is not None and (frm_s ^ to_s) == {maint}:
                # The maintained WAN, and ONLY it, entering or leaving the
                # active set IS the reboot, not news — both halves of it.
                # Suppressing only its departure still paged on its RETURN (the
                # fail-back), which a live run confirmed fires on every
                # maintenance night. Anything else still reports: a switch the
                # maintained WAN did not cause (the other WAN really failed),
                # or one that moved it AND another WAN at once — strictly worse
                # than the event we are excusing.
                #
                # Track the new set silently, so we never later announce a
                # "return" from a departure we never announced.
                self._announced_active = to_s
                self._pending_switch = None
                return []
            if to_s == self._announced_active:
                # It flapped straight back to what they already believe. Drop
                # the pending announcement: as far as they are concerned,
                # nothing happened.
                self._pending_switch = None
            else:
                # A new target restarts the hold — while the active set is
                # still churning, there is nothing stable worth announcing.
                self._pending_switch = (to_s, list(frm), list(to), reason, now)

        p = self._pending_switch
        if p is None:
            return []
        to_s, frm_list, to_list, reason, since = p
        if now - since < self.switch_hold_s:
            return []
        self._pending_switch = None
        self._announced_active = to_s
        return [Event("wan_switch",
                      f"🔀 WAN switch → {','.join(to_list)}",
                      f"active {','.join(frm_list)} → {','.join(to_list)}\n"
                      f"reason: {reason}", "high")]

    def _mode_events(self, obs):
        if self._mode == obs.mode:
            return []
        was_full = self._mode == "full"
        is_full = obs.mode == "full"
        if not (was_full or is_full):
            return []
        if self._handoff_active or obs.handoff_active:
            # A duplication window forcing (or just having forced) full mode
            # is not an operator/policy event -- either end of the window
            # touching this transition means the window caused it, not a
            # human, and the window's own INFO logs + duplication block are
            # the observability for it. Checking BOTH the previous and the
            # new observation's flag catches the transition landing on the
            # window's open tick, its close tick, or (rate-limit permitting)
            # both in the same tick.
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

    def _egress_events(self, obs):
        """Page once when the observed exit has disagreed with the selected mode
        for the configured number of checks, and once when it agrees again. A
        failed check is not a recovery.

        An alert belongs to the selected mode it was raised for. It ends silently
        when an observation names another mode, or reports `skipped` (the check
        does not run while local_direct is selected). A fallback on the new mode
        then pages afresh, and a match under it is not announced as a restore.
        `checking` alone ends nothing: the observer reports it at its own start
        and after a mode change, and the mode comparison already covers a change.
        That leaves its start, after a restart, where an alert the record carried
        over (see _seed) must stand until the first check confirms or clears it.

        Each page carries the on_sent that settles the record once spool-notify
        accepts it (see the class docstring); a silent end settles it at once."""
        e = obs.egress
        if not e:
            return []
        status, selected = e.get("status"), e.get("selected")
        if self._egress_alert_mode is not None and (
                selected != self._egress_alert_mode or status == "skipped"):
            self._end_egress_alert()
        if status == "mismatch" and self._egress_alert_mode is None:
            on_sent = self._raise_egress_alert(selected)
            ip = f" ({e['ip']})" if e.get("ip") else ""
            return [Event("egress", "🧭 Egress fallback",
                          f"selected {egress_label(selected)}, "
                          f"actual {egress_label(e.get('observed'))}{ip}", "high",
                          on_sent=on_sent)]
        if status == "match" and self._egress_alert_mode is not None:
            return [Event("egress", "🧭 Egress restored",
                          f"actual exit matches {egress_label(selected)} again",
                          "default", on_sent=self._restore_egress_alert())]
        return []

    # -- the egress alert record ------------------------------------------------
    #
    # The record follows the last egress page spool-notify accepted. Every
    # transition below moves the alert at once and bumps _egress_gen; a page's
    # on_sent touches the record only if no transition has happened since its
    # page was made, because a newer page then settles it. Transitions and
    # on_sents all hold _egress_lock, so an on_sent never lands between a
    # transition's change and its file I/O, or the other way round.
    #
    # The record only spares the operator a repeated page. A failure to read,
    # write, sync or remove it is logged and the tick carries on: it must never
    # cost a page or stop the control loop.

    def _raise_egress_alert(self, mode) -> Callable[[], None]:
        """The fallback page: the alert stands for `mode` now. Returns the page's
        on_sent, which writes the record."""
        with self._egress_lock:
            self._egress_gen += 1
            self._egress_alert_mode = mode
            return functools.partial(self._fallback_page_sent, self._egress_gen, mode)

    def _fallback_page_sent(self, gen: int, mode) -> None:
        with self._egress_lock:
            if self._egress_gen == gen and self._egress_alert_mode == mode:
                self._write_egress_alert(mode)

    def _restore_egress_alert(self) -> Callable[[], None]:
        """The restore page: the alert ends now. Returns the page's on_sent, which
        removes the record. Until then a restart must still send a restore."""
        with self._egress_lock:
            self._egress_gen += 1
            self._egress_alert_mode = None
            return functools.partial(self._restore_page_sent, self._egress_gen)

    def _restore_page_sent(self, gen: int) -> None:
        with self._egress_lock:
            if self._egress_gen == gen and self._egress_alert_mode is None:
                self._remove_egress_alert()

    def _end_egress_alert(self, failure_level: int = logging.WARNING) -> None:
        """A silent end (a mode change, `skipped`, a record for another mode at the
        seed, or a seed with the check off). No page goes out, so nothing would
        confirm a deferred removal: the record goes now. A removal that fails is
        logged at failure_level."""
        with self._egress_lock:
            self._egress_gen += 1
            self._egress_alert_mode = None
            self._remove_egress_alert(failure_level)

    def _adopt_egress_alert(self, mode) -> None:
        """A record for the selected mode at the seed: its alert stands again, and
        the record stays as written."""
        with self._egress_lock:
            self._egress_gen += 1
            self._egress_alert_mode = mode

    def _count_egress_alert_as_announced(self, mode) -> None:
        """A fallback already confirmed at the seed, with no record for it, counts
        as announced. No page goes out for it, so it is recorded now."""
        with self._egress_lock:
            self._egress_gen += 1
            self._egress_alert_mode = mode
            self._write_egress_alert(mode)

    def _write_egress_alert(self, mode) -> None:
        path = self._egress_alert_path
        if path is None:
            return
        body = json.dumps({"selected": mode, "announced_at": self._wall_clock()})
        # A temp file beside the record, renamed over it, so a reader never meets
        # half a record. Its data is synced before the rename and the rename after
        # it, so a power loss leaves the old record or the new one. The temp name is
        # this writer's own.
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(body)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            _fsync_parent(path)
        except OSError as e:
            logging.warning("egress alert: cannot record the fallback in %s durably, so a "
                            "restart may page it again: %s", path, e)

    def _remove_egress_alert(self, failure_level: int = logging.WARNING) -> None:
        path = self._egress_alert_path
        if path is None:
            return
        try:
            remove_egress_alert_record(path)
        except OSError as e:
            logging.log(failure_level, "egress alert: cannot remove the record %s durably, "
                        "so a restart may take its fallback for still standing: %s", path, e)

    def _read_egress_alert(self) -> Optional[str]:
        """The selected mode the record names, or None when there is no usable
        record. Only a missing file passes without a warning. A file that cannot
        be read is left where it is."""
        path = self._egress_alert_path
        if path is None:
            return None
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            return None
        except OSError as e:
            logging.warning("egress alert: cannot read the record %s, so it counts "
                            "as absent: %s", path, e)
            return None
        try:
            rec = json.loads(raw)
        except (ValueError, RecursionError) as e:   # not UTF-8, not JSON, or too deep
            logging.warning("egress alert: the record %s is not JSON, so it counts "
                            "as absent: %s", path, e)
            return None
        selected = rec.get("selected") if isinstance(rec, dict) else None
        if not isinstance(selected, str) or not selected:
            logging.warning("egress alert: the record %s names no selected mode, so "
                            "it counts as absent", path)
            return None
        return selected
