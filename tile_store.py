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
import os

log = logging.getLogger("tile_store")

# Standard geohash base-32 alphabet (no a/i/l/o).
_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_B32_INDEX = {c: i for i, c in enumerate(_B32)}

DEFAULT_PRECISION = 7


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
