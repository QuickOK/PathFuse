#!/usr/bin/env python3
"""Daily maintenance reboot for the two WANs.

Reboots wan1 (cellular hotspot, via hotspot_watchdog's admin-API client) and
then wan2 (satellite terminal, via grpcurl), sequentially and only ever one at
a time, so at least one WAN is up by construction. Fired hourly by a systemd
timer; exits immediately unless the current local hour is the operator's
configured hour, which lets the schedule be changed from the UI without
rewriting the unit.

Silent on a normal night: while a leg is in flight it publishes a maintenance
window that sbfd-ctl reads to suppress that WAN's alerts. It only speaks when a
WAN fails to come back."""
import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("maintenance-reboot")

DEFAULT_NOTIFY = "/usr/local/sbin/spool-notify"
PUBLISHED_MAX_AGE_S = 60.0


@dataclass
class Wan1Cfg:
    iface: str
    watchdog_bin: str
    watchdog_config: str


@dataclass
class Wan2Cfg:
    iface: str
    grpcurl_bin: str
    addr: str
    min_uptime_s: float


@dataclass
class MrConfig:
    published_state: str
    sbfd_state_path: str
    window_path: str
    wan1: Wan1Cfg
    wan2: Wan2Cfg
    recovery_deadline_s: float
    settle_s: float
    notify_bin: str
    notify_topic: str
    dry_run: bool


def load_config(path: str) -> MrConfig:
    with open(path) as f:
        raw = json.load(f)
    w1, w2 = raw["wan1"], raw["wan2"]
    cfg = MrConfig(
        published_state=raw.get("published_state", "/run/sbfd-ctl/state.json"),
        sbfd_state_path=raw.get("sbfd_state_path", "/run/sbfd/state.json"),
        window_path=raw.get("window_path",
                            "/run/sbfd-ctl/maintenance_window.json"),
        wan1=Wan1Cfg(iface=w1.get("iface", "wan1"),
                     watchdog_bin=w1["watchdog_bin"],
                     watchdog_config=w1["watchdog_config"]),
        wan2=Wan2Cfg(iface=w2.get("iface", "wan2"),
                     grpcurl_bin=w2.get("grpcurl_bin",
                                        "/usr/local/bin/grpcurl"),
                     addr=w2["addr"],
                     min_uptime_s=float(w2.get("min_uptime_s", 43200))),
        recovery_deadline_s=float(raw.get("recovery_deadline_s", 600)),
        settle_s=float(raw.get("settle_s", 30)),
        notify_bin=raw.get("notify_bin", DEFAULT_NOTIFY),
        notify_topic=raw.get("notify_topic", "pathfuse"),
        dry_run=bool(raw.get("dry_run", True)),
    )
    for k in ("recovery_deadline_s", "settle_s"):
        if getattr(cfg, k) <= 0:
            raise ValueError(f"{k} must be > 0")
    if cfg.wan2.min_uptime_s < 0:
        raise ValueError("wan2.min_uptime_s must be >= 0")
    return cfg


def read_published(path: str, now: float,
                   max_age_s: float = PUBLISHED_MAX_AGE_S) -> Optional[dict]:
    """sbfd-ctl's published state, or None if absent/stale/unparseable.

    Fail-safe: a schedule we cannot confirm is current is not a licence to
    reboot, so every failure mode here means "skip tonight"."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    ts = raw.get("ts") or raw.get("timestamp")
    if not isinstance(ts, (int, float)) or now - ts > max_age_s:
        return None
    return raw


def should_run(published: dict, now_local_hour: int) -> tuple:
    """(ok, reason). The timer fires hourly; this is the gate that makes it
    daily, so the hour can move from the UI without a daemon-reload."""
    m = (published or {}).get("maintenance") or {}
    if not m.get("configured"):
        return False, "maintenance reboot not configured"
    if not m.get("enabled"):
        return False, "maintenance reboot disabled by operator"
    hour = m.get("hour")
    if not isinstance(hour, int) or not 0 <= hour <= 23:
        return False, f"published hour is not valid ({hour!r})"
    if now_local_hour != hour:
        return False, f"not the configured hour ({now_local_hour} != {hour})"
    return True, f"hour {hour}"


def read_wan_states(path: str, now: float, max_age_s: float = 30.0) -> dict:
    """iface -> BFD state. Empty dict when the file is missing or stale, which
    every caller must treat as "not UP"."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    ts = raw.get("timestamp")
    if not isinstance(ts, (int, float)) or now - ts > max_age_s:
        return {}
    sessions = raw.get("sessions")
    if not isinstance(sessions, dict):
        return {}
    out = {}
    for s in sessions.values():
        if isinstance(s, dict) and "iface" in s:
            out[s["iface"]] = s.get("state", "UNKNOWN")
    return out


def peer_of(cfg: MrConfig, wan: str) -> str:
    return cfg.wan2.iface if wan == cfg.wan1.iface else cfg.wan1.iface
