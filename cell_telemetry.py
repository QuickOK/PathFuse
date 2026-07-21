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
            val = _num(raw) if metric in ("rsrp", "rsrq", "sinr") else str(raw)
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


def load_config(path):
    with open(path) as f:
        raw = json.load(f)
    cfg = CtCfg(
        admin_url=raw["admin_url"],
        iface=raw.get("iface", "wan1"),
        cookie_jar=raw.get("cookie_jar", "/run/cell-telemetry/cookies.txt"),
        secret_path=raw.get("secret_path"),
        state_path=raw.get("state_path", "/run/sbfd-ctl/cell_telemetry.json"),
        poll_interval_s=float(raw.get("poll_interval_s", 2.0)),
        login_backoff_s=float(raw.get("login_backoff_s", 60.0)),
    )
    if cfg.poll_interval_s <= 0:
        raise ValueError("poll_interval_s must be > 0")
    if cfg.login_backoff_s < 0:
        raise ValueError("login_backoff_s must be >= 0")
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


def poll_once(client, cfg, now, last_login_ts):
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
        atomic_write_json(cfg.state_path, {**reading, "set_ts": now})
    return reading, last_login_ts


def run(cfg, stop_event=None):
    if stop_event is None:
        stop_event = threading.Event()
    client = netgear_api.NetgearClient(cfg.admin_url, cfg.iface, cfg.cookie_jar)
    last_login_ts = None
    while not stop_event.is_set():
        try:
            _, last_login_ts = poll_once(client, cfg, time.time(), last_login_ts)
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
