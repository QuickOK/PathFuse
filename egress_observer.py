"""Actual-exit check: which egress the relay path really uses right now.

sbfd-ctl selects an egress mode and the relay enacts it, but the relay can fall
back without telling anyone: an upstream fails its health probe, or the relay
never learns the mode. This module fetches a plain-text trace page through the
tunnel, reads its `key=value` lines, classifies the exit against configured
rules, and tracks whether the observed exit matches the selected mode.

Config (`egress.observe` in the sbfd-ctl config; off while `url` is empty):

    {"url": "https://probe.example.net/trace", "iface": "wg0",
     "interval_s": 120, "timeout_s": 8, "mismatch_checks": 2,
     "exits": [{"mode": "relay_backbone", "field": "ip", "values": ["203.0.113.10"]}]}

The first rule whose `field` value appears in `values` names the observed exit.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional


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
    exits: tuple = ()


def _positive(name: str, value) -> float:
    if isinstance(value, bool):
        raise ValueError(f"egress.observe.{name} must be a number, got {value!r}")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"egress.observe.{name} must be a number, got {value!r}") from None
    if not 0 < f < float("inf"):
        raise ValueError(f"egress.observe.{name} must be finite and > 0, got {value!r}")
    return f


def parse_observe_cfg(raw, valid_modes) -> Optional[ObserveCfg]:
    """ObserveCfg from the raw `egress.observe` block. None when the block is
    absent or `url` is empty (the check is off). ValueError on anything malformed."""
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
    checks = raw.get("mismatch_checks", 2)
    if isinstance(checks, bool) or not isinstance(checks, int) or checks < 1:
        raise ValueError(f"egress.observe.mismatch_checks must be an integer >= 1, got {checks!r}")
    rules = []
    for i, r in enumerate(raw.get("exits") or []):
        if not isinstance(r, dict):
            raise ValueError(f"egress.observe.exits[{i}] must be an object")
        mode, field, values = r.get("mode"), r.get("field"), r.get("values")
        if mode not in valid_modes:
            raise ValueError(f"egress.observe.exits[{i}].mode must be one of {sorted(valid_modes)}")
        if not isinstance(field, str) or not field:
            raise ValueError(f"egress.observe.exits[{i}].field must be a non-empty string")
        if (not isinstance(values, list) or not values
                or not all(isinstance(v, str) and v for v in values)):
            raise ValueError(f"egress.observe.exits[{i}].values must be a non-empty list of strings")
        rules.append(ExitRule(mode=mode, field=field, values=tuple(values)))
    return ObserveCfg(url=url, iface=iface,
                      interval_s=_positive("interval_s", raw.get("interval_s", 120)),
                      timeout_s=_positive("timeout_s", raw.get("timeout_s", 8)),
                      mismatch_checks=checks, exits=tuple(rules))


def parse_trace(body: str) -> dict:
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
    """
    argv = ["curl", "--silent", "--show-error", "--max-time", f"{timeout_s:g}",
            "--interface", iface, url]
    try:
        r = runner(argv, capture_output=True, text=True, timeout=timeout_s + 3)
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
    error     the last fetch failed (the mismatch count is kept)
    match     the observed exit is the selected mode
    pending   mismatching, but for fewer than `mismatch_checks` checks in a row
    mismatch  mismatching for `mismatch_checks` checks in a row
    """

    def __init__(self, mismatch_checks: int, clock=time.time):
        self.mismatch_checks = max(1, int(mismatch_checks))
        self._clock = clock
        self._count = 0
        self.selected = None
        self.observed = None
        self.ip = None
        self.error = None
        self.status = "checking"
        self.since = clock()
        self.checked_at = None

    def _set(self, status: str, now: float) -> None:
        if status != self.status:
            self.status = status
            self.since = now

    def select(self, mode: str) -> None:
        """Note the selected mode; a change restarts the evaluation."""
        if mode == self.selected:
            return
        self.selected = mode
        self._count = 0
        self.observed = self.ip = self.error = None
        self._set("checking", self._clock())

    def update(self, observed: Optional[str], ip: Optional[str], error: Optional[str]) -> None:
        now = self._clock()
        self.checked_at = now
        if self.selected == "local_direct":
            self._count = 0
            self.observed = self.ip = self.error = None
            self._set("skipped", now)
            return
        if error is not None:
            self.error = error
            self._set("error", now)
            return
        self.error = None
        self.observed, self.ip = observed, ip
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


class EgressObserver:
    """Runs the check on a daemon thread every `interval_s`, and `settle_s` after
    the selected mode changes. The relay applies a new mode on its next 10 s tick,
    so an immediate check would still report the old exit."""

    def __init__(self, cfg: ObserveCfg, fetch=fetch_trace, clock=time.time,
                 settle_s: float = 15.0):
        self.cfg = cfg
        self.settle_s = settle_s
        self._fetch = fetch
        self._lock = threading.Lock()
        self._kick = threading.Event()
        self._tracker = ExitTracker(cfg.mismatch_checks, clock=clock)
        self._thread = None

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
            body, err = self._fetch(self.cfg.url, self.cfg.iface, self.cfg.timeout_s)
            if err is None:
                fields = parse_trace(body)
                observed = classify(fields, self.cfg.exits)
                ip = fields.get("ip")
        with self._lock:
            if self._tracker.selected == selected:   # the mode did not change mid-fetch
                self._tracker.update(observed, ip, err)

    def snapshot(self) -> dict:
        with self._lock:
            return self._tracker.snapshot()

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.check_once()
            except Exception:  # the thread must outlive any one bad check
                logging.exception("egress observer: check failed")
            if self._kick.wait(self.cfg.interval_s):
                self._kick.clear()
                stop.wait(self.settle_s)

    def start(self, stop: threading.Event) -> None:
        self._thread = threading.Thread(target=self._run, args=(stop,),
                                        name="egress-observer", daemon=True)
        self._thread.start()
