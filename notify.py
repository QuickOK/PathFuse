#!/usr/bin/env python3
"""ntfy notifications for sbfd-ctl, delivered via the spool-notify helper.

Four units:
  RateLimiter        -- per-event-kind coalescing (pure logic, injectable clock)
  Notifier           -- bounded buffer + daemon thread that shells out to spool-notify
  EventDetector      -- edge-triggered event derivation from per-tick observations
  EgressRecordKeeper -- daemon thread that does the egress alert record's file I/O

Design notes: the control loop only ever calls Notifier.notify(), which
appends to an in-memory deque and returns; all subprocess work happens on the
worker thread. Delivery reliability (spool + redeliver when the uplink is
down) is spool-notify's job, not ours. The egress alert record works the same
way: after the first observation, EventDetector.observe() only queues record
operations, and the EgressRecordKeeper's thread does every write, removal and
fsync of it, so a disk that stalls never holds up the control loop. A stale
record is not trusted, because only a clean close with the alert standing
marks it (`closed_at` and the id of the boot, written by EventDetector.close()
alone, onto a record the run's own pages wrote, and dropped again by the
rewrite at the next adoption), and the mark is trusted within that boot alone;
every other ending fails toward a repeated page: an operation the disk refuses
is tried again for as long as the run lasts, and once more at the close; one
still queued when the process ends (a crash, a power loss, or a page
spool-notify takes after the keeper has closed) is lost, and costs at most one
repeated page at the next start; a record without the mark, or one a restart
cannot write again, is adopted as distrusted, so the fallback it names pages
again rather than never; and a run that could write nothing (a disk read-only
from its seed to its end) leaves the mark the run before it wrote, which the
reboot that frees such a disk puts out of trust (a disk freed by hand within the
boot, and a restart after it, trust it still: the accepted residual). So a
service restart during a standing fallback is silent, and a reboot during one
pages it once more (see EventDetector).
"""
import dataclasses
import functools
import itertools
import json
import logging
import math
import os
import queue
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

DEFAULT_COMMAND = "/usr/local/sbin/spool-notify"
# The egress alert record's one place (see EventDetector), in sbfd-ctl's
# StateDirectory: it survives a service restart and a reboot alike. It is not
# configurable, so every run finds, and can end, a record an earlier run left.
# sbfd_ctl reads it when it runs, never at import, so tests can point it elsewhere.
EGRESS_ALERT_PATH = "/var/lib/sbfd-ctl/egress_alert.json"
_RECORD_MAX_BYTES = 64 * 1024   # a record is a few hundred bytes; anything bigger is not one
# A record operation that fails with an OSError is tried again after each of these
# delays in turn, then every last one of them for as long as the run lasts (see
# EgressRecordKeeper). Tests shorten them; empty, no operation is tried again.
_RECORD_RETRY_DELAYS_S = (1.0, 5.0, 30.0)
# The kernel's id of this boot: the record's clean-close mark names the boot it was
# made in, and is trusted within that boot alone (see EventDetector).
_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
# Stands in for the boot's id where the kernel gives none: this process's own, so
# that no mark outlives the process there (see _boot_id).
_PROCESS_ID = uuid.uuid4().hex

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
    # Keep the egress alert record at EGRESS_ALERT_PATH: the egress fallback the
    # operator was paged about, so a restart in the middle of it does not page it
    # again (see EventDetector). Only a run with notifications on, the actual-exit
    # check on and this switch on keeps it up to date. Any other run removes it at
    # startup (sbfd_ctl.end_saved_egress_alert), so a later run cannot take a stale
    # alert over.
    egress_alert_record: bool = True


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
    so e.g. a flapping WAN never delays an all-WANs-down alert.

    A kind's events go out in the order they came: one whose kind has events
    held joins them, even once the window has run out. The egress alert record
    relies on that (see EventDetector)."""

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
    waits only so long, and says whether the worker ended in time), never runs
    it. An on_sent that raises is logged, and the worker carries on with the next
    page. A kind's pages are handed over, and their on_sents run, in the order
    the pages were made."""

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

    def stop(self, timeout: float = 5.0) -> bool:
        """Have the worker send what it holds and end, waiting at most `timeout`
        seconds for it. True once the worker thread has ended, or was never started;
        False while it is still sending. A page spool-notify takes after a False runs
        its on_sent after the caller has moved on: run_controller closes the egress
        record keeper next, so that page's record change is lost."""
        with self._cond:
            self._stopping = True
            self._cond.notify()
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

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
    # The actual-exit check's snapshot (egress_observer.EgressObserver.snapshot()),
    # or None when the check is off. Drives the egress fallback page, and the page
    # for a check that keeps failing (sized from its `error_checks` and `interval_s`).
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
    alert back. The directory is synced even when there is no record: a removal
    whose sync failed is tried again, and the retry finds no record, yet must still
    sync the removal. False when there was no record; any other failure raises
    OSError, so that it is tried again, except for a directory that does not exist,
    where there is nothing to remove or to sync."""
    try:
        os.remove(path)
    except FileNotFoundError:
        removed = False
    else:
        removed = True
    try:
        _fsync_parent(path)
    except FileNotFoundError:
        if removed:
            raise
    return removed


def _finite(value, default=None):
    """`value` when it is a finite number (a bool is not one), else `default`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        return value if math.isfinite(value) else default
    except OverflowError:   # an int past float range: not a finite number either
        return default


def _boot_id() -> str:
    """The id of this boot, the kernel's, which every process of the boot reads
    alike; or this process's own where the kernel gives none, so that no mark is
    trusted across a restart there, and a standing fallback pages once more: the
    direction the record may fail in."""
    try:
        with open(_BOOT_ID_PATH, encoding="ascii") as f:
            return f.read().strip() or _PROCESS_ID
    except (OSError, ValueError):
        return _PROCESS_ID


def _record_fields(raw: bytes) -> "tuple[str, float, Optional[str]]":
    """The egress alert record's fields from its bytes: the selected mode it names,
    when its fallback was paged, and the id of the boot its clean-close mark was
    made in, or None without the mark (see EventDetector). ValueError, saying why,
    when it is no record: not JSON, not an object, or naming no selected mode. An
    `announced_at` that is not a finite number reads as 0.0; a `closed_at` that is
    not one, or a `boot_id` that is not a non-empty string, as no mark."""
    try:
        rec = json.loads(raw)
    except (ValueError, RecursionError) as e:   # not UTF-8, not JSON, or too deep
        raise ValueError(f"is not JSON ({e})") from None
    selected = rec.get("selected") if isinstance(rec, dict) else None
    if not isinstance(selected, str) or not selected:
        raise ValueError("names no selected mode")
    announced_at = _finite(rec.get("announced_at"), 0.0)
    boot = rec.get("boot_id")
    if _finite(rec.get("closed_at")) is None or not isinstance(boot, str) or not boot:
        return selected, announced_at, None
    return selected, announced_at, boot


@dataclass(frozen=True)
class _RecordOp:
    """One operation on the egress alert record: write a record naming `mode`,
    remove the record when `mode` is None, or, with `mark`, add the clean-close mark
    (`closed_at`, and this boot's id) to the record if it names `mode`. A page's
    operation carries the generation the page was made under (see EventDetector);
    a change made without a page carries None. `refresh` marks the seed's rewrite
    of a record it adopts. A removal's `on_removed`, if any, is called on the
    keeper's thread once it has removed a record, at whichever attempt that is, and
    not when there was none to remove. `seq` is the operation's place in the queue,
    given as it is queued, and `tries` how many times it has been run before."""
    mode: Optional[str]
    announced_at: float = 0.0
    gen: Optional[int] = None
    failure_level: int = logging.WARNING
    refresh: bool = False
    mark: bool = False
    closed_at: float = 0.0
    on_removed: Optional[Callable[[], None]] = None
    seq: int = 0
    tries: int = 0


class EgressRecordKeeper:
    """Does all of the egress alert record's file I/O, on one daemon thread, one
    operation at a time, first in, first out.

    EventDetector and a page's on_sent only queue an operation and return, so a
    disk that stalls in a sync holds up this thread alone: never the control loop,
    nor the Notifier's next page. A page's operation runs only if `generation()`
    still returns the generation it carries when its turn comes; any other runs
    unconditionally (see EventDetector for why that keeps the record right).

    A write goes to a temp file beside the path, which is flushed, fsynced and
    renamed over the path, and then the directory is fsynced, so a reader never
    meets half a record and a power loss leaves the old record or the new one. A
    removal fsyncs the directory too, whether or not there was a record.

    The record's trust rule (see EventDetector): a record is trusted at the next
    start only if it carries the clean-close mark of this boot, `closed_at` with the
    boot's id, which close() alone writes, as its last operation, and only while a
    trusted alert stands: it is added to the record this run's pages left, never to
    a record of its own, since a page spool-notify refused left none, and never to
    a record an earlier run left behind, which such a refused page would otherwise
    have the next run trust. So this thread remembers the mode of the record it
    last wrote to completion, forgets it before any removal it attempts and any
    write it begins, and marks only while that mode is the standing alert's (see
    _mark). Every other write omits the mark, so a crash, a power cut, or a disk
    that refuses the run's last operations leaves the record unmarked; and a mark
    of an earlier boot is no mark to the run that finds it, so a run that could
    write nothing (a disk read-only from its seed to its end) leaves the mark the
    run before it wrote only to a run of the same boot: a disk freed by hand, the
    accepted residual (see EventDetector).

    An operation the disk refuses (an OSError) is tried again after each of
    _RECORD_RETRY_DELAYS_S in turn, then every last one of them, for as long as the
    run lasts: a daemon timer queues it again, so this thread never sleeps, and the
    operations behind it run meanwhile. The first len(_RECORD_RETRY_DELAYS_S)
    failures of an operation are logged at its level, the rest at debug, so a disk
    that refuses for hours does not flood the journal. A retried operation still
    carries its generation, and is dropped once a later operation has run, or
    raised, since the record follows the later one; an operation its generation
    check skipped ran nothing, and drops no retry. close() cancels the pending
    retries' timers and runs each of those operations once more, in queue order,
    before the mark and the exit; one that fails then is one warning. Any other
    failure is a warning, and the thread carries on with the next operation. A
    refresh, EventDetector's rewrite of the record it adopts at the seed, sets
    `unreliable` once it has failed len(_RECORD_RETRY_DELAYS_S) times (or raised
    anything else), which the detector reads without I/O: that alert is not to be
    trusted. The record only spares the operator a repeated page, so an operation
    lost at process end, still queued, or queued once close() has ended the thread,
    costs at most one repeated page at the next start."""

    def __init__(self, path: str, generation: Callable[[], int]):
        self.path = path
        self._generation = generation
        # Operations, drain()'s markers, and close()'s None.
        self._ops: "queue.Queue[Union[_RecordOp, threading.Event, None]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        # Set on this keeper's thread once a refresh has failed for good, and never
        # cleared; the detector reads it.
        self.unreliable = False
        # Guards _closing and _timers, and keeps seq in the queue's order. It is
        # shared with the detector's and the Notifier's threads (they queue), and
        # never held across I/O or a thread's start.
        self._lock = threading.Lock()
        self._closing = False
        self._seq = itertools.count(1)
        # (seq, tries) -> the timer and the operation it will queue again.
        self._timers: "dict[tuple[int, int], tuple[threading.Timer, _RecordOp]]" = {}
        self._last_seq = 0            # of the last operation run or raised; the thread's own
        # The mode of the record this thread last wrote to completion, or None once a
        # removal has been attempted or a write begun since: the only record the
        # clean-close mark may be added to (see _mark). The thread's own.
        self._settled: Optional[str] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="egress-record",
                                        daemon=True)
        self._thread.start()

    def write(self, mode: str, announced_at: float, gen: Optional[int] = None,
              refresh: bool = False) -> None:
        """Queue a write of the record naming `mode`, paged at `announced_at`. A
        `refresh` is the seed's rewrite of a record it adopts, as it is."""
        self._queue(_RecordOp(mode, announced_at, gen, refresh=refresh))

    def remove(self, gen: Optional[int] = None,
               failure_level: int = logging.WARNING,
               on_removed: "Optional[Callable[[], None]]" = None) -> None:
        """Queue the record's removal. A failure to remove it is logged at
        failure_level; no record to remove is no failure. `on_removed`, if given, is
        called on this keeper's thread once a record has been removed, at whichever
        attempt that is, and not when there was none."""
        self._queue(_RecordOp(None, gen=gen, failure_level=failure_level,
                              on_removed=on_removed))

    def _queue(self, op: _RecordOp) -> None:
        # Its place in the queue goes with it, so a retry can tell whether a later
        # operation has had its turn meanwhile.
        with self._lock:
            self._ops.put(dataclasses.replace(op, seq=next(self._seq)))

    def drain(self, timeout: float = 5.0) -> bool:
        """Wait at most `timeout` seconds for every operation queued so far to run.
        True once they have."""
        if self._thread is None or not self._thread.is_alive():
            return self._ops.empty()
        ran = threading.Event()
        self._ops.put(ran)
        return ran.wait(timeout)

    def close(self, timeout: float = 5.0,
              mark: "Optional[tuple[str, float]]" = None) -> bool:
        """Run every operation queued so far, then each operation whose retry is
        pending once more, in queue order, then, with `mark` (the standing alert's
        mode, and the wall-clock time of this close), add the clean-close mark (that
        time, and this boot's id) to the record if it names that mode, and end the
        thread. Waits at most `timeout` seconds, and returns True when the thread
        has ended; past that the thread carries on alone, and ends once the queue
        is through. An operation queued after this never runs, and no retry is made
        after this: a last attempt that fails is one warning."""
        if self._thread is None:
            return True
        with self._lock:
            if self._closing:
                timers: list = []
            else:
                self._closing = True
                timers, self._timers = list(self._timers.values()), {}
                # In queue order: each retry once more, then the mark, then the exit.
                for _timer, op in sorted(timers, key=lambda pending: pending[1].seq):
                    self._ops.put(op)
                if mark is not None:
                    self._ops.put(_RecordOp(mark[0], mark=True, closed_at=mark[1],
                                            seq=next(self._seq)))
                self._ops.put(None)
        for timer, _op in timers:
            timer.cancel()
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            op = self._ops.get()
            if op is None:
                return
            if isinstance(op, threading.Event):
                op.set()
                continue
            if op.seq < self._last_seq:
                # A retry, and a later operation has had its turn since it failed:
                # the record follows that one.
                logging.debug("egress alert: a record operation the disk refused is "
                              "dropped, a later one having run since")
                continue
            try:
                ran = self._apply(op)
            except OSError as e:
                self._last_seq = op.seq
                self._failed(op, e)
            except Exception as e:   # the thread must outlive any one operation
                self._last_seq = op.seq
                if op.refresh:
                    self.unreliable = True
                logging.warning("egress alert: a record operation failed, and the "
                                "keeper carries on: %r", e, exc_info=True)
            else:
                if ran:
                    self._last_seq = op.seq

    def _apply(self, op: _RecordOp) -> bool:
        """Run `op`, unless its page is out of date, or it is a mark with no record to
        mark: True when it ran, so that it counts as the record's latest news (a retry
        of an earlier one is dropped). OSError when the disk refuses."""
        if op.gen is not None and op.gen != self._generation():
            return False   # a change made without a page came after this page was made
        if op.mark:
            return self._mark(op)
        # A removal or a write that fails leaves the record in doubt, so the record
        # this run last wrote is forgotten first, and remembered again only once a
        # write has completed.
        self._settled = None
        if op.mode is None:
            if remove_egress_alert_record(self.path) and op.on_removed is not None:
                op.on_removed()
        else:
            self._write(op.mode, op.announced_at)
            self._settled = op.mode
        return True

    def _mark(self, op: _RecordOp) -> bool:
        """Add the clean-close mark to the record, if this run wrote it (the last
        write to complete named `op.mode`, and no removal has been attempted since)
        and it still names `op.mode`: the record the pages left, as they left it,
        with `closed_at` and this boot's id added. Otherwise the record is left as it
        is: none (a page spool-notify refused left none, and the fallback it would
        name was never paged), another mode's, or one this run never wrote, which an
        earlier run's refused removal or power cut left behind, and which may name
        an alert long restored; a later run then pages the fallback, a repeat at
        worst. True when the record was written."""
        if self._settled != op.mode:
            logging.debug("egress alert: no record this run wrote in %s names %s, so "
                          "none is marked as closed", self.path, op.mode)
            return False
        try:
            with open(self.path, "rb") as f:
                raw = f.read(_RECORD_MAX_BYTES)
        except FileNotFoundError:
            logging.debug("egress alert: no record in %s to mark as closed", self.path)
            return False
        try:
            selected, announced_at, _mark_boot = _record_fields(raw)
        except ValueError as e:
            logging.debug("egress alert: the record %s %s, so it is not marked as closed",
                          self.path, e)
            return False
        if selected != op.mode:
            return False
        self._write(selected, announced_at, op.closed_at)
        return True

    def _failed(self, op: _RecordOp, e: OSError) -> None:
        """`op` raised `e`: log it, and have it tried again after the next listed delay,
        or the last one again once they have all run; for as long as the run lasts.
        Only the first len(_RECORD_RETRY_DELAYS_S) failures are logged at the
        operation's level, and a refresh that has failed that many times marks the
        record unreliable. No retry once close() has been called, or with no delay
        listed: that failure is logged at the operation's level, and says so."""
        delays = _RECORD_RETRY_DELAYS_S
        listed = len(delays)
        tried = op.tries + 1
        what, so = self._describe(op)
        if op.refresh and tried >= listed:
            self.unreliable = True
        delay = delays[min(op.tries, listed - 1)] if delays else None
        if delay is not None and self._retry_after(delay, dataclasses.replace(op, tries=tried)):
            if tried < listed:
                logging.log(op.failure_level, "egress alert: cannot %s durably: %s; the "
                            "keeper tries again in %g s (failure %d of %d)", what, e,
                            delay, tried, listed)
            elif tried == listed:
                logging.log(op.failure_level, "egress alert: cannot %s durably, so %s: %s; "
                            "the keeper tries again in %g s, and keeps trying at that "
                            "interval, quietly, until it closes (failure %d of %d)", what,
                            so, e, delay, tried, listed)
            else:
                logging.debug("egress alert: cannot %s durably: %s; the keeper tries again "
                              "in %g s (failure %d)", what, e, delay, tried)
            return
        why = "the keeper is closing" if delay is not None else "no retry delay is listed"
        logging.log(op.failure_level, "egress alert: cannot %s durably, and %s, so %s: %s",
                    what, why, so, e)

    def _describe(self, op: _RecordOp) -> "tuple[str, str]":
        """(what the operation does, what its failure means), for its warnings."""
        if op.mark:
            return (f"mark the record {self.path} as closed",
                    "the next run will not trust it, and may page its fallback again")
        if op.mode is None:
            return (f"remove the record {self.path}",
                    "the next run may page its fallback again, or send its restore again")
        if op.refresh:
            return (f"write the record {self.path} again",
                    "the fallback it names counts as unannounced")
        return f"record the fallback in {self.path}", "a restart may page it again"

    def _retry_after(self, delay: float, op: _RecordOp) -> bool:
        """Have `op` queued again after `delay` seconds, by a daemon timer, so this
        thread is free for the operations behind it. False once close() has been
        called: no retry then. The timer starts outside the lock, which the
        detector's and the Notifier's threads take to queue an operation."""
        key = (op.seq, op.tries)
        timer = threading.Timer(delay, self._requeue, (key, op))
        timer.daemon = True
        timer.name = "egress-record-retry"
        with self._lock:
            if self._closing:
                return False
            self._timers[key] = (timer, op)
        # A close() in between has cancelled it, and queues the operation itself: a
        # cancelled timer ends as soon as it starts.
        timer.start()
        return True

    def _requeue(self, key: "tuple[int, int]", op: _RecordOp) -> None:
        # On the timer's thread: back into the queue, unless close() has been called,
        # in which case close() has queued it once more itself.
        with self._lock:
            self._timers.pop(key, None)
            if not self._closing:
                self._ops.put(op)

    def _write(self, mode: str, announced_at: float, closed_at: Optional[float] = None) -> None:
        path = self.path
        rec: dict = {"selected": mode, "announced_at": announced_at}
        if closed_at is not None:
            rec["closed_at"] = closed_at
            rec["boot_id"] = _boot_id()
        body = json.dumps(rec)
        # A temp file beside the record, renamed over it, so a reader never meets
        # half a record. Its data is synced before the rename and the rename after
        # it, so a power loss leaves the old record or the new one. The temp name is
        # this writer's own.
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
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
    egress_alert_path set, the record there follows each egress page spool-notify
    accepted, in the order the pages were made. The controller passes
    EGRESS_ALERT_PATH, the record's one place, unless the record is switched off.
    A fallback page's on_sent writes the record and a restore page's removes it,
    both run by the Notifier once spool-notify has the page. So a page that goes
    out after its alert has moved on still settles the record, and the pages
    after it settle the record again as they go out. A page that never got that
    far (refused, or still buffered when the process ended) leaves the record
    where the pages before it left it, so a restart pages a fallback the operator
    never heard of, and sends a restore they never got.

    Every change of the selected mode, and every `skipped` (the check does not
    run while local_direct is selected), ends the saved record without a page,
    whether or not an alert stands here and whether or not a record exists: an
    alert that stands ends at once, and the record's removal is queued. A page
    made before such an end no longer touches the record. The alert itself
    changes at once in every case, so what a run pages waits neither for the
    Notifier nor for the disk.

    Threads: observe() runs on the controller's thread and on_sent on the
    Notifier's, and neither touches the record file, but for the one read at the
    seed. Each change only queues an operation for this detector's
    EgressRecordKeeper, whose own thread does all the writing, removing and
    syncing, in queue order. So after the seed, observe() never waits for the
    disk, and nor does the Notifier. close() runs what is queued and stops that
    thread.

    A restart reads the record at the seed: a fallback paged before the restart
    is not paged again and its restore still is, while one that began during the
    restart pages as usual. So does a mismatch the seed itself finds confirmed
    with no record for it: it is unannounced, whether it began while the
    controller was down or its record was lost, and the next tick pages it.

    The trust rule. A record can be stale: a restore page's removal the disk
    refused, a crash after a restore page, a power cut. So the record carries a
    clean-close mark, `closed_at` with the id of the boot it was made in, which
    close() alone writes, as the keeper's last operation, and only while a trusted
    alert stands: through run_controller's shutdown, and onto the record this
    run's pages left (a page spool-notify refused left none, and none is made for
    it; a record an earlier run left is never marked, even one naming the alert's
    mode, see EgressRecordKeeper). Every other write omits it. A record with the
    mark of this boot, for the selected mode, is adopted as trusted (a confirmed
    mismatch stays silent, a match pages the restore), and written once more
    without the mark, so a crash of this run leaves it unmarked; a rewrite the
    keeper cannot complete withdraws the trust. One without the mark, or with an
    earlier boot's, is adopted as distrusted from the start: the alert stands for
    its restore, but its fallback counts as unannounced, so a confirmed mismatch
    pages it, a repeat at worst, and the record follows that page as usual. So the
    mark certifies that the last run to write the record closed cleanly with that
    alert standing, and every other ending of a run that can write fails toward a
    repeated page. A run that can write nothing, its disk read-only from its seed
    to its end, neither strips the mark the run before it left nor removes the
    record; but only a reboot frees such a disk, and the mark is trusted within
    the boot that made it alone, so the run after the reboot pages the fallback it
    confirms. The cost: a reboot during a standing fallback pages it once more,
    where a service restart stays silent. What remains, and is accepted: a disk
    freed by hand within the boot (a remount), and a service restart after it,
    trust that mark still, so a new fallback that restart confirms stays silent.
    With egress_alert_path None the detector has no keeper and does no file I/O
    at all, and a restart in the middle of a fallback pages it again.

    A check that keeps failing (`failing`: error_checks failed checks in a row)
    says nothing about the exit, so it has an alert of its own, beside the
    fallback alert and independent of it, paged once when the status reaches
    `failing` and once when a check works again. That alert is in memory only:
    a restart re-pages it once the failure is confirmed again, by design."""

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
        # egress alert stands. _egress_gen moves on at each change made without a
        # page (a silent end, or the seed's), so a page's record operation can
        # tell that one happened after its page was made. Only the controller's
        # thread writes either; the keeper's reads _egress_gen. _egress_selected
        # and _egress_skipped are what the last egress snapshot said, so a change
        # can be told from a repeat.
        self._egress_alert_mode: Optional[str] = None
        # True while the standing alert is the one the seed adopted and its record has
        # not been found wanting: only then is the keeper's `unreliable` read, once a
        # tick. True once that record could not be written again: the alert stands
        # for its restore, but its fallback counts as unannounced.
        self._egress_alert_adopted = False
        self._egress_alert_unreliable = False
        self._egress_gen = 0
        self._egress_selected: Optional[str] = None
        self._egress_skipped = False
        # True while the exit check's failing has been announced (see _egress_check_events).
        self._egress_check_failing = False
        self._egress_alert_path = egress_alert_path
        # Made at the seed when there is a path: it does the record's file I/O.
        self._keeper: Optional[EgressRecordKeeper] = None

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

    def drain(self, timeout: float = 5.0) -> bool:
        """Wait at most `timeout` seconds for the record operations queued so far to
        run. True once they have, or when there is no record to keep."""
        return self._keeper is None or self._keeper.drain(timeout)

    def close(self, timeout: float = 5.0) -> bool:
        """Run the record operations queued so far, and once more each one whose
        retry is pending, then, while a trusted alert stands, have the record it
        names marked as closed (`closed_at`, now, and this boot's id), if this run's
        pages wrote that record (see EgressRecordKeeper), waiting at most `timeout`
        seconds in all, and stop the record keeper's thread. The mark is the one
        thing a later run, within this boot, trusts the record for (see the class
        docstring): only this clean close writes it, and a distrusted alert (a record
        adopted at the seed without the mark or with an earlier boot's, or one whose
        rewrite failed) leaves the record unmarked; a run of this boot that could
        write nothing leaves an earlier mark standing, the accepted residual. For
        shutdown, once the Notifier has stopped, or its stop() has given up on a
        send: its stop() sends the pages it still holds, and their on_sents queue
        operations of their own, ahead of the mark. True when the keeper finished in
        time, or when there is no record to keep. An operation queued after this
        never runs, as if the process had ended."""
        if self._keeper is None:
            return True
        self._distrust_if_unwritable()
        mark = None
        if self._egress_alert_mode is not None and not self._egress_alert_unreliable:
            mark = (self._egress_alert_mode, self._wall_clock())
        return self._keeper.close(timeout, mark=mark)

    def _seed(self, obs):
        # Whatever is already broken at startup is treated as announced: no
        # alert now, and none once the hold expires either. A later recovery
        # still reports, which is the useful half of the edge. (An egress
        # fallback is the exception: its record says whether it was announced,
        # and without one it pages. See _seed_egress.)
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
        # The record keeper starts here, before the seed queues anything for it.
        if self._egress_alert_path is not None:
            self._keeper = EgressRecordKeeper(self._egress_alert_path,
                                              lambda: self._egress_gen)
            self._keeper.start()
        # With the check off there is no egress, and this run cannot follow a saved
        # alert: the fallback could end, or a new one begin, unseen. So it ends the
        # alert and removes the record, unread, or a later run with the check on
        # would take a stale alert over. Quietly: the controller has already tried
        # to remove it at startup, and warned if it could not.
        if obs.egress:
            self._seed_egress(obs.egress)
        else:
            self._end_egress_alert(failure_level=logging.DEBUG)
        self._seeded = True

    def _seed_egress(self, e: dict) -> None:
        # An egress fallback standing at a restart is invisible here: a fresh
        # observer reports `checking` until its first check. The record follows the
        # egress pages spool-notify accepted, so it tells a fallback the operator was
        # paged about from one that began during the restart. Reading it is the one
        # piece of record I/O on the controller's thread: a single small read, once.
        #   - A record for the selected mode, with the clean-close mark of this boot:
        #     the last run to write it ended through close() with this alert
        #     standing and trusted, so its fallback page went out and no restore
        #     page has (unless a run of this boot since could write nothing, its
        #     disk freed by hand after: the accepted residual, see the class
        #     docstring). The alert stands, trusted: a confirmed mismatch stays
        #     silent and a match sends the restore. The record is written once more,
        #     without the mark (see _adopt_egress_alert): the one write per
        #     adoption, so a crash of this run leaves it unmarked. A rewrite the
        #     keeper cannot complete withdraws the trust (see _egress_events).
        #   - A record for the selected mode, without the mark, or with an earlier
        #     boot's: the last run ended some other way (a crash, a power cut, a disk
        #     that refused its last operations), or a reboot has come between, and
        #     with it perhaps a run that could write nothing; nothing says whether
        #     the fallback still stood. The alert stands, distrusted: it stands for
        #     its restore, but its fallback counts as unannounced, so a confirmed
        #     mismatch pages it, a repeat at worst, and the record follows that page
        #     as usual. Nothing is written.
        #   - A record for another mode: the selected mode changed while the
        #     controller was down, which ends an alert silently. Drop it, mark or no.
        #   - No usable record: nothing stands, so a fallback pages as usual.
        # That holds for a `mismatch` already confirmed here as well: with no record
        # for it, it is unannounced, whether it began while the controller was down
        # or its record was lost, and both must page. Nothing stands, so the next
        # tick pages it (see _egress_events) and the record follows that page, like
        # any other. A `pending` is not yet a fallback, so it stays armed too.
        # A `skipped` ends the saved record, as it does on any later tick (see
        # _egress_events), so the record goes unread.
        # A check already `failing` counts as announced, like anything else broken
        # at startup: no page now, and the first check that works pages the recovery.
        status, selected = e.get("status"), e.get("selected")
        self._egress_selected, self._egress_skipped = selected, status == "skipped"
        self._egress_check_failing = status == "failing"
        if status == "skipped":
            self._end_egress_alert()
            return
        recorded = self._read_egress_alert()
        if recorded is not None:
            mode, announced_at, mark_boot = recorded
            if mode == selected:
                self._adopt_egress_alert(mode, announced_at, mark_boot)
            else:
                self._end_egress_alert()

    # -- per-category edges ----------------------------------------------

    def _maint_wan(self, obs) -> Optional[str]:
        """The WAN currently under maintenance, or None. Fails open.

        `until` is an absolute wall-clock epoch (the window is written by
        another process and must survive a restart of this one), so it is
        judged against the wall clock, NOT against self._clock() — that one is
        monotonic (seconds since boot) and would leave every window looking
        open, muting a WAN's alerts indefinitely.

        `until` must also be FINITE, and must not be a bool; an int past float
        range is no timestamp either (math.isfinite raises on it). json.loads accepts
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
        if not isinstance(wan, str):
            return None
        if _finite(until) is None:   # not a number, a bool, non-finite, or past float range
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

        An alert belongs to the selected mode it was raised for. Every change of
        the selected mode, and every `skipped` (the check does not run while
        local_direct is selected), ends the saved record without a page: the alert
        ends silently if one stands, and the record goes, alert or no alert. A
        restore page spool-notify refused leaves a record with no alert standing,
        so that a restart sends the restore again; once the mode has moved, that
        record stands for nothing a later run should take over. A fallback on the
        new mode then pages afresh, and a match under it is not announced as a
        restore. A run of `skipped` ends the record once, at its first: no page is
        made while it lasts, so nothing can write the record again in between, and
        a removal repeated every tick would only repeat its warning on a disk that
        refuses it.
        `checking` alone ends nothing: the observer reports it at its own start
        and after a mode change, and the mode comparison already covers a change.
        That leaves its start, after a restart, where an alert the record carried
        over (see _seed_egress) must stand until the first check confirms or
        clears it. A mismatch the seed found with no record for it left nothing
        standing, so it pages here on the first tick after the seed: it is
        unannounced, whether it began while the controller was down or its record
        was lost.

        An alert the seed adopted from a marked record stands on trust in the mark,
        and the record is rewritten without it. The keeper's `unreliable`, read
        here at the start of each tick while such an alert stands, says that
        rewrite has failed for good (see _distrust_if_unwritable): the alert is then
        distrusted, with one warning, like one adopted from an unmarked record. A
        distrusted alert's fallback counts as unannounced: a confirmed mismatch
        pages the fallback as if nothing stood (a repeat at worst), and that page's
        record follows as usual; a confirmed match still pages the restore once and
        queues the removal; a mode change or `skipped` ends it as any other.

        Each page carries the on_sent that settles the record once spool-notify
        accepts it (see the class docstring); a silent end settles it itself.

        The check's own alert (see _egress_check_events) is judged on the same tick,
        first: a mismatch is a check that works, so its page follows the one that
        says so."""
        self._distrust_if_unwritable()
        e = obs.egress
        if not e:
            return []
        status, selected = e.get("status"), e.get("selected")
        skipped = status == "skipped"
        moved = selected != self._egress_selected
        if moved or (skipped and not self._egress_skipped):
            self._end_egress_alert()
        if moved:
            # A `skipped` is a move too: the check never reports `failing` under
            # local_direct, so the alert's mode is another, and the move ends it.
            self._egress_check_failing = False
        self._egress_selected, self._egress_skipped = selected, skipped
        evs = self._egress_check_events(e, status)
        if status == "mismatch" and (self._egress_alert_mode is None
                                     or self._egress_alert_unreliable):
            on_sent = self._raise_egress_alert(selected)
            ip = f" ({e['ip']})" if e.get("ip") else ""
            evs.append(Event("egress", "🧭 Egress fallback",
                             f"selected {egress_label(selected)}, "
                             f"actual {egress_label(e.get('observed'))}{ip}", "high",
                             on_sent=on_sent))
        elif status == "match" and self._egress_alert_mode is not None:
            evs.append(Event("egress", "🧭 Egress restored",
                             f"actual exit matches {egress_label(selected)} again",
                             "default", on_sent=self._restore_egress_alert()))
        return evs

    def _egress_check_events(self, e: dict, status) -> list:
        """Page once when the exit check has failed `error_checks` times in a row
        (`failing`), and once when a check works again (match, pending or mismatch).
        A failed check says nothing about the exit, so this alert stands beside the
        fallback alert and neither touches the other: a `failing` under a standing
        fallback is no restore, and a mismatch that ends this alert raises that one
        as usual. `error` (not yet confirmed) and `checking` leave it as it is; a
        change of the selected mode, which a `skipped` always is, ends it silently
        (the caller does that, with the fallback alert's). It is kept in memory
        only, with no record: a restart re-pages it once the failure is confirmed
        again.

        The failing page says how long the exit has gone unchecked, error_checks
        intervals, from the cadence the observer's snapshot carries; without a
        usable cadence it names the error alone."""
        if status == "failing" and not self._egress_check_failing:
            self._egress_check_failing = True
            error = e.get("error") or "unknown error"
            checks = _finite(e.get("error_checks"))
            interval_s = _finite(e.get("interval_s"))
            span = (f" for {round(checks * interval_s / 60)} min"
                    if checks is not None and interval_s is not None else "")
            return [Event("egress_check", "🧭 Egress check failing",
                          f"no exit check{span}: {error}", "default")]
        if status in ("match", "pending", "mismatch") and self._egress_check_failing:
            self._egress_check_failing = False
            return [Event("egress_check", "🧭 Egress check working again",
                          "the exit check succeeded again", "default")]
        return []

    # -- the egress alert record ------------------------------------------------
    #
    # Every transition below moves the alert at once, on the controller's thread,
    # and leaves the record to the keeper: it queues an operation, or hands its
    # page an on_sent that queues one. None of them waits for the disk: the one
    # lock shared with the keeper (and the Notifier, whose thread runs the
    # on_sents) is the keeper's queue lock, never held across I/O.
    #
    # Pages. The Notifier runs on_sent only for a page spool-notify accepted, and
    # runs a kind's on_sents in the order their pages were made (a held run of
    # pages goes out as one summary carrying the last one's). So the pages'
    # operations reach the queue in page order, and the keeper runs the queue in
    # order: a page's operation applies whatever the alert has done since the page
    # was made, and the next page's applies after it.
    #
    # Changes made without a page: a silent end, and the seed's changes. Each moves
    # _egress_gen on, and only then queues its own operation, if it has one. A
    # page's operation applies only if the generation, read when its turn comes,
    # is still the one its page was made under. Such a change settles the record
    # itself, with no page to redo it, and a late page would otherwise undo a
    # silent end's removal for good. For a page made before a silent end whose
    # on_sent comes late, both ways round:
    #   - its operation is queued before the end's removal: the removal runs after
    #     it, whatever it did;
    #   - it is queued after the removal: the generation had moved on before the
    #     removal was queued, so by the operation's turn its page is out of date,
    #     and it does nothing.
    # Either way the ended alert leaves no record. A page made after the end
    # carries the new generation, and its operation can only be queued after the
    # end's removal, so it applies as usual.
    #
    # What holds once the queue has run: the record says what the last page
    # spool-notify accepted said, among the pages made since the last change made
    # without a page; with no such page, it is what that change left. That is the
    # operator's latest news, but for two things: a page made before such a change
    # and accepted after it counts for nothing, and spool-notify accepting a page
    # means it is sent or spooled for redelivery, not that it has been read.
    #
    # The record only spares the operator a repeated page. A failure to read,
    # write, sync or remove it is logged and the tick carries on: it must never
    # cost a page or stop the control loop.

    def _raise_egress_alert(self, mode) -> Callable[[], None]:
        """The fallback page: the alert stands for `mode` now, on its own page rather
        than an adopted record. Returns the page's on_sent, which has the record
        written."""
        self._egress_alert_mode = mode
        self._egress_alert_adopted = self._egress_alert_unreliable = False
        return functools.partial(self._fallback_page_sent, self._egress_gen, mode)

    def _fallback_page_sent(self, gen: int, mode) -> None:
        """spool-notify took the fallback page; this runs on the Notifier's thread.
        Queue the record's write. Unless a change made without a page came after the
        page was made, the record then names its mode, even if the alert moved on."""
        if self._keeper is not None:
            self._keeper.write(mode, self._wall_clock(), gen)

    def _restore_egress_alert(self) -> Callable[[], None]:
        """The restore page: the alert ends now. Returns the page's on_sent, which
        has the record removed. Until then a restart must still send a restore."""
        self._egress_alert_mode = None
        self._egress_alert_adopted = self._egress_alert_unreliable = False
        return functools.partial(self._restore_page_sent, self._egress_gen)

    def _restore_page_sent(self, gen: int) -> None:
        """spool-notify took the restore page; this runs on the Notifier's thread.
        Queue the record's removal. Unless a change made without a page came after
        the page was made, the record goes, even if a new fallback stands by now."""
        if self._keeper is not None:
            self._keeper.remove(gen)

    def _end_egress_alert(self, failure_level: int = logging.WARNING) -> None:
        """A silent end (a mode change, `skipped`, a record for another mode at the
        seed, or a seed with the check off). No page goes out, so nothing would
        confirm a deferred removal: the generation moves on, so no page made before
        the end brings the record back, and then the removal is queued. A removal
        that fails is logged at failure_level."""
        self._egress_gen += 1
        self._egress_alert_mode = None
        self._egress_alert_adopted = self._egress_alert_unreliable = False
        if self._keeper is not None:
            self._keeper.remove(failure_level=failure_level)

    def _adopt_egress_alert(self, mode, announced_at: float,
                            mark_boot: Optional[str]) -> None:
        """A record for the selected mode at the seed: its alert stands again. With
        the clean-close mark of this boot (`mark_boot`, the id of the boot the mark
        was made in), it is trusted: the last run to write it ended through close()
        with this alert standing, so nothing stale can have been left, and no
        reboot has come between (a run of this boot that could write nothing, its
        disk freed by hand after, is the accepted residual). The record is then
        written once more with its own `selected` and `announced_at` and without the
        mark, so it still names the page, and a crash of this run leaves it unmarked;
        a rewrite that fails for good sets the keeper's `unreliable`, which
        _distrust_if_unwritable reads, and the trust is withdrawn. Nothing else
        writes the record while the alert stands. Without the mark, or with an
        earlier boot's, the alert is distrusted from the start (see
        _distrust_egress_alert), at info, since a power cut, and a reboot during a
        standing fallback, end that way, and the record is left as it is."""
        self._egress_gen += 1
        self._egress_alert_mode = mode
        if mark_boot is None:
            self._distrust_egress_alert("was not closed cleanly by the run that left it",
                                        logging.INFO)
            return
        if mark_boot != _boot_id():
            self._distrust_egress_alert("was closed cleanly, but in an earlier boot",
                                        logging.INFO)
            return
        self._egress_alert_adopted, self._egress_alert_unreliable = True, False
        if self._keeper is not None:
            self._keeper.write(mode, announced_at, self._egress_gen, refresh=True)

    def _distrust_if_unwritable(self) -> None:
        """Withdraw the trust in an adopted alert whose record the keeper could not
        write again. The keeper's `unreliable` is one attribute read, no I/O and no
        lock, made at the start of each tick and at close() while such an alert
        stands. Set, the disk that refused the rewrite may have refused the removal
        that should have ended the alert before the restart, and the mark the record
        carried proves nothing about this run."""
        if (self._egress_alert_adopted and self._keeper is not None
                and self._keeper.unreliable):
            self._distrust_egress_alert("could not be written again", logging.WARNING)

    def _distrust_egress_alert(self, why: str, level: int) -> None:
        """The adopted alert is not trusted, because its record `why`: it stands, for
        its restore, but its fallback counts as unannounced, so a confirmed mismatch
        pages it, a repeat at worst, and that page's record follows as usual. Said
        once, at `level`. close() marks no such alert's record, so the next run
        distrusts it too, until a page or a clean close settles it."""
        self._egress_alert_adopted, self._egress_alert_unreliable = False, True
        logging.log(level, "egress alert: the record %s %s, so it is not trusted: the "
                    "fallback it names (%s) counts as unannounced, and a confirmed "
                    "mismatch pages it, perhaps again", self._egress_alert_path, why,
                    egress_label(self._egress_alert_mode))

    def _read_egress_alert(self) -> "Optional[tuple[str, float, Optional[str]]]":
        """The selected mode the record names, when its fallback was paged, and the
        id of the boot its clean-close mark was made in (None without the mark), or
        None when there is no usable record. Only a missing file passes without a
        warning. A file that cannot be read is left where it is. An `announced_at`
        that is not a finite number reads as 0.0; a `closed_at` that is not one, or
        a `boot_id` that is not a non-empty string, as no mark."""
        path = self._egress_alert_path
        if path is None:
            return None
        try:
            with open(path, "rb") as f:
                raw = f.read(_RECORD_MAX_BYTES)
        except FileNotFoundError:
            return None
        except OSError as e:
            logging.warning("egress alert: cannot read the record %s, so it counts "
                            "as absent: %s", path, e)
            return None
        try:
            return _record_fields(raw)
        except Exception as e:
            # Whatever a malformed record raises (_record_fields raises ValueError; the
            # rest is belt and braces), it counts as absent: the seed runs on the
            # controller's thread, and a record file must never stop a start.
            logging.warning("egress alert: the record %s %s, so it counts as absent",
                            path, e)
            return None
