"""Bounded in-memory history of per-tick FEC direction samples, appended by
the controller loop and served whole by /api/fec_history for the UI graph."""
import threading
from collections import deque

_FIELDS = ("delivered_per_s", "recovered_per_s", "lost_pkts_est_per_s",
           "par_waste_per_s")


class FecHistory:
    """Thread-safe ring of {t, c2r, r2c}. The 0.5 s controller tick outpaces
    the 1/s graph resolution, so appends are throttled by min_interval_s."""

    def __init__(self, maxlen=3600, min_interval_s=1.0):
        self._lock = threading.Lock()
        self._buf = deque(maxlen=maxlen)
        self._min_interval_s = min_interval_s
        self._last_t = None

    @staticmethod
    def _side(d):
        d = d or {}
        wire = d.get("wire") or {}
        rx = d.get("rx") or {}
        out = {"tx_mbps": wire.get("tx_mbps"),
               "overhead_pct": wire.get("overhead_pct")}
        out.update({f: rx.get(f) for f in _FIELDS})
        return out

    def append_from_directions(self, t, directions):
        directions = directions or {}
        with self._lock:
            if self._last_t is not None and (t - self._last_t) < self._min_interval_s:
                return
            self._last_t = t
            self._buf.append({
                "t": t,
                "c2r": self._side(directions.get("client_to_relay")),
                "r2c": self._side(directions.get("relay_to_client")),
            })

    def snapshot(self):
        with self._lock:
            return list(self._buf)
