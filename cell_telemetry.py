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
