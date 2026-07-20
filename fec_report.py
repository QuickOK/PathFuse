"""Pure parsing + rate tracking for UDPspeeder --report lines, shared by the
client (sbfd-ctl) and relay (udpspeeder_fec) wire-stats tailers. See
docs/superpowers/specs/2026-05-24-fec-phase2-report-capture.md for the format."""
import logging
import re
import threading
import time

# One direction group: (original:N pkt<SEP>N byte) (fec:N pkt<SEP>N byte)
# SEP is ';' everywhere except the first fec group, which uses ',' — accept both.
_GROUP = r"\(original:(\d+) pkt[;,](\d+) byte\) \(fec:(\d+) pkt[;,](\d+) byte\)"
_RE = re.compile(r"client-->server:" + _GROUP + r"\s+server-->client:" + _GROUP)

# RX decode outcomes, emitted by the patched speederv2 on the same --report
# cadence. NOTE: a unit's rx line describes the decode (receive) side of the
# OPPOSITE direction from its [report] TX counters — the client decodes
# server->client traffic and vice versa. Consumers cross-wire accordingly.
_RX_FIELDS = ("pkt_ok", "pkt_rec", "grp_ok", "grp_rec", "grp_fail",
              "shard_lost", "par_waste")
_RX_RE = re.compile(r"\[report_fec_rx\]" +
                    " ".join(f + r":(\d+)" for f in _RX_FIELDS))


def parse_fec_rx_line(msg):
    """Parse a '[report_fec_rx]...' message into cumulative counters, or None."""
    if not msg or "[report_fec_rx]" not in msg:
        return None
    m = _RX_RE.search(msg)
    if not m:
        return None
    return dict(zip(_RX_FIELDS, (int(x) for x in m.groups())))


def parse_report_line(msg):
    """Parse a bare '[ts][INFO][report]...' message into per-direction counters,
    or None if it isn't a report line. Tolerates an optional '[peer]' tag."""
    if not msg or "[report]" not in msg:
        return None
    m = _RE.search(msg)
    if not m:
        return None
    g = [int(x) for x in m.groups()]
    return {
        "client_to_server": {"orig_pkt": g[0], "orig_byte": g[1],
                             "fec_pkt": g[2], "fec_byte": g[3]},
        "server_to_client": {"orig_pkt": g[4], "orig_byte": g[5],
                             "fec_pkt": g[6], "fec_byte": g[7]},
    }


class FecWireTracker:
    """Thread-safe: feed() consumes report lines for one direction, snapshot()
    returns {tx_mbps, overhead_pct, sample_age_s, stale}. Rates are computed
    between successive reports; a counter decrease (process restart) resets the
    baseline without emitting a bogus negative rate."""

    def __init__(self, direction, stale_after_s=30.0):
        assert direction in ("client_to_server", "server_to_client")
        self.direction = direction
        self.stale_after_s = stale_after_s
        self._lock = threading.Lock()
        self._prev = None      # (t, counters)
        self._wire = None      # {"tx_mbps":..,"overhead_pct":..}
        self._wire_t = None
        self._prev_rx = None   # (t, counters)
        self._rx = None        # rates dict
        self._rx_totals = None
        self._rx_t = None
        self._avg_pkts_per_grp = None

    def feed(self, msg, now):
        rep = parse_report_line(msg)
        if rep is not None:
            self._feed_wire(rep[self.direction], now)
            return
        rx = parse_fec_rx_line(msg)
        if rx is not None:
            self._feed_rx(rx, now)

    def _feed_wire(self, cur, now):
        # (old feed() body from `with self._lock:` down, unchanged)
        with self._lock:
            prev = self._prev
            self._prev = (now, cur)
            if prev is None:
                return
            pt, pc = prev
            dt = now - pt
            d_fec = cur["fec_byte"] - pc["fec_byte"]
            d_orig = cur["orig_byte"] - pc["orig_byte"]
            if dt <= 0 or d_fec < 0 or d_orig < 0:
                return  # bad/zero interval or counter reset: keep last good wire
            self._wire = {
                "tx_mbps": round(d_fec * 8.0 / dt / 1e6, 3),
                "overhead_pct": round((d_fec - d_orig) / d_orig * 100.0, 1) if d_orig > 0 else 0.0,
            }
            self._wire_t = now

    def _feed_rx(self, cur, now):
        with self._lock:
            prev = self._prev_rx
            self._prev_rx = (now, cur)
            self._rx_totals = dict(cur)
            if prev is None:
                return
            pt, pc = prev
            dt = now - pt
            d = {k: cur[k] - pc[k] for k in cur}
            if dt <= 0 or any(v < 0 for v in d.values()):
                return  # bad interval or counter reset: keep last good rates
            grp_done = d["grp_ok"] + d["grp_rec"]
            if grp_done > 0:
                self._avg_pkts_per_grp = (d["pkt_ok"] + d["pkt_rec"]) / grp_done
            if self._avg_pkts_per_grp is not None:
                lost = d["grp_fail"] * self._avg_pkts_per_grp
            else:
                lost = d["shard_lost"]  # no completed group yet: shard count as floor
            self._rx = {
                "delivered_per_s": round(d["pkt_ok"] / dt, 1),
                "recovered_per_s": round(d["pkt_rec"] / dt, 1),
                "lost_pkts_est_per_s": round(lost / dt, 1),
                "par_waste_per_s": round(d["par_waste"] / dt, 1),
            }
            self._rx_t = now

    def snapshot(self, now):
        with self._lock:
            if self._wire is None or self._wire_t is None:
                return None
            age = now - self._wire_t
            if age > self.stale_after_s:
                return {"tx_mbps": None, "overhead_pct": None,
                        "sample_age_s": round(age, 1), "stale": True}
            return {**self._wire, "sample_age_s": round(age, 1), "stale": False}

    def rx_snapshot(self, now):
        """Decode-outcome rates + raw totals, or None before two reports.
        Stale mirrors snapshot(): rates null out, totals stay (they are
        cumulative facts, not rates)."""
        with self._lock:
            if self._rx is None or self._rx_t is None:
                return None
            age = now - self._rx_t
            if age > self.stale_after_s:
                return {"delivered_per_s": None, "recovered_per_s": None,
                        "lost_pkts_est_per_s": None, "par_waste_per_s": None,
                        "totals": dict(self._rx_totals),
                        "sample_age_s": round(age, 1), "stale": True}
            return {**self._rx, "totals": dict(self._rx_totals),
                    "sample_age_s": round(age, 1), "stale": False}


def start_wire_tailer(unit, tracker, stop_event=None, line_source=None):
    """Spawn a daemon thread that feeds report lines into `tracker`. By default it
    follows `journalctl -u <unit> -o cat`; pass `line_source` (an iterable of
    str) to inject lines for testing. Best-effort: if journalctl can't start, the
    thread logs a warning and exits (tracker simply never updates -> wire None)."""
    import subprocess
    if stop_event is None:
        stop_event = threading.Event()

    def run():
        src = line_source
        proc = None
        if src is None:
            try:
                proc = subprocess.Popen(
                    ["journalctl", "-u", unit, "-o", "cat", "-f", "-n", "0"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                src = proc.stdout
            except (OSError, ValueError) as e:
                logging.warning("wire tailer for %s failed to start: %s", unit, e)
                return
        try:
            for line in src:
                if stop_event.is_set():
                    break
                tracker.feed(line.rstrip("\n"), time.time())
        finally:
            if proc is not None:
                proc.terminate()

    t = threading.Thread(target=run, name=f"fec-wire-{unit}", daemon=True)
    t.start()
    return t
