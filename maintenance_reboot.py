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
    if not isinstance(raw, dict):
        return None
    ts = raw.get("ts", raw.get("timestamp"))
    if not isinstance(ts, (int, float)) or abs(now - ts) > max_age_s:
        return None
    return raw


def should_run(published: dict, now_local_hour: int) -> tuple:
    """(ok, reason). The timer fires hourly; this is the gate that makes it
    daily, so the hour can move from the UI without a daemon-reload."""
    m = (published or {}).get("maintenance")
    if m is None:
        m = {}
    if not isinstance(m, dict):
        return False, f"published maintenance is not an object ({m!r})"
    if not m.get("configured"):
        return False, "maintenance reboot not configured"
    if not m.get("enabled"):
        return False, "maintenance reboot disabled by operator"
    hour = m.get("hour")
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
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
    if not isinstance(raw, dict):
        return {}
    ts = raw.get("timestamp")
    if not isinstance(ts, (int, float)) or abs(now - ts) > max_age_s:
        return {}
    sessions = raw.get("sessions")
    if not isinstance(sessions, dict):
        return {}
    out = {}
    for s in sessions.values():
        if not isinstance(s, dict):
            continue
        iface = s.get("iface")
        if not isinstance(iface, str):
            continue
        out[iface] = s.get("state", "UNKNOWN")
    return out


def peer_of(cfg: MrConfig, wan: str) -> str:
    if wan == cfg.wan1.iface:
        return cfg.wan2.iface
    if wan == cfg.wan2.iface:
        return cfg.wan1.iface
    raise ValueError(f"unrecognized WAN iface {wan!r}")


GRPC_METHOD = "SpaceX.API.Device.Device/Handle"
GRPC_TIMEOUT_S = 20


class DishClient:
    """Talks to the wan2 terminal's gRPC API by shelling out to grpcurl.

    grpcurl rather than a generated stub because the device serves protobuf
    reflection: no .proto files to vendor, nothing to re-sync when the vendor
    changes the schema, and no protobuf dependency in a stdlib-only repo."""

    def __init__(self, cfg: Wan2Cfg, runner=subprocess.run):
        self.cfg = cfg
        self._run = runner

    def _call(self, payload: dict) -> Optional[dict]:
        argv = [self.cfg.grpcurl_bin, "-plaintext",
                "-max-time", str(GRPC_TIMEOUT_S),
                "-d", json.dumps(payload),
                self.cfg.addr, GRPC_METHOD]
        try:
            r = self._run(argv, capture_output=True, text=True,
                          timeout=GRPC_TIMEOUT_S + 10)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("grpcurl failed: %s", e)
            return None
        if r.returncode != 0:
            log.warning("grpcurl rc=%s: %s", r.returncode,
                        (r.stderr or "").strip()[:200])
            return None
        try:
            return json.loads(r.stdout or "{}")
        except ValueError:
            log.warning("grpcurl returned non-JSON")
            return None

    def status(self) -> Optional[dict]:
        resp = self._call({"get_status": {}})
        if resp is None:
            return None
        return resp.get("dishGetStatus") or {}

    def bootcount(self) -> Optional[int]:
        """The reboot receipt. BFD coming back says the path recovered; only a
        bumped bootcount says the device actually rebooted."""
        st = self.status()
        if not st:
            return None
        bc = (st.get("deviceInfo") or {}).get("bootcount")
        return bc if isinstance(bc, int) else None

    def uptime_s(self) -> Optional[float]:
        st = self.status()
        if not st:
            return None
        up = (st.get("deviceState") or {}).get("uptimeS")
        return float(up) if isinstance(up, (int, float)) else None

    def update_staged(self, st: Optional[dict] = None) -> bool:
        """A firmware update is staged and waiting for a reboot to apply it.
        Note swupdateRebootReady is omitted when false (proto3), so a missing
        key means False."""
        st = self.status() if st is None else st
        if not st:
            return False
        if st.get("swupdateRebootReady") is True:
            return True
        secs = st.get("secondsUntilSwupdateRebootPossible")
        return isinstance(secs, (int, float)) and secs >= 0

    def update_in_flight(self, st: Optional[dict] = None) -> bool:
        """The device is fetching or writing firmware — do not touch it."""
        st = self.status() if st is None else st
        if not st:
            return False
        return st.get("softwareUpdateState") in ("FETCHING", "APPLYING")

    def reboot(self) -> bool:
        return self._call({"reboot": {}}) is not None

    def apply_update(self) -> bool:
        """Initiate the staged update; the device reboots as part of applying
        it. Preferred over a plain reboot when an update is staged — a plain
        reboot discards it, and the next night would find it staged again."""
        return self._call({"update": {"schedule_reboot": True}}) is not None
