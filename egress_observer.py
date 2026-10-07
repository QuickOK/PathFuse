"""Actual-exit check: which egress the relay path really uses right now.

sbfd-ctl selects an egress mode and the relay enacts it, but the relay can fall
back without telling anyone: an upstream fails its health probe, or the relay
never learns the mode. This module fetches a plain-text trace page through the
tunnel, reads its `key=value` lines, classifies the exit against configured
rules, and tracks whether the observed exit matches the selected mode.

Config (`egress.observe` in the sbfd-ctl config; off while `url` is empty):

    {"url": "https://probe.example.net/trace", "iface": "wg0",
     "interval_s": 120, "timeout_s": 8, "mismatch_checks": 2, "error_checks": 3,
     "exits": [{"mode": "relay_backbone", "field": "ip", "values": ["203.0.113.10"]}]}

`exits` needs at least one rule. Bounds: interval_s 5..86400, timeout_s up to 120,
mismatch_checks and error_checks 1..100.

The first rule whose `field` value appears in `values` names the observed exit.
A page that carries none of the rule fields (an HTTP error, a redirect, an empty
body) says nothing about the exit, so it is an `error`, never a `mismatch`.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional, Protocol

_IP_MAX_CHARS = 64   # the `ip` text is stored as the page sent it, so it is capped


@dataclass(frozen=True)
class ExitRule:
    mode: str
    field: str
    values: tuple


@dataclass(frozen=True)
class ObserveCfg:
    url: str
    iface: str = "wg0"
    interval_s: float = 120.0
    timeout_s: float = 8.0
    mismatch_checks: int = 2
    error_checks: int = 3
    exits: tuple = ()


def _bounded(name: str, value, lo: float, hi: float, open_lo: bool = False) -> float:
    """float(value) when it lies in [lo, hi] ((lo, hi] with open_lo); else ValueError."""
    if isinstance(value, bool):
        raise ValueError(f"egress.observe.{name} must be a number, got {value!r}")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"egress.observe.{name} must be a number, got {value!r}") from None
    if not ((f > lo if open_lo else f >= lo) and f <= hi):   # a NaN fails both comparisons
        raise ValueError(f"egress.observe.{name} must be within "
                         f"{'(' if open_lo else '['}{lo:g}, {hi:g}], got {value!r}")
    return f


def _count(name: str, value) -> int:
    """`value` when it is an integer within [1, 100] (a bool is not one); else ValueError."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError(
            f"egress.observe.{name} must be an integer within [1, 100], got {value!r}")
    return value


def parse_observe_cfg(raw, valid_modes) -> Optional[ObserveCfg]:
    """ObserveCfg from the raw `egress.observe` block. None when the block is
    absent or `url` is empty (the check is off). ValueError on anything malformed,
    and on a `url` with no `exits` rule: nothing could then name an exit."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("egress.observe must be an object")
    url = raw.get("url", "")
    if not isinstance(url, str):
        raise ValueError("egress.observe.url must be a string")
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        raise ValueError("egress.observe.url must be an http(s) URL")
    iface = raw.get("iface", "wg0")
    if not isinstance(iface, str) or not iface:
        raise ValueError("egress.observe.iface must be an interface name")
    checks = _count("mismatch_checks", raw.get("mismatch_checks", 2))
    error_checks = _count("error_checks", raw.get("error_checks", 3))
    interval_s = _bounded("interval_s", raw.get("interval_s", 120), 5, 86400)
    timeout_s = _bounded("timeout_s", raw.get("timeout_s", 8), 0, 120, open_lo=True)
    rules = []
    for i, r in enumerate(raw.get("exits") or []):
        if not isinstance(r, dict):
            raise ValueError(f"egress.observe.exits[{i}] must be an object")
        mode, field, values = r.get("mode"), r.get("field"), r.get("values")
        if not isinstance(mode, str) or mode not in valid_modes:
            raise ValueError(f"egress.observe.exits[{i}].mode must be one of {sorted(valid_modes)}")
        if not isinstance(field, str) or not field:
            raise ValueError(f"egress.observe.exits[{i}].field must be a non-empty string")
        if (not isinstance(values, list) or not values
                or not all(isinstance(v, str) and v for v in values)):
            raise ValueError(f"egress.observe.exits[{i}].values must be a non-empty list of strings")
        rules.append(ExitRule(mode=mode, field=field, values=tuple(values)))
    if not rules:
        raise ValueError("egress.observe.exits must list at least one rule")
    for mode in sorted(set(valid_modes) - {"local_direct"} - {r.mode for r in rules}):
        logging.warning("egress.observe: no exits rule names %s, so the check can never observe it "
                        "and reports a mismatch whenever it is selected", mode)
    return ObserveCfg(url=url, iface=iface, interval_s=interval_s, timeout_s=timeout_s,
                      mismatch_checks=checks, error_checks=error_checks, exits=tuple(rules))


def parse_trace(body: Optional[str]) -> dict:
    """`key=value` lines into a dict; other lines are ignored."""
    out = {}
    for line in (body or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


def classify(fields: dict, rules) -> str:
    """The mode of the first rule that matches `fields`, else "unknown"."""
    for r in rules:
        if fields.get(r.field) in r.values:
            return r.mode
    return "unknown"


def fetch_trace(url: str, iface: str, timeout_s: float, runner=subprocess.run) -> tuple:
    """(body, None) or (None, error).

    curl binds to `iface` with SO_BINDTODEVICE, which an unprivileged process may
    do on a fresh socket (Linux >= 5.7). With no route via that interface, the
    kernel treats the destination as on-link, so the request takes the tunnel.

    `--fail` makes curl exit 22 on an HTTP error status, so a 503 comes back as an
    error and not as a page to classify. The body is decoded as UTF-8 with
    replacement, so stray bytes cannot raise.
    """
    argv = ["curl", "--silent", "--show-error", "--fail", "--max-time", f"{timeout_s:g}",
            "--interface", iface, url]
    try:
        r = runner(argv, capture_output=True, encoding="utf-8", errors="replace",
                   timeout=timeout_s + 3)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError as e:
        return None, f"cannot run curl: {e}"
    if r.returncode != 0:
        return None, f"curl rc={r.returncode}: {(r.stderr or '').strip()[:80]}"
    return r.stdout, None


class ExitTracker:
    """Turns successive checks into one status:

    checking  no check yet since start, or since the selected mode changed
    skipped   the selected mode is local_direct (traffic never takes the relay)
    error     the last fetch failed, or its page had none of the rule fields,
              for fewer than `error_checks` checks in a row (the mismatch count is kept)
    failing   that, for `error_checks` checks in a row (the mismatch count is still kept)
    match     the observed exit is the selected mode
    pending   mismatching, but for fewer than `mismatch_checks` checks in a row
    mismatch  mismatching for `mismatch_checks` checks in a row

    A check that works (match, pending or mismatch), a `skipped` and a new selected
    mode each reset the error count.
    """

    def __init__(self, mismatch_checks: int, clock=time.time, error_checks: int = 3):
        self.mismatch_checks = max(1, int(mismatch_checks))
        self.error_checks = max(1, int(error_checks))
        self._clock = clock
        self._count = 0
        self._errors = 0
        self.selected: Optional[str] = None
        self.observed: Optional[str] = None
        self.ip: Optional[str] = None
        self.error: Optional[str] = None
        self.status = "checking"
        self.since = clock()
        self.checked_at: Optional[float] = None

    def _set(self, status: str, now: float) -> None:
        if status != self.status:
            self.status = status
            self.since = now

    def select(self, mode: str) -> None:
        """Note the selected mode; a change restarts the evaluation."""
        if mode == self.selected:
            return
        self.selected = mode
        self._count = self._errors = 0
        self.observed = self.ip = self.error = None
        self._set("checking", self._clock())

    def update(self, observed: Optional[str], ip: Optional[str], error: Optional[str]) -> None:
        now = self._clock()
        self.checked_at = now
        if self.selected == "local_direct":
            self._count = self._errors = 0
            self.observed = self.ip = self.error = None
            self._set("skipped", now)
            return
        if error is not None:
            self.error = error
            self._errors += 1
            self._set("failing" if self._errors >= self.error_checks else "error", now)
            return
        self.error = None
        self._errors = 0
        self.observed = observed
        self.ip = ip[:_IP_MAX_CHARS] if ip is not None else None   # the page is not trusted to be short
        if observed == self.selected:
            self._count = 0
            self._set("match", now)
            return
        self._count += 1
        self._set("mismatch" if self._count >= self.mismatch_checks else "pending", now)

    def snapshot(self) -> dict:
        return {"selected": self.selected, "observed": self.observed, "ip": self.ip,
                "status": self.status, "since": self.since,
                "checked_at": self.checked_at, "error": self.error}


class StopSignal(Protocol):
    """What the observer thread needs from its stop flag (a threading.Event has both)."""

    def is_set(self) -> bool: ...

    def wait(self, timeout: Optional[float] = None) -> bool: ...


class EgressObserver:
    """Runs the check on a daemon thread every `interval_s`, and `settle_s` after
    the selected mode changes. The relay applies a new mode on its next 10 s tick,
    so an immediate check would still report the old exit. Every check that follows
    a mode change waits a full settle first, including a change that lands during
    the settle. Setting the stop flag ends the thread promptly, whether it is
    settling or waiting out the interval."""

    def __init__(self, cfg: ObserveCfg, fetch=fetch_trace, clock=time.time,
                 settle_s: float = 15.0):
        self.cfg = cfg
        self.settle_s = settle_s
        self._fetch = fetch
        self._lock = threading.Lock()
        self._kick = threading.Event()
        self._tracker = ExitTracker(cfg.mismatch_checks, clock=clock,
                                    error_checks=cfg.error_checks)
        self._thread: Optional[threading.Thread] = None

    def set_selected(self, mode: str) -> None:
        with self._lock:
            changed = mode != self._tracker.selected
            self._tracker.select(mode)
        if changed:
            self._kick.set()

    def check_once(self) -> None:
        with self._lock:
            selected = self._tracker.selected
        if selected is None:
            return
        observed = ip = err = None
        if selected != "local_direct":
            try:
                body, err = self._fetch(self.cfg.url, self.cfg.iface, self.cfg.timeout_s)
            except Exception as e:  # a fetch that raises is a failed check, not a lost one
                body, err = None, f"fetch error: {e}"
            if err is None:
                fields = parse_trace(body)
                if not any(r.field in fields for r in self.cfg.exits):
                    # An error page, a redirect or an empty reply says nothing about the exit.
                    err = "trace page has none of the rule fields"
                else:
                    observed = classify(fields, self.cfg.exits)
                    ip = fields.get("ip")
        with self._lock:
            if self._tracker.selected == selected:   # the mode did not change mid-fetch
                self._tracker.update(observed, ip, err)

    def snapshot(self) -> dict:
        """The tracker's snapshot, plus the check's cadence (`error_checks`,
        `interval_s`): what a `failing` has gone without a check for, for a page."""
        with self._lock:
            snap = self._tracker.snapshot()
        snap["error_checks"], snap["interval_s"] = self.cfg.error_checks, self.cfg.interval_s
        return snap

    def _run(self, stop: StopSignal) -> None:
        while not stop.is_set():
            # A pending kick means the mode changed: let the relay apply it before
            # checking. A change that lands during the settle earns another full one.
            while self._kick.is_set():
                self._kick.clear()
                if stop.wait(self.settle_s):
                    return
            try:
                self.check_once()
            except Exception:  # the thread must outlive any one bad check
                logging.exception("egress observer: check failed")
            # A mode change cuts the wait short, and so does a stop (see _kick_on_stop).
            # Either way the loop test runs next, so a stop returns before any settle.
            self._kick.wait(self.cfg.interval_s)

    def _kick_on_stop(self, stop: StopSignal) -> None:
        """Set the kick once `stop` is set. The loop's interval wait sleeps on the
        kick, so without this a stop would wait out up to interval_s."""
        stop.wait()
        self._kick.set()

    def start(self, stop: threading.Event) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, args=(stop,),
                                        name="egress-observer", daemon=True)
        self._thread.start()
        threading.Thread(target=self._kick_on_stop, args=(stop,),
                         name="egress-observer-stop", daemon=True).start()
