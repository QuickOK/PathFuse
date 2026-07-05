#!/usr/bin/env python3
"""station_tracker.py — local station learner for PathFuse environ-ctl.

Clusters GPS dwells into stations whose centroids are the running average of
every fix observed there (noisy indoor fixes converge), learns first-order
visit transitions, and predicts the next stop(s). Pure logic: no I/O except
the explicit save()/load() helpers; `now` is always injected (wall clock —
state persists across restarts).
Spec: docs/superpowers/specs/2026-07-05-environ-forecast-stations-design.md
"""

import json
import logging
import math
import os

log = logging.getLogger("station_tracker")


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


class StationTracker:
    def __init__(self, *, radius_m=150.0, dwell_speed_ms=1.0, dwell_min_s=600.0,
                 hold_s=900.0, max_stations=16, predict_n=2):
        self.radius_m = radius_m
        self.dwell_speed_ms = dwell_speed_ms
        self.dwell_min_s = dwell_min_s
        self.hold_s = hold_s
        self.max_stations = max_stations
        self.predict_n = predict_n
        # persisted state
        self.stations = {}      # sid -> {lat, lon, n_fixes, visits, last_visit}
        self.transitions = {}   # sid -> {sid: count}
        self.next_id = 1
        self.last_station = None
        # transient state
        self._at_station = None
        self._dwell_start = None
        self._sum_lat = 0.0
        self._sum_lon = 0.0
        self._n = 0
        self._last_fix = None   # (lat, lon, wall_ts)

    # -- learning --------------------------------------------------------------

    def update(self, fix, now):
        """Feed one poll's fix ((lat, lon, speed_ms) or None for no/stale fix).
        No fix leaves dwell state untouched (indoor GPS dropouts must not reset
        an in-progress dwell)."""
        if fix is None:
            return
        lat, lon, speed = fix
        self._last_fix = (lat, lon, now)
        if speed < self.dwell_speed_ms:
            if self._dwell_start is None:
                self._dwell_start = now
                self._sum_lat = self._sum_lon = 0.0
                self._n = 0
            self._sum_lat += lat
            self._sum_lon += lon
            self._n += 1
            if self._at_station is not None:
                self._absorb(self._at_station, lat, lon, now)
            elif now - self._dwell_start >= self.dwell_min_s and self._n:
                self._arrive(self._sum_lat / self._n, self._sum_lon / self._n, now)
        else:
            self._dwell_start = None
            self._at_station = None     # departure; last_station remembers origin

    def _arrive(self, lat, lon, now):
        sid = self._match(lat, lon)
        if sid is None:
            sid = self._create(lat, lon)
        else:
            self._absorb(sid, lat, lon, now)
        st = self.stations[sid]
        st["visits"] += 1
        st["last_visit"] = now
        prev = self.last_station
        if prev and prev != sid and prev in self.stations:
            row = self.transitions.setdefault(prev, {})
            row[sid] = row.get(sid, 0) + 1
        self.last_station = sid
        self._at_station = sid
        log.info("at station %s (%.5f, %.5f) visits=%d", sid, st["lat"], st["lon"],
                 st["visits"])

    def _absorb(self, sid, lat, lon, now):
        st = self.stations[sid]
        n = st["n_fixes"] + 1
        st["lat"] += (lat - st["lat"]) / n
        st["lon"] += (lon - st["lon"]) / n
        st["n_fixes"] = n
        st["last_visit"] = now

    def _match(self, lat, lon):
        best, best_d = None, None
        for sid, st in self.stations.items():
            d = haversine_m(lat, lon, st["lat"], st["lon"])
            if d <= self.radius_m and (best_d is None or d < best_d):
                best, best_d = sid, d
        return best

    def _create(self, lat, lon):
        if len(self.stations) >= self.max_stations:
            evict = min(self.stations, key=lambda s: self.stations[s]["last_visit"])
            self._remove(evict)
        sid = f"s{self.next_id}"
        self.next_id += 1
        self.stations[sid] = {"lat": lat, "lon": lon, "n_fixes": 1,
                              "visits": 0, "last_visit": 0.0}
        return sid

    def _remove(self, sid):
        self.stations.pop(sid, None)
        self.transitions.pop(sid, None)
        for row in self.transitions.values():
            row.pop(sid, None)
        if self.last_station == sid:
            self.last_station = None
        if self._at_station == sid:
            self._at_station = None
        log.info("evicted station %s", sid)

    # -- prediction ------------------------------------------------------------

    def predict_points(self):
        origin = self._at_station or self.last_station
        if not origin:
            return []
        row = self.transitions.get(origin, {})
        ranked = sorted(
            row.items(),
            key=lambda kv: (-kv[1],
                            -self.stations.get(kv[0], {}).get("last_visit", 0.0)))
        out = []
        for sid, _count in ranked[:self.predict_n]:
            st = self.stations.get(sid)
            if st:
                out.append((st["lat"], st["lon"]))
        return out
