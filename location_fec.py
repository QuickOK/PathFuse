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
import math
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


def zone_terms(zones, points, wans, tiles=()):
    """(levels, labels, suppressed) contributed by operator zones.

    A zone matches on the current position OR any look-ahead point, so a manual
    zone leads arrival on the same terms a learned tile does.

    `suppressed` is {wan: {tile, ...}} — the tiles a `suppress_learned` zone
    covers, NOT the WANs it touches. The two differ the moment the look-ahead
    is longer than the zone: the operator overruled the learner about one
    place, and a confirmed bad tile a few hundred metres short of that place
    must still raise the floor."""
    levels = {w: 0 for w in wans}
    labels = {w: "" for w in wans}
    suppressed = {}
    for zone in zones or []:
        if not any(station_tracker.haversine_m(zone["lat"], zone["lon"], lat, lon)
                   <= zone["radius_m"] for lat, lon in points):
            continue
        targets = zone.get("wans") or wans
        covered = None
        for wan in wans:
            if wan not in targets:
                continue
            if zone["level"] > levels[wan]:
                levels[wan] = zone["level"]
                labels[wan] = zone.get("label") or "zone"
            if not zone.get("suppress_learned"):
                continue
            if covered is None:
                covered = _tiles_in_zone(zone, tiles)
            if covered:
                suppressed.setdefault(wan, set()).update(covered)
    return levels, labels, suppressed


def _tiles_in_zone(zone, tiles):
    """The candidate tiles whose centre falls inside the zone's circle."""
    out = set()
    for tile in tiles or ():
        try:
            lat, lon = tile_store.center(tile)
        except ValueError:
            continue
        if station_tracker.haversine_m(zone["lat"], zone["lon"],
                                       lat, lon) <= zone["radius_m"]:
            out.add(tile)
    return out


def learned_terms(store, tiles, wans, table, suppressed=None):
    """(levels, sources) contributed by the learned store, worst tile wins.

    `suppressed` is {wan: {tile, ...}} from zone_terms: only those (wan, tile)
    pairs are skipped, so suppression stays inside the zone that asked for
    it."""
    levels = {w: 0 for w in wans}
    sources = {w: "" for w in wans}
    for wan in wans:
        blocked = (suppressed or {}).get(wan) or ()
        for tile in tiles:
            if tile in blocked:
                continue
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
    manual, labels, suppressed = zone_terms(zones, points, wans, tiles)
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


class Episode:
    """A condition that persists across ticks, announced once at each edge.

    The loop runs at 1 Hz, so anything logged unconditionally while a fault
    lasts is a log line per second for as long as the fault lasts. `begin()`
    is true only on the tick that opens the episode and `end()` only on the
    tick that closes it; every tick in between is silent."""

    def __init__(self):
        self.active = False

    def begin(self):
        if self.active:
            return False
        self.active = True
        return True

    def end(self):
        if not self.active:
            return False
        self.active = False
        return True


class RateLimitedLog:
    """One line per interval_s for a REPEATING message, with a repeat count.

    For a fault whose text carries detail worth keeping (an exception message)
    an episode flag is too coarse — a different failure must be visible at
    once. So: a new message logs immediately and takes the window; the same
    message inside the window is counted and swallowed, and the count rides
    the next line out. `now_mono` is injected — durations are monotonic and
    this stays a pure helper."""

    def __init__(self, interval_s):
        self.interval_s = float(interval_s)
        self._msg = None
        self._at = None
        self._suppressed = 0

    def due(self, msg, now_mono):
        """The text to log, or None to stay quiet."""
        if msg == self._msg and self._at is not None \
                and now_mono - self._at < self.interval_s:
            self._suppressed += 1
            return None
        repeats = self._suppressed if msg == self._msg else 0
        self._msg, self._at, self._suppressed = msg, now_mono, 0
        if repeats:
            return (f"{msg} (repeated {repeats}\u00d7 in the last "
                    f"{self.interval_s:.0f} s)")
        return msg


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
    # Zones the operator drew on the map. sbfd-ctl owns this file; we only
    # read it, and we re-read it when it changes rather than on SIGHUP — a
    # zone drawn on the map must take effect without a signal.
    operator_zones_path: str = "/var/lib/sbfd-ctl/location_zones.json"
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
        # bool is a subclass of int: int(True) is 1, so `"level": true` would
        # otherwise become a silent floor of level 1 instead of a rejection.
        if isinstance(raw["level"], bool):
            raise TypeError("level must be a number")
        level = int(raw["level"])
    except (KeyError, TypeError, ValueError):
        log.warning("zone %r ignored: needs lat, lon, radius_m and level", label)
        return None
    # Finiteness first, and separately: `radius <= 0` is False for inf, and
    # every haversine comparison against inf is True, so an infinite radius is
    # a zone that matches EVERYWHERE. NaN is the mirror image -- it fails
    # every comparison, so the range test below passes it too.
    if not (math.isfinite(lat) and math.isfinite(lon)
            and math.isfinite(radius)):
        log.warning("zone %r ignored: position or radius is not a finite "
                    "number", label)
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


def load_operator_zones(path, table_len):
    """The zones sbfd-ctl wrote from the map, validated exactly as the
    configured ones are.

    Fail-open in every direction: a missing file is the normal state of a box
    nobody has drawn on yet and says nothing; anything else unusable costs one
    warning and yields no zones. This must never raise — the caller is the 1 Hz
    poll loop, and an operator zone is an addition to the floor, never a
    prerequisite for it."""
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        log.warning("operator zones %s unreadable: %s", path, e)
        return []
    zones = raw.get("zones") if isinstance(raw, dict) else None
    if not isinstance(zones, list):
        log.warning("operator zones %s ignored: no zones list", path)
        return []
    return [z for z in (validate_zone(z, table_len) for z in zones) if z]


class ZoneFile:
    """The operator zone file, re-read only when it actually changes.

    Same memo shape as sbfd-ctl's tile-store memo, and for the same reason:
    the loop runs at 1 Hz and the operator draws a zone once in a while, so
    re-parsing an unchanged file every tick is pure waste — and a malformed
    file would log its warning once a second forever. The inode is in the key
    because mtime_ns + size alone cannot tell a rewrite that reproduces both
    from no change at all; sbfd-ctl's tmp + os.replace always lands a new
    one. table_len is in the key because a SIGHUP can swap the loss table
    under us, and a zone's level is only ever valid against a particular
    table."""

    def __init__(self):
        self._key = None
        self._zones = []

    def maybe_reload(self, path, table_len):
        try:
            st = os.stat(path)
            key = (path, table_len, st.st_mtime_ns, st.st_size, st.st_ino)
        except (OSError, ValueError):
            # Gone, unreadable, or an unusable path (os.stat raises ValueError
            # on an embedded NUL). Clear the memo as well as the list, or a
            # file recreated at the same size under a coarse mtime would be
            # served from a cache of zones that no longer exist.
            self._key, self._zones = None, []
            return self._zones
        if key != self._key:
            self._key = key
            self._zones = load_operator_zones(path, table_len)
        return self._zones


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
        operator_zones_path=raw.get("operator_zones_path",
                                    "/var/lib/sbfd-ctl/location_zones.json"),
        table=table,
    )


# The learning parameters a reload may re-apply to a live store, and the type
# each is stored as (TileStore.__init__ coerces the same way).
_LEARNING_ATTRS = {"min_passes": int, "alpha": float, "pass_gap_s": float,
                   "max_tiles": int, "max_age_days": float,
                   "clean_drop_days": float}


def apply_reload(cfg, store, hold, old_store_path):
    """Re-apply a reloaded config to the objects built from the previous one,
    returning the names of what actually changed.

    SIGHUP is the documented way to change zones without restarting, because a
    restart loses the open pass. But `hold` and the store were constructed from
    the OLD config and would otherwise keep its hold and learning parameters
    until a restart — silently, which is the worst way to be ignored. A value
    that will not coerce is skipped rather than raised on (fail-open: a typo in
    one field must not take the daemon down mid-pass).

    store_path is the one thing deliberately NOT re-applied: swapping the store
    mid-run would strand everything learned since boot. A changed one is warned
    about and PINNED BACK on cfg, so the running process keeps saving where it
    loaded from — a warning alone would not have stopped the next save."""
    applied = []
    if hold.hold_s != float(cfg.exit_hold_s):
        hold.hold_s = float(cfg.exit_hold_s)
        applied.append("exit_hold_s")
    for name, cast in _LEARNING_ATTRS.items():
        if name not in cfg.learning:
            continue
        try:
            value = cast(cfg.learning[name])
        except (TypeError, ValueError):
            log.warning("reload: %s=%r is not usable; keeping %r",
                        name, cfg.learning[name], getattr(store, name))
            continue
        if cast is float and not math.isfinite(value):
            log.warning("reload: %s=%r is not finite; keeping %r",
                        name, cfg.learning[name], getattr(store, name))
            continue
        if getattr(store, name) != value:
            setattr(store, name, value)
            applied.append(name)
    if old_store_path != cfg.store_path:
        log.warning("reload: store_path now %s; that takes effect on restart, "
                    "the running store still writes %s",
                    cfg.store_path, old_store_path)
        # Warning about the deferral is not deferring. main() saves with
        # `store.save(cfg.store_path)` and cfg is already the reloaded one, so
        # without this the very next save writes tiles loaded from the old file
        # into the new one and freezes the old. Pin it for the life of the
        # process; the operator gets the new path on the restart we just asked
        # for. The dataclass is deliberately not frozen.
        cfg.store_path = old_store_path
    return applied


def _dig(obj, *keys):
    """Walk a nested-dict path, yielding {} the moment a level is not a dict."""
    for key in keys:
        if not isinstance(obj, dict):
            return {}
        obj = obj.get(key)
    return obj if isinstance(obj, dict) else {}


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
    # `or {}` rescues only a FALSY wrong type — a non-empty string walks
    # straight into .items()/.get() and raises. A hand-edited or half-written
    # snapshot must cost us this tick's learning, not the daemon.
    client_local = snap.get("client_local")
    if not isinstance(client_local, dict):
        return None
    loss = {}
    for wan, obj in client_local.items():
        if not isinstance(obj, dict):
            continue
        value = obj.get("loss_pct")
        # Finite, because these blend into a tile's EWMA and stay there: one
        # NaN makes every later average NaN, so the tile can never confirm
        # again. json.loads accepts the bareword NaN, so this is reachable.
        if tile_store.finite_number(value):
            loss[wan] = float(value)
    rx = _dig(snap, "fec", "directions", "client_to_relay", "rx")
    residual = rx.get("lost_pkts_est_per_s")
    if not tile_store.finite_number(residual):
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


def poll_once(cfg, store, hold, fix, state, now_mono, now_wall,
              operator_zones=()):
    """One tick: learn from state (if fresh), resolve from position, publish.
    Returns the record; the caller writes it.

    `operator_zones` are the map-drawn ones. They are simply appended to the
    configured list: downstream a zone is a zone, and the two sources combine
    by max like any other pair of terms."""
    if fix is None:
        store.observe(None, {}, None, now_mono, now_wall)
        return build_record({}, now_wall)
    tile = tile_store.encode(fix[0], fix[1], cfg.precision)
    zones = list(cfg.zones) + list(operator_zones or ())
    wans = set(cfg.wans)
    if state is not None:
        per_wan_loss, residual = state
        store.observe(tile, per_wan_loss, residual, now_mono, now_wall)
        wans |= set(per_wan_loss)
    for zone in zones:
        wans |= set(zone.get("wans") or [])
    wans = sorted(wans)
    if not wans:
        return build_record({}, now_wall)
    levels = resolve(store, fix, zones, wans, cfg.table,
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
    zone_file = ZoneFile()
    last_operator_count = None
    # Every 1 Hz fault the loop can hit gets an edge-triggered or rate-limited
    # voice: a fault that lasts an hour must not cost 3600 journal lines.
    blind = Episode()               # blind for longer than max_stale_s
    gpsd_out = Episode()            # gpsd handed us nothing at all
    poll_errors = RateLimitedLog(60.0)
    # Cadence is a DURATION, so it is measured on the monotonic clock. This
    # hardware has no dependable RTC: the first fix can step the wall clock
    # minutes forward or back, and a backward step would stall saves and the
    # hourly prune for exactly as long as the step was.
    last_save = last_prune = time.monotonic()
    last_good = time.monotonic()
    last_published = None
    global _reload
    while _running:
        now_mono, now_wall = time.monotonic(), time.time()
        try:
            if _reload:
                _reload = False
                old_store_path = cfg.store_path
                cfg = load_location_config(args.config)
                applied = apply_reload(cfg, store, hold, old_store_path)
                log.info("config reloaded: %d zones; re-applied %s",
                         len(cfg.zones), ", ".join(applied) or "nothing else")
            # quiet=True: environ_ctl's own warning is written for a caller
            # polling once a minute. One line per outage is ours to write.
            raw_fix = environ_ctl.get_fix(cfg.gpsd_host, cfg.gpsd_port,
                                          timeout=1.5, quiet=True)
            if raw_fix is None:
                if gpsd_out.begin():
                    # gpsd answers with nothing whether the daemon is down or
                    # simply has no sky, and get_fix cannot tell us which.
                    log.warning("gpsd unreachable or without a fix (%s:%s)",
                                cfg.gpsd_host, cfg.gpsd_port)
            elif gpsd_out.end():
                log.info("gpsd reachable again")
            fix = fresh_fix(raw_fix, now_wall, cfg.max_fix_age_s)
            state = read_state(cfg.state_path, now_wall, cfg.max_state_age_s)
            if fix is not None:
                blind_for = now_mono - last_good
                last_good = now_mono
                if blind.end():
                    log.info("fix recovered after %.0f s", blind_for)
            elif now_mono - last_good > cfg.max_stale_s and blind.begin():
                # Alive but blind for long enough that any held floor is a guess
                # about a place we cannot confirm we are still in. Once per
                # blind episode: the condition can last for hours.
                log.warning("no usable fix for %.0f s; withdrawing",
                            now_mono - last_good)
            # No signal: a zone drawn on the map is live within a poll, so
            # this is keyed off the file's own mtime rather than SIGHUP.
            operator_zones = zone_file.maybe_reload(cfg.operator_zones_path,
                                                    len(cfg.table))
            if len(operator_zones) != last_operator_count:
                last_operator_count = len(operator_zones)
                log.info("operator zones -> %d", last_operator_count)
            record = poll_once(cfg, store, hold, fix, state, now_mono, now_wall,
                               operator_zones=operator_zones)
            write_record(cfg.output_path, record)
            published = {w: v["level"] for w, v in record["wans"].items()}
            if published != last_published:
                log.info("location floor -> %s", published or "none")
                last_published = published
            if now_mono - last_save >= cfg.save_interval_s:
                store.save(cfg.store_path)
                last_save = now_mono
            if now_mono - last_prune >= 3600.0:
                dropped = store.prune(now_wall)
                last_prune = now_mono
                if dropped:
                    log.info("pruned %d tiles (%d remain)", dropped, len(store.tiles))
        except Exception as e:  # noqa: BLE001 - keep the daemon alive
            # A permanent failure (an unwritable output path, say) repeats
            # every tick; rate-limit the repeat, but never delay a NEW message.
            line = poll_errors.due(f"poll error: {e}", now_mono)
            if line:
                log.error("%s", line)
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
