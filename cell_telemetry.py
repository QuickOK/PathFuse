#!/usr/bin/env python3
"""cell_telemetry.py — wan1 modem signal telemetry collector for PathFuse.

Polls the Nighthawk M6 Pro admin API (model.json) and atomically publishes
RSRP/RSRQ/SINR/cell-ID/band to a state file sbfd-ctl reads fail-open (same
pattern as environ_ctl's auto-override). Only successful polls write; the
file simply going stale IS the failure signal — consumers apply a TTL.
Spec: docs/superpowers/specs/2026-07-21-cellular-adaptive-fec-design.md
"""
import argparse
import json
import logging
import os
import re
import signal
import threading
import time
from dataclasses import dataclass
from typing import Optional

import netgear_api

log = logging.getLogger("cell_telemetry")

# model.json layouts vary by firmware; each metric lists candidate paths in
# preference order. P1 field discovery trims this to what the live device
# actually serves.
CANDIDATE_PATHS = {
    "rsrp":    [("wwan", "signalStrength", "rsrp"), ("wwanadv", "rsrp")],
    "rsrq":    [("wwan", "signalStrength", "rsrq"), ("wwanadv", "rsrq")],
    "sinr":    [("wwan", "signalStrength", "sinr"), ("wwanadv", "sinr")],
    "cell_id": [("wwanadv", "cellId"), ("wwan", "cellId")],
    "band":    [("wwanadv", "curBand"), ("wwan", "band")],
}
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?")


def _dig(obj, path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _num(v):
    """Coerce a metric to float: numbers pass through, strings may carry a
    unit suffix ('-105dBm'). bool is an int subclass and is junk here."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = _NUM_RE.match(v.strip())
        if m:
            return float(m.group(0))
    return None


def _text(v):
    """Coerce a metric to a plain identity string: bool and anything that
    isn't str/int/float (dicts, lists, ...) is junk — a wrong-shape or
    unauthenticated model.json can nest an object/array where a scalar cell
    ID or band is expected, and str()'ing that would publish garbage AND
    make the reading count as 'signal present', blocking the login
    fallback in read_signal."""
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        return None
    return str(v).strip() or None


def extract_signal(model):
    """Best-effort pull of the five metrics. Keys always present; values None
    when absent/unparseable. Never raises."""
    out = {}
    for metric, paths in CANDIDATE_PATHS.items():
        val = None
        for path in paths:
            raw = _dig(model, path)
            if raw is None:
                continue
            val = _num(raw) if metric in ("rsrp", "rsrq", "sinr") else _text(raw)
            if val is not None:
                break
        out[metric] = val
    return out


def atomic_write_json(path, obj):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


@dataclass
class CtCfg:
    admin_url: str
    iface: str = "wan1"
    cookie_jar: str = "/run/cell-telemetry/cookies.txt"
    secret_path: Optional[str] = None
    state_path: str = "/run/sbfd-ctl/cell_telemetry.json"
    poll_interval_s: float = 2.0
    login_backoff_s: float = 60.0
    handoff_path: str = "/run/sbfd-ctl/cell_handoff.json"
    handoff_window_s: float = 4.0
    handoff_min_interval_s: float = 15.0
    handoff_rsrq_drop_db: float = 4.0
    handoff_loss_spike_pct: float = 2.0
    sbfd_state_path: str = "/run/sbfd/state.json"
    handoff_enabled: bool = True


def load_config(path):
    with open(path) as f:
        raw = json.load(f)
    h = raw.get("handoff") or {}
    cfg = CtCfg(
        admin_url=raw["admin_url"],
        iface=raw.get("iface", "wan1"),
        cookie_jar=raw.get("cookie_jar", "/run/cell-telemetry/cookies.txt"),
        secret_path=raw.get("secret_path"),
        state_path=raw.get("state_path", "/run/sbfd-ctl/cell_telemetry.json"),
        poll_interval_s=float(raw.get("poll_interval_s", 2.0)),
        login_backoff_s=float(raw.get("login_backoff_s", 60.0)),
        handoff_enabled=bool(h.get("enabled", True)),
        handoff_path=h.get("path", "/run/sbfd-ctl/cell_handoff.json"),
        handoff_window_s=float(h.get("window_s", 4.0)),
        handoff_min_interval_s=float(h.get("min_interval_s", 15.0)),
        handoff_rsrq_drop_db=float(h.get("rsrq_drop_db", 4.0)),
        handoff_loss_spike_pct=float(h.get("loss_spike_pct", 2.0)),
        sbfd_state_path=h.get("sbfd_state_path", "/run/sbfd/state.json"),
    )
    if cfg.poll_interval_s <= 0:
        raise ValueError("poll_interval_s must be > 0")
    if cfg.login_backoff_s < 0:
        raise ValueError("login_backoff_s must be >= 0")
    if cfg.handoff_window_s <= 0:
        raise ValueError("handoff.window_s must be > 0")
    if cfg.handoff_min_interval_s < cfg.handoff_window_s:
        raise ValueError("handoff.min_interval_s must be >= window_s")
    if cfg.handoff_rsrq_drop_db <= 0:
        raise ValueError("handoff.rsrq_drop_db must be > 0")
    if cfg.handoff_loss_spike_pct <= 0:
        raise ValueError("handoff.loss_spike_pct must be > 0")
    return cfg


def read_signal(client):
    """One fetch+extract. None when unreachable or NO metric was found —
    all-None means an unauthenticated (or wrong-shape) response, and is the
    trigger for the login fallback."""
    model = client.fetch_model()
    if model is None:
        return None
    reading = extract_signal(model)
    return reading if any(v is not None for v in reading.values()) else None


def _read_secret(path):
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError as e:
        log.warning("admin secret unreadable (%s): %s", path, e)
        return None


def poll_once(client, cfg, now, last_login_ts, *, detector=None, wan_loss=None):
    """One poll. Writes the state file on success (only then — a stale file is
    the downstream failure signal). Falls back to an admin login at most once
    per login_backoff_s when the unauthenticated read carries no signal."""
    reading = read_signal(client)
    if reading is None and cfg.secret_path and (
            last_login_ts is None or now - last_login_ts >= cfg.login_backoff_s):
        last_login_ts = now
        password = _read_secret(cfg.secret_path)
        if password and client.login(password):
            reading = read_signal(client)
        else:
            log.warning("login fallback failed; will retry after backoff")
    if reading is not None:
        try:
            atomic_write_json(cfg.state_path, {**reading, "set_ts": now})
        except OSError as e:
            log.warning("state write failed (%s): %s", cfg.state_path, e)
    if detector is not None:
        reason = detector.update(reading, wan_loss, now)
        if reason is not None:
            try:
                write_handoff(cfg.handoff_path, now, cfg.handoff_window_s, reason)
                log.info("handoff window opened (%s)", reason)
            except OSError as e:
                log.warning("handoff write failed (%s): %s", cfg.handoff_path, e)
    return reading, last_login_ts


def read_wan_loss(state_path, wan="wan1", max_age_s=10.0, now=None):
    """wan's loss_pct from the local sbfd state file, or None (fail-open).
    RX-side loss (relay->client), used only as the REACTIVE handoff fallback —
    a burst big enough to matter shows up here within a second.

    sbfd.py's write_state_file keys "sessions" by session NAME, not by a
    "wan" field -- there is no such field. Match the session name against
    `wan` first (the common case: sessions named after their wan), and fall
    back to the session's "iface" field for one named something else.

    Staleness gate (CodeRabbit PR#5 CR2): a frozen sbfd state file (sbfd died
    mid-spike, or the write path wedged) would otherwise keep reporting its
    last loss_pct forever, opening a duplication window every poll while the
    gated WAN stays "active". sbfd.py's write_state_file publishes a
    top-level "timestamp" (now_s()) refreshed on every tick — gate on THAT
    real schema field, not a guessed one. Fall back to the file's own mtime
    only if the payload doesn't carry a usable timestamp (missing/wrong
    type/non-dict root). max_age_s=None disables the gate entirely."""
    now = time.time() if now is None else now
    try:
        with open(state_path) as f:
            raw = json.load(f)
        if max_age_s is not None:
            ts = raw.get("timestamp") if isinstance(raw, dict) else None
            if not (isinstance(ts, (int, float)) and not isinstance(ts, bool)):
                ts = os.stat(state_path).st_mtime
            if now - ts > max_age_s:
                return None
        for name, s in (raw.get("sessions") or {}).items():
            if isinstance(s, dict) and (name == wan or s.get("iface") == wan):
                loss = s.get("loss_pct")
                if isinstance(loss, (int, float)) and not isinstance(loss, bool):
                    return float(loss)
    except (OSError, ValueError, TypeError, AttributeError):
        # TypeError/AttributeError: a non-dict JSON root (list, string,
        # number, null...) has no .get/.items -- fail open rather than
        # escape to the poll loop's log.exception spam.
        return None
    return None


class HandoffDetector:
    """Detects an imminent/in-progress tower handoff from consecutive telemetry
    samples and opens rate-limited duplication windows.

    Pair validity: cell-change and RSRQ-delta triggers compare CONSECUTIVE
    successful polls only — a gap longer than 2x the poll interval (modem
    reboot, nightly maintenance) invalidates the pair, so waking up on a new
    tower after an outage never opens a window. The loss-spike fallback needs
    no pair (it is already a smoothed measurement).

    Rate limit: at most one window per min_interval_s, measured open-to-open;
    triggers inside an open window are ignored, never extended (spec §5)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._prev = None            # (ts, cell_id, rsrq)
        self._last_open_ts = None
        self._until_ts = 0.0

    def _rate_limited(self, now):
        if now < self._until_ts:
            return True              # window still open: ignore, don't extend
        return (self._last_open_ts is not None
                and now - self._last_open_ts < self.cfg.handoff_min_interval_s)

    def _open(self, now):
        self._last_open_ts = now
        self._until_ts = now + self.cfg.handoff_window_s

    def update(self, reading, wan_loss, now):
        """One tick. Returns a reason string when a duplication window should
        open, else None. reading may be None (modem unreachable) — the loss
        fallback still runs."""
        if not self.cfg.handoff_enabled:
            self._prev = None
            return None
        prev = self._prev
        reason = None
        if reading is not None:
            cell, rsrq = reading.get("cell_id"), reading.get("rsrq")
            fresh_pair = (prev is not None and
                          now - prev[0] <= 2 * self.cfg.poll_interval_s)
            if fresh_pair and cell is not None and prev[1] is not None \
                    and cell != prev[1]:
                reason = f"cell_change:{prev[1]}->{cell}"
            elif fresh_pair and rsrq is not None and prev[2] is not None \
                    and (prev[2] - rsrq) > self.cfg.handoff_rsrq_drop_db:
                reason = f"rsrq_drop:{prev[2]}->{rsrq}"
            self._prev = (now, cell, rsrq)
        if reason is None and wan_loss is not None \
                and wan_loss >= self.cfg.handoff_loss_spike_pct:
            reason = f"loss_spike:{wan_loss}"
        if reason is None or self._rate_limited(now):
            return None
        self._open(now)
        return reason


def write_handoff(path, now, window_s, reason):
    atomic_write_json(path, {"set_ts": now, "until_ts": now + window_s,
                             "reason": reason})


def run(cfg, stop_event=None):
    if stop_event is None:
        stop_event = threading.Event()
    client = netgear_api.NetgearClient(cfg.admin_url, cfg.iface, cfg.cookie_jar)
    last_login_ts = None
    detector = HandoffDetector(cfg)
    while not stop_event.is_set():
        try:
            now = time.time()
            wan_loss = read_wan_loss(cfg.sbfd_state_path, cfg.iface, now=now)
            _, last_login_ts = poll_once(client, cfg, now, last_login_ts,
                                         detector=detector, wan_loss=wan_loss)
        except Exception:  # noqa: BLE001 — a poll must never kill the daemon
            log.exception("poll failed")
        stop_event.wait(cfg.poll_interval_s)


def main():
    ap = argparse.ArgumentParser(description="modem signal telemetry collector")
    ap.add_argument("-c", "--config", required=True)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run(cfg, stop)


if __name__ == "__main__":
    main()
