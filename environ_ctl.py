#!/usr/bin/env python3
"""
environ_ctl.py — environmental redundancy controller for PathFuse.

Polls on-route environmental hazards (precipitation, wildfire smoke) at the
client's current GPS position and a course-projected look-ahead point, and writes
a small auto-override heartbeat that sbfd-ctl reads to raise the link to full
redundancy ahead of a hazard:

    HAZARD -> force_full=true   (both WANs hot; protect against link fade)
    CLEAR  -> force_full=false  (let operator/default policy stand; conserve)

Directionality (the look-ahead bearing) comes from the client's gpsd course over
ground; every signal uses the client's travel heading (not wind direction).

Stdlib only: gpsd over its TCP JSON protocol, urllib for HTTP. The poll loop
never dies on a transient error; it degrades to the fail-safe path.
"""

import argparse
import json
import logging
import math
import os
import signal
import socket
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("environ_ctl")


# -- Geometry ----------------------------------------------------------------

def project(lat, lon, bearing_deg, dist_m):
    """Forward geodesic: the point dist_m from (lat,lon) along bearing_deg."""
    R = 6371000.0
    br = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    dr = dist_m / R
    lat2 = math.asin(math.sin(lat1) * math.cos(dr) +
                     math.cos(lat1) * math.sin(dr) * math.cos(br))
    lon2 = lon1 + math.atan2(math.sin(br) * math.sin(dr) * math.cos(lat1),
                             math.cos(dr) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)


def build_points(fix, lookahead_s, min_speed_ms):
    """[current] plus a course-projected look-ahead point when moving fast enough
    to trust gpsd's track. fix = (lat, lon, speed_ms, track_deg)."""
    lat, lon, speed, track = fix
    points = [(lat, lon)]
    if lookahead_s > 0 and track is not None and speed >= min_speed_ms:
        points.append(project(lat, lon, track, speed * lookahead_s))
    return points


# -- Per-signal hysteresis / debounce ----------------------------------------

class SignalController:
    """Hysteretic, debounced hazard detector for one environmental signal.
    Separate ON/OFF thresholds (the hysteresis band) plus N consecutive
    confirmations each way prevent flapping through scattered cells / API jitter.
    """

    def __init__(self, name, on_thresh, off_thresh, wet_confirm, dry_confirm, reason):
        self.name = name
        self.on_thresh = on_thresh
        self.off_thresh = off_thresh
        self.wet_confirm = wet_confirm
        self.dry_confirm = dry_confirm
        self.reason = reason
        self.hazard = False
        self.wet_streak = 0
        self.dry_streak = 0

    def update(self, value) -> bool:
        is_wet = value >= self.on_thresh
        is_dry = value <= self.off_thresh
        if is_wet:
            self.wet_streak += 1
            self.dry_streak = 0
        elif is_dry:
            self.dry_streak += 1
            self.wet_streak = 0
        else:  # in the hysteresis band: reset streaks — confirmation must be consecutive
            self.wet_streak = 0
            self.dry_streak = 0

        if not self.hazard and self.wet_streak >= self.wet_confirm:
            self.hazard = True
            self.wet_streak = self.dry_streak = 0
        elif self.hazard and self.dry_streak >= self.dry_confirm:
            self.hazard = False
            self.wet_streak = self.dry_streak = 0
        return self.hazard


def classify_codes(vals, hazard_codes):
    """Map raw categorical codes (e.g. WMO weather_code) to a binary hazard score:
    1.0 when the code is in hazard_codes, else 0.0. This lets a categorical signal
    feed the numeric SignalController unchanged — run it with on_thresh=1.0,
    off_thresh=0.0 so the result never lands in the hysteresis band and debounce
    (wet_confirm/dry_confirm) still governs flap protection. Non-integer / junk
    values map to 0.0 (clear) rather than raising."""
    out = []
    for v in vals:
        try:
            out.append(1.0 if int(v) in hazard_codes else 0.0)
        except (TypeError, ValueError):
            out.append(0.0)
    return out


def combine_hazard(controllers):
    """OR the signal controllers. Returns (force_full, reason) where reason names
    the signals currently in hazard, joined by '; '."""
    active = [c.reason for c in controllers if c.hazard]
    return (len(active) > 0, "; ".join(active))


def build_override_record(force_full, reason, now, source="environ_ctl"):
    return {"force_full": bool(force_full), "source": source,
            "reason": reason, "set_ts": now}


# -- Environmental data sources (Open-Meteo, keyless) ------------------------

HTTP_TIMEOUT = 10.0


def parse_open_meteo(data, current_field):
    """Normalize an Open-Meteo response (object for one point, array for many)
    into a list of floats for `current.<current_field>`, same order as points."""
    if isinstance(data, dict):
        data = [data]
    out = []
    for d in data:
        out.append(float(d.get("current", {}).get(current_field, 0.0) or 0.0))
    return out


def fetch_open_meteo(points, url, current_field, timeout=HTTP_TIMEOUT):
    """Query an Open-Meteo endpoint for `current_field` at each point. May raise
    (callers in the loop catch and hold the signal's last state)."""
    lats = ",".join(f"{p[0]:.4f}" for p in points)
    lons = ",".join(f"{p[1]:.4f}" for p in points)
    qs = urllib.parse.urlencode({
        "latitude": lats, "longitude": lons,
        "current": current_field, "timezone": "UTC",
    })
    req = urllib.request.Request(f"{url}?{qs}",
                                 headers={"User-Agent": "environ-ctl/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return parse_open_meteo(data, current_field)


def write_override(path, record):
    """Atomically write the auto-override record (tmp + os.replace)."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(record, f)
    os.replace(tmp, path)


# -- GPS ---------------------------------------------------------------------

def get_fix(host, port, timeout=5.0):
    """Return (lat, lon, speed_ms, track_deg) from gpsd, or None on no fix.
    Opens a fresh gpsd connection each call (stateless; robust at minute cadence).
    """
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        log.warning("gpsd connect failed: %s", e)
        return None
    try:
        s.settimeout(timeout)
        s.sendall(b'?WATCH={"enable":true,"json":true}\n')
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("class") != "TPV" or obj.get("mode", 0) < 2:
                    continue
                lat, lon = obj.get("lat"), obj.get("lon")
                if lat is None or lon is None:
                    continue
                speed = obj.get("speed", 0.0) or 0.0
                track = obj.get("track")
                return (lat, lon, float(speed), track)
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass


# -- Config ------------------------------------------------------------------

@dataclass
class SignalSpec:
    controller: SignalController
    url: str
    current_field: str
    hazard_codes: object = None  # optional set[int]; categorical (code-membership) signal


@dataclass
class EnvConfig:
    poll_interval_s: float
    lookahead_s: float
    min_speed_ms: float
    max_stale_s: float
    gpsd_host: str
    gpsd_port: int
    auto_override_path: str
    signals: list


def load_env_config(path) -> EnvConfig:
    with open(path) as f:
        raw = json.load(f)
    signals = []
    for name, s in raw.get("signals", {}).items():
        if not s.get("enabled", True):
            continue
        hc = s.get("hazard_codes")
        signals.append(SignalSpec(
            controller=SignalController(
                name=name,
                on_thresh=float(s["on_thresh"]),
                off_thresh=float(s["off_thresh"]),
                wet_confirm=int(s.get("wet_confirm", 1)),
                dry_confirm=int(s.get("dry_confirm", 2)),
                reason=s.get("reason", name),
            ),
            url=s["url"],
            current_field=s["current_field"],
            hazard_codes=set(int(c) for c in hc) if hc is not None else None,
        ))
    g = raw.get("gpsd", {})
    return EnvConfig(
        poll_interval_s=float(raw.get("poll_interval_s", 60)),
        lookahead_s=float(raw.get("lookahead_s", 300)),
        min_speed_ms=float(raw.get("min_speed_ms", 2.0)),
        max_stale_s=float(raw.get("max_stale_s", 600)),
        gpsd_host=g.get("host", "127.0.0.1"),
        gpsd_port=int(g.get("port", 2947)),
        auto_override_path=raw["auto_override"]["path"],
        signals=signals,
    )


# -- Main loop ---------------------------------------------------------------

_running = True


def _stop(signum, frame):
    global _running
    _running = False


def poll_once(cfg: EnvConfig, last_good_mono: float, now_mono: float) -> float:
    """One poll cycle. Updates signal controllers and writes the override.
    Returns the (possibly updated) last_good_mono. Fail-safe: if no signal can be
    evaluated for longer than max_stale_s, write force_full=false. The only
    exception it may propagate is OSError from write_override, which main()
    catches; transient fetch/GPS errors are handled internally."""
    fix = get_fix(cfg.gpsd_host, cfg.gpsd_port)
    evaluated = False
    if fix is not None:
        points = build_points(fix, cfg.lookahead_s, cfg.min_speed_ms)
        for spec in cfg.signals:
            try:
                vals = fetch_open_meteo(points, spec.url, spec.current_field)
                if spec.hazard_codes is not None:
                    vals = classify_codes(vals, spec.hazard_codes)
                spec.controller.update(max(vals) if vals else 0.0)
                evaluated = True
            except Exception as e:  # noqa: BLE001 - hold this signal's last state
                log.warning("fetch %s failed: %s", spec.controller.name, e)
    else:
        log.warning("no GPS fix")

    if evaluated:
        last_good_mono = now_mono
        force_full, reason = combine_hazard([s.controller for s in cfg.signals])
        write_override(cfg.auto_override_path,
                       build_override_record(force_full, reason, time.time()))
        log.info("override force_full=%s reason=%r", force_full, reason)
    elif now_mono - last_good_mono > cfg.max_stale_s:
        write_override(cfg.auto_override_path,
                       build_override_record(False, "stale: no data", time.time()))
        log.warning("data stale > %ss, wrote force_full=false", cfg.max_stale_s)
    return last_good_mono


def _parse_args(argv):
    ap = argparse.ArgumentParser(prog="environ_ctl.py",
                                 description="PathFuse environmental redundancy controller")
    ap.add_argument("-c", "--config", required=True,
                    help="path to the environmental config JSON")
    return ap.parse_args(argv)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    args = _parse_args(sys.argv[1:])
    cfg = load_env_config(args.config)
    last_good = time.monotonic()
    while _running:
        cycle_start = time.monotonic()
        try:
            last_good = poll_once(cfg, last_good, cycle_start)
        except Exception as e:  # noqa: BLE001 - keep the daemon alive
            log.error("poll error: %s", e)
        end = time.monotonic() + max(5.0, cfg.poll_interval_s - (time.monotonic() - cycle_start))
        while _running and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))
    log.info("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
