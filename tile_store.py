#!/usr/bin/env python3
"""tile_store.py — geographic tile keying and the learned per-tile loss store
for PathFuse location-aware FEC.

Pure logic: no I/O except the explicit save()/load() helpers, and `now` is
always injected. Wall clock for persisted timestamps (aging must survive a
restart), monotonic for pass boundaries — the caller passes both.
Spec: docs/superpowers/specs/2026-08-25-location-aware-fec-design.md
"""

import json
import logging
import math
import os

import fec_control

log = logging.getLogger("tile_store")

# Standard geohash base-32 alphabet (no a/i/l/o).
_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_B32_INDEX = {c: i for i, c in enumerate(_B32)}

DEFAULT_PRECISION = 7

# A tile whose remembered loss is at or below this is "clean": prune drops it on
# the shorter clean_drop_days clock. Well under the lowest table rung — the point
# is to forget places that never had a problem, not places that have recovered.
LOSSY_EWMA_PCT = 0.5


def encode(lat, lon, precision=DEFAULT_PRECISION):
    """Geohash of a position. Precision 7 is ~153 m square at the equator and
    narrower with latitude, which is the resolution a road-scale bad spot wants:
    fine enough to separate an underpass from the approach, coarse enough that
    GPS noise stays inside one cell."""
    lat_i, lon_i = [-90.0, 90.0], [-180.0, 180.0]
    bits = ch = 0
    even = True
    out = []
    while len(out) < precision:
        if even:
            mid = (lon_i[0] + lon_i[1]) / 2.0
            if lon > mid:
                ch = (ch << 1) | 1
                lon_i[0] = mid
            else:
                ch <<= 1
                lon_i[1] = mid
        else:
            mid = (lat_i[0] + lat_i[1]) / 2.0
            if lat > mid:
                ch = (ch << 1) | 1
                lat_i[0] = mid
            else:
                ch <<= 1
                lat_i[1] = mid
        even = not even
        bits += 1
        if bits == 5:
            out.append(_B32[ch])
            bits = ch = 0
    return "".join(out)


def bbox(tile):
    """(south, west, north, east) of a tile, for drawing it on the map."""
    lat_i, lon_i = [-90.0, 90.0], [-180.0, 180.0]
    even = True
    for c in tile:
        try:
            idx = _B32_INDEX[c]
        except KeyError:
            raise ValueError(f"not a geohash: {tile!r}")
        for shift in (4, 3, 2, 1, 0):
            bit = (idx >> shift) & 1
            interval = lon_i if even else lat_i
            mid = (interval[0] + interval[1]) / 2.0
            interval[0 if bit else 1] = mid
            even = not even
    return (lat_i[0], lon_i[0], lat_i[1], lon_i[1])


def center(tile):
    south, west, north, east = bbox(tile)
    return ((south + north) / 2.0, (west + east) / 2.0)


def _p90(values):
    """The pass's representative loss. Below ten samples this is the maximum,
    which is the intended reading of a brief pass through a bad spot: a tile
    crossed in four seconds should be remembered by its worst second, not
    averaged into innocence."""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, -(-len(ordered) * 9 // 10) - 1)   # ceil(0.9n) - 1
    return ordered[idx]


class TileStore:
    """Per-(tile, WAN) loss memory.

    A PASS — one contiguous occupancy of a tile — is the unit of confidence,
    not a sample. Sample count would let a crawl through a bad spot, or a
    vehicle parked in one, confirm a tile on a single visit; three passes means
    three genuine visits.
    """

    def __init__(self, *, min_passes=3, alpha=0.35, pass_gap_s=30.0,
                 max_tiles=20000, max_age_days=14.0, clean_drop_days=7.0):
        self.min_passes = int(min_passes)
        # A NaN alpha poisons every subsequent blend, so a tile can never
        # confirm again — silently, and for the life of the store file.
        if not math.isfinite(float(alpha)):
            raise ValueError("alpha must be a finite number")
        self.alpha = float(alpha)
        self.pass_gap_s = float(pass_gap_s)
        self.max_tiles = int(max_tiles)
        self.max_age_days = float(max_age_days)
        self.clean_drop_days = float(clean_drop_days)
        # persisted
        self.tiles = {}      # tile -> wan -> {passes, ewma_loss, last_seen}
        self.residual = {}   # tile -> {ewma, last_seen}
        # transient
        self._tile = None
        self._samples = {}   # wan -> [loss, ...]
        self._residual = []
        self._last_mono = None

    # -- learning ------------------------------------------------------------

    def observe(self, tile, per_wan_loss, residual, now_mono, now_wall):
        """Feed one tick. `tile` is None when there is no usable fix, which
        records nothing and leaves the open pass alone — a GPS dropout must not
        split a pass, only a gap longer than pass_gap_s does that."""
        if tile is None:
            return
        gap = (now_mono - self._last_mono) if self._last_mono is not None else 0.0
        if self._tile is not None and (tile != self._tile or gap > self.pass_gap_s):
            self.close_pass(now_wall)
        self._tile = tile
        self._last_mono = now_mono
        for wan, loss in (per_wan_loss or {}).items():
            if isinstance(loss, (int, float)) and not isinstance(loss, bool):
                self._samples.setdefault(wan, []).append(float(loss))
        if isinstance(residual, (int, float)) and not isinstance(residual, bool):
            self._residual.append(float(residual))

    def close_pass(self, now_wall):
        """Fold the open pass into the store: one EWMA step per WAN."""
        tile, samples = self._tile, self._samples
        residual = self._residual
        self._tile, self._samples, self._residual = None, {}, []
        self._last_mono = None
        if tile is None:
            return
        per_wan = self.tiles.setdefault(tile, {})
        for wan, values in samples.items():
            value = _p90(values)
            if value is None:
                continue
            entry = per_wan.setdefault(wan, {"passes": 0, "ewma_loss": 0.0,
                                             "last_seen": now_wall})
            if entry["passes"] == 0:
                entry["ewma_loss"] = value
            else:
                entry["ewma_loss"] = (self.alpha * value
                                      + (1.0 - self.alpha) * entry["ewma_loss"])
            entry["passes"] += 1
            entry["last_seen"] = now_wall
        if not per_wan:
            self.tiles.pop(tile, None)
        value = _p90(residual)
        if value is not None:
            r = self.residual.setdefault(tile, {"ewma": value, "last_seen": now_wall})
            r["ewma"] = self.alpha * value + (1.0 - self.alpha) * r["ewma"]
            r["last_seen"] = now_wall

    # -- reading -------------------------------------------------------------

    def passes_for(self, tile, wan):
        return ((self.tiles.get(tile) or {}).get(wan) or {}).get("passes", 0)

    def level_for(self, tile, wan, table):
        """The level this place asks for, or 0 below the confidence threshold.
        Resolved through the table the ACTIVE profile is using, so a learned
        level means what the adaptive engine means by that level."""
        entry = (self.tiles.get(tile) or {}).get(wan)
        if not entry or entry.get("passes", 0) < self.min_passes:
            return 0
        return fec_control.loss_to_level(entry.get("ewma_loss", 0.0), table)

    # -- bounding ------------------------------------------------------------

    def prune(self, now_wall):
        """Drop tiles that have aged out, gone quiet, or fallen off the LRU
        tail. Returns how many tiles were removed. Age uses the WALL clock:
        the store outlives the process."""
        before = len(self.tiles)
        clean_cut = now_wall - self.clean_drop_days * 86400.0
        age_cut = now_wall - self.max_age_days * 86400.0
        for tile in list(self.tiles):
            per_wan = self.tiles[tile]
            last = max((e.get("last_seen", 0.0) for e in per_wan.values()),
                       default=0.0)
            lossy = any(e.get("ewma_loss", 0.0) > LOSSY_EWMA_PCT
                        for e in per_wan.values())
            if last < age_cut or (not lossy and last < clean_cut):
                del self.tiles[tile]
        if len(self.tiles) > self.max_tiles:
            ordered = sorted(
                self.tiles,
                key=lambda t: max((e.get("last_seen", 0.0)
                                   for e in self.tiles[t].values()), default=0.0))
            for tile in ordered[:len(self.tiles) - self.max_tiles]:
                del self.tiles[tile]
        for tile in list(self.residual):
            if tile not in self.tiles:
                del self.residual[tile]
        return before - len(self.tiles)

    # -- persistence ---------------------------------------------------------

    def to_dict(self):
        return {"version": 1, "tiles": self.tiles, "residual": self.residual}

    @classmethod
    def from_dict(cls, raw, **kw):
        """A schema-malformed store loads with only its valid entries — never
        by raising later. `load()`'s try/except only catches a store that
        isn't parseable JSON; a store that parses fine into the wrong shape
        (a list, a per-WAN entry that isn't a dict, a passes/ewma_loss/
        last_seen of the wrong type) has to be caught here, or it survives
        into the next passes_for()/level_for() call as a crash instead of a
        cache miss. At most one log line either way."""
        store = cls(**kw)
        if not isinstance(raw, dict):
            log.warning("tile store malformed (%s); starting empty",
                        type(raw).__name__)
            return store
        dropped = 0
        tiles = raw.get("tiles")
        if isinstance(tiles, dict):
            clean_tiles = {}
            for tile, per_wan in tiles.items():
                if not isinstance(per_wan, dict):
                    dropped += 1
                    continue
                clean_wan = {}
                for wan, entry in per_wan.items():
                    if not isinstance(entry, dict):
                        dropped += 1
                        continue
                    passes = entry.get("passes")
                    ewma_loss = entry.get("ewma_loss")
                    last_seen = entry.get("last_seen")
                    if (isinstance(passes, int) and not isinstance(passes, bool)
                            and passes >= 0
                            and isinstance(ewma_loss, (int, float))
                            and not isinstance(ewma_loss, bool)
                            and isinstance(last_seen, (int, float))
                            and not isinstance(last_seen, bool)):
                        clean_wan[wan] = {"passes": passes,
                                          "ewma_loss": float(ewma_loss),
                                          "last_seen": float(last_seen)}
                    else:
                        dropped += 1
                if clean_wan:
                    clean_tiles[tile] = clean_wan
            store.tiles = clean_tiles
        elif tiles is not None:
            dropped += 1
        residual = raw.get("residual")
        if isinstance(residual, dict):
            clean_residual = {}
            for tile, r in residual.items():
                if not isinstance(r, dict):
                    dropped += 1
                    continue
                ewma = r.get("ewma")
                last_seen = r.get("last_seen")
                if (isinstance(ewma, (int, float)) and not isinstance(ewma, bool)
                        and isinstance(last_seen, (int, float))
                        and not isinstance(last_seen, bool)):
                    clean_residual[tile] = {"ewma": float(ewma),
                                            "last_seen": float(last_seen)}
                else:
                    dropped += 1
            store.residual = clean_residual
        elif residual is not None:
            dropped += 1
        if dropped:
            log.warning("tile store: dropped %d malformed entr%s",
                        dropped, "y" if dropped == 1 else "ies")
        return store

    def save(self, path):
        """Atomic replace: a torn store read at boot would be indistinguishable
        from a corrupt one, and would silently throw away weeks of learning."""
        tmp = f"{path}.tmp"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(self.to_dict(), f)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path, **kw):
        """A corrupt or missing store loads EMPTY, with one log line. The store
        is a cache of what was observed, never a source of truth — refusing to
        start because of it would trade a degraded feature for a dead daemon."""
        try:
            with open(path) as f:
                raw = json.load(f)
        except (FileNotFoundError, ValueError, OSError) as e:
            if not isinstance(e, FileNotFoundError):
                log.warning("tile store unreadable (%s); starting empty", e)
            return cls(**kw)
        return cls.from_dict(raw, **kw)
