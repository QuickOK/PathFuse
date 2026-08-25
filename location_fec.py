#!/usr/bin/env python3
"""location_fec.py — location-aware FEC floors for PathFuse.

Learns which places degrade which WAN, and publishes a raise-only FEC level per
WAN so sbfd-ctl can lift the floor BEFORE the vehicle arrives — the adaptive
engine can only react after loss has already been felt. Manual zones pin a
minimum level to a named place; learned tiles and zones combine by max.

Never lowers anything, never writes a FIFO, never posts to the relay: it
publishes one number per WAN and sbfd-ctl decides what to do with it.
Spec: docs/superpowers/specs/2026-08-25-location-aware-fec-design.md
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field

import environ_ctl
import fec_control
import station_tracker
import tile_store

log = logging.getLogger("location_fec")


def candidate_points(fix, lookahead_s, min_speed_ms, sample_step_m):
    """The current position plus the positions we are about to occupy.

    Below the speed gate, or with no course over ground, this is the current
    point alone: a parked vehicle has no heading to project, and projecting a
    stale one would raise the floor for a road we are not on."""
    lat, lon, speed, track = fix[0], fix[1], fix[2], fix[3]
    points = [(lat, lon)]
    if speed is None or track is None or speed < min_speed_ms:
        return points
    horizon_m = speed * lookahead_s
    step = max(1.0, float(sample_step_m))
    dist = step
    while dist <= horizon_m + 1e-9:
        points.append(environ_ctl.project(lat, lon, track, dist))
        dist += step
    if len(points) == 1:
        # Horizon shorter than one step: still probe it, or a slow crawl toward
        # a bad spot would get no look-ahead at all.
        points.append(environ_ctl.project(lat, lon, track, horizon_m))
    return points


def candidate_tiles(points, precision):
    """Distinct tiles under those points, nearest first."""
    out = []
    for lat, lon in points:
        tile = tile_store.encode(lat, lon, precision)
        if tile not in out:
            out.append(tile)
    return out


def zone_terms(zones, points, wans):
    """(levels, labels, suppressed) contributed by operator zones.

    A zone matches on the current position OR any look-ahead point, so a manual
    zone leads arrival on the same terms a learned tile does."""
    levels = {w: 0 for w in wans}
    labels = {w: "" for w in wans}
    suppressed = set()
    for zone in zones or []:
        if not any(station_tracker.haversine_m(zone["lat"], zone["lon"], lat, lon)
                   <= zone["radius_m"] for lat, lon in points):
            continue
        targets = zone.get("wans") or wans
        for wan in wans:
            if wan not in targets:
                continue
            if zone["level"] > levels[wan]:
                levels[wan] = zone["level"]
                labels[wan] = zone.get("label") or "zone"
            if zone.get("suppress_learned"):
                suppressed.add(wan)
    return levels, labels, suppressed


def learned_terms(store, tiles, wans, table, suppressed=()):
    """(levels, sources) contributed by the learned store, worst tile wins."""
    levels = {w: 0 for w in wans}
    sources = {w: "" for w in wans}
    for wan in wans:
        if wan in suppressed:
            continue
        for tile in tiles:
            level = store.level_for(tile, wan, table)
            if level > levels[wan]:
                levels[wan] = level
                sources[wan] = f"{tile} ({store.passes_for(tile, wan)} passes)"
    return levels, sources


def resolve(store, fix, zones, wans, table, *, precision, lookahead_s,
            min_speed_ms, sample_step_m):
    """{wan: {level, reason}} for this position. Raise-only by construction:
    every term is a max and the floor of the range is 0 (no opinion)."""
    points = candidate_points(fix, lookahead_s, min_speed_ms, sample_step_m)
    tiles = candidate_tiles(points, precision)
    manual, labels, suppressed = zone_terms(zones, points, wans)
    learned, sources = learned_terms(store, tiles, wans, table, suppressed)
    top = len(table) - 1
    out = {}
    for wan in wans:
        level = min(max(learned[wan], manual[wan]), top)
        if level <= 0:
            out[wan] = {"level": 0, "reason": ""}
        elif manual[wan] >= learned[wan]:
            out[wan] = {"level": level, "reason": f"zone {labels[wan]}"}
        else:
            out[wan] = {"level": level, "reason": f"learned {sources[wan]}"}
    return out


class ExitHold:
    """Holds a level for hold_s after the tile that produced it drops out.

    A geohash cell has a hard edge; a bad spot does not. Without this the floor
    falls away at the far boundary of a place whose tail is a little longer than
    its tile. A rise is always adopted at once — the hold may only delay a
    DROP."""

    def __init__(self, hold_s):
        self.hold_s = float(hold_s)
        self._held = {}

    def update(self, levels, now_mono, reasons=None):
        out = {}
        for wan, level in levels.items():
            held = self._held.get(wan)
            if held is None or level >= held["level"]:
                self._held[wan] = {"level": level, "since": None,
                                   "reason": (reasons or {}).get(wan, "")}
                out[wan] = level
                continue
            if held["since"] is None:
                held["since"] = now_mono
            if now_mono - held["since"] < self.hold_s:
                out[wan] = held["level"]
            else:
                self._held[wan] = {"level": level, "since": None,
                                   "reason": (reasons or {}).get(wan, "")}
                out[wan] = level
        return out

    def reason_for(self, wan):
        return (self._held.get(wan) or {}).get("reason", "")


@dataclass
class LocationConfig:
    gpsd_host: str = "127.0.0.1"
    gpsd_port: int = 2947
    max_fix_age_s: float = 30.0
    state_path: str = "/run/sbfd-ctl/state.json"
    store_path: str = "/var/lib/sbfd-ctl/location_fec_store.json"
    output_path: str = "/run/sbfd-ctl/location_fec.json"
    poll_interval_s: float = 1.0
    precision: int = 7
    lookahead_s: float = 25.0
    min_speed_ms: float = 2.0
    sample_step_m: float = 75.0
    exit_hold_s: float = 20.0
    max_stale_s: float = 600.0
    max_state_age_s: float = 10.0
    save_interval_s: float = 60.0
    # The WANs to publish for. state.json also names them, but a zone must be
    # able to raise a floor with no live state at all.
    wans: list = field(default_factory=list)
    learning: dict = field(default_factory=dict)
    zones: list = field(default_factory=list)
    table: list = field(default_factory=lambda: list(fec_control.DEFAULT_LOSS_TABLE))


def validate_zone(raw, table_len):
    """Normalize one zone, or None if it is unusable. A hand-edited zone must
    weaken the feature, never stop the daemon: every rejection is logged and
    skipped (mirrors fec_control.safe_ratio's posture)."""
    if not isinstance(raw, dict):
        log.warning("zone ignored: not an object")
        return None
    label = str(raw.get("label") or "zone")
    try:
        lat, lon = float(raw["lat"]), float(raw["lon"])
        radius = float(raw["radius_m"])
        level = int(raw["level"])
    except (KeyError, TypeError, ValueError):
        log.warning("zone %r ignored: needs lat, lon, radius_m and level", label)
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) or radius <= 0:
        log.warning("zone %r ignored: position or radius out of range", label)
        return None
    if not (0 <= level <= table_len - 1):
        log.warning("zone %r ignored: level %d outside the table (0..%d)",
                    label, level, table_len - 1)
        return None
    wans = raw.get("wans")
    if wans is not None and not (isinstance(wans, list)
                                 and all(isinstance(w, str) for w in wans)):
        log.warning("zone %r ignored: wans must be a list of names", label)
        return None
    return {"label": label, "lat": lat, "lon": lon, "radius_m": radius,
            "level": level, "wans": wans,
            "suppress_learned": bool(raw.get("suppress_learned", False))}


def load_location_config(path) -> LocationConfig:
    with open(path) as f:
        raw = json.load(f)
    g = raw.get("gpsd", {})
    tile_cfg = raw.get("tile", {})
    learn = raw.get("learning", {})
    look = raw.get("lookahead", {})
    withdraw = raw.get("withdraw", {})
    table = raw.get("loss_table") or list(fec_control.DEFAULT_LOSS_TABLE)
    zones = [z for z in (validate_zone(z, len(table))
                         for z in raw.get("zones", [])) if z]
    return LocationConfig(
        gpsd_host=g.get("host", "127.0.0.1"),
        gpsd_port=int(g.get("port", 2947)),
        max_fix_age_s=float(g.get("max_fix_age_s", 30)),
        state_path=raw.get("state_path", "/run/sbfd-ctl/state.json"),
        store_path=raw.get("store_path",
                           "/var/lib/sbfd-ctl/location_fec_store.json"),
        output_path=raw.get("output_path", "/run/sbfd-ctl/location_fec.json"),
        poll_interval_s=float(raw.get("poll_interval_s", 1.0)),
        precision=int(tile_cfg.get("precision", tile_store.DEFAULT_PRECISION)),
        lookahead_s=float(look.get("seconds", 25)),
        min_speed_ms=float(look.get("min_speed_ms", 2.0)),
        sample_step_m=float(look.get("sample_step_m", 75)),
        exit_hold_s=float(look.get("exit_hold_s", 20)),
        max_stale_s=float(withdraw.get("max_stale_s", 600)),
        max_state_age_s=float(raw.get("max_state_age_s", 10)),
        save_interval_s=float(learn.get("save_interval_s", 60)),
        wans=[str(w) for w in raw.get("wans", [])],
        learning={k: learn[k] for k in
                  ("min_passes", "alpha", "pass_gap_s", "max_tiles",
                   "max_age_days", "clean_drop_days") if k in learn},
        zones=zones,
        table=table,
    )


def read_state(path, now_wall, max_age_s):
    """(per_wan_loss, residual) from sbfd-ctl's published snapshot, or None.

    A stale snapshot returns None so the caller SKIPS learning: attributing the
    loss sbfd-ctl measured minutes ago to the tile we happen to be in now would
    teach the store about the wrong place. Resolution does not depend on this —
    a floor needs only a position."""
    try:
        with open(path) as f:
            snap = json.load(f)
        ts = float(snap["ts"])
    except (FileNotFoundError, ValueError, OSError, KeyError, TypeError):
        return None
    if now_wall - ts > max_age_s:
        return None
    loss = {}
    for wan, obj in (snap.get("client_local") or {}).items():
        value = (obj or {}).get("loss_pct")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            loss[wan] = float(value)
    rx = (((snap.get("fec") or {}).get("directions") or {})
          .get("client_to_relay") or {}).get("rx") or {}
    residual = rx.get("lost_pkts_est_per_s")
    if not isinstance(residual, (int, float)) or isinstance(residual, bool):
        residual = None
    return loss, residual


def fresh_fix(fix, now_wall, max_age_s):
    """The fix, or None if gpsd's TPV timestamp is older than max_age_s.
    A fix with no timestamp is trusted (some receivers omit it); a stale
    one is a lost fix — the last position gpsd served is not where we are."""
    if fix is None:
        return None
    fix_ts = fix[4] if len(fix) > 4 else None
    if fix_ts is not None and (now_wall - fix_ts) > max_age_s:
        return None
    return fix


def build_record(levels, now_wall):
    """The published contract. LEVEL is the actuated field — each WAN profile
    has its own loss table, so a ratio resolved here could mean something
    different by the time sbfd-ctl applies it. Only non-zero levels are
    published; an empty `wans` is an explicit withdrawal."""
    return {"set_ts": now_wall,
            "source": "location_fec",
            "wans": {w: {"level": v["level"], "reason": v["reason"]}
                     for w, v in levels.items() if v["level"] > 0}}


def write_record(path, record):
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(record, f)
    os.replace(tmp, path)


def poll_once(cfg, store, hold, fix, state, now_mono, now_wall):
    """One tick: learn from state (if fresh), resolve from position, publish.
    Returns the record; the caller writes it."""
    if fix is None:
        store.observe(None, {}, None, now_mono, now_wall)
        return build_record({}, now_wall)
    tile = tile_store.encode(fix[0], fix[1], cfg.precision)
    wans = set(cfg.wans)
    if state is not None:
        per_wan_loss, residual = state
        store.observe(tile, per_wan_loss, residual, now_mono, now_wall)
        wans |= set(per_wan_loss)
    for zone in cfg.zones:
        wans |= set(zone.get("wans") or [])
    wans = sorted(wans)
    if not wans:
        return build_record({}, now_wall)
    levels = resolve(store, fix, cfg.zones, wans, cfg.table,
                     precision=cfg.precision, lookahead_s=cfg.lookahead_s,
                     min_speed_ms=cfg.min_speed_ms,
                     sample_step_m=cfg.sample_step_m)
    reasons = {w: v["reason"] for w, v in levels.items()}
    held = hold.update({w: v["level"] for w, v in levels.items()}, now_mono, reasons=reasons)
    for wan, level in held.items():
        if level != levels[wan]["level"]:
            levels[wan] = {"level": level,
                           "reason": f"exit hold ({hold.reason_for(wan)})"}
    return build_record(levels, now_wall)


_running = True
_reload = False


def _stop(signum, frame):
    global _running
    _running = False


def _sighup(signum, frame):
    global _reload
    _reload = True


def _parse_args(argv):
    ap = argparse.ArgumentParser(prog="location_fec.py",
                                 description="PathFuse location-aware FEC floors")
    ap.add_argument("-c", "--config", required=True,
                    help="path to the location-fec config JSON")
    return ap.parse_args(argv)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGHUP, _sighup)
    args = _parse_args(sys.argv[1:])
    cfg = load_location_config(args.config)
    store = tile_store.TileStore.load(cfg.store_path, **cfg.learning)
    log.info("tile store: %d tiles loaded", len(store.tiles))
    hold = ExitHold(cfg.exit_hold_s)
    last_save = last_prune = time.time()
    last_good = time.monotonic()
    last_published = None
    global _reload
    while _running:
        now_mono, now_wall = time.monotonic(), time.time()
        try:
            if _reload:
                _reload = False
                cfg = load_location_config(args.config)
                log.info("config reloaded: %d zones", len(cfg.zones))
            fix = fresh_fix(environ_ctl.get_fix(cfg.gpsd_host, cfg.gpsd_port, timeout=1.5),
                            now_wall, cfg.max_fix_age_s)
            state = read_state(cfg.state_path, now_wall, cfg.max_state_age_s)
            if fix is not None:
                last_good = now_mono
            elif now_mono - last_good > cfg.max_stale_s:
                # Alive but blind for long enough that any held floor is a guess
                # about a place we cannot confirm we are still in.
                log.warning("no usable fix for %.0f s; withdrawing",
                            now_mono - last_good)
            record = poll_once(cfg, store, hold, fix, state, now_mono, now_wall)
            write_record(cfg.output_path, record)
            published = {w: v["level"] for w, v in record["wans"].items()}
            if published != last_published:
                log.info("location floor -> %s", published or "none")
                last_published = published
            if now_wall - last_save >= cfg.save_interval_s:
                store.save(cfg.store_path)
                last_save = now_wall
            if now_wall - last_prune >= 3600.0:
                dropped = store.prune(now_wall)
                last_prune = now_wall
                if dropped:
                    log.info("pruned %d tiles (%d remain)", dropped, len(store.tiles))
        except Exception as e:  # noqa: BLE001 - keep the daemon alive
            log.error("poll error: %s", e)
        end = now_mono + cfg.poll_interval_s
        while _running and time.monotonic() < end:
            time.sleep(min(0.2, max(0.0, end - time.monotonic())))
    store.close_pass(time.time())
    try:
        store.save(cfg.store_path)
    except OSError as e:
        log.warning("tile store persist on shutdown failed: %s", e)
    log.info("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
