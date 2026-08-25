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

import logging

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

    def update(self, levels, now_mono):
        out = {}
        for wan, level in levels.items():
            held = self._held.get(wan)
            if held is None or level >= held["level"]:
                self._held[wan] = {"level": level, "since": now_mono}
                out[wan] = level
                continue
            if now_mono - held["since"] < self.hold_s:
                out[wan] = held["level"]
            else:
                self._held[wan] = {"level": level, "since": now_mono}
                out[wan] = level
        return out
