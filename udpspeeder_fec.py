#!/usr/bin/env python3
"""Adaptive FEC controller for the relay side (relay->client direction).

Drives the udpspeeder-server FIFO from the loss the CLIENT measures
(relay->client is the direction this leg's parity repairs, and sbfd loss_pct
is RX-side, so only the client can see it). The client pushes that sample in
its POST /fec body (`client_loss_pct`). Relay-local sbfd loss measures the
opposite (client->relay) direction and is kept only as a correlation-proxy
fallback for older clients that don't push."""
import argparse
import json
import logging
import signal
import socket
import threading
import time
from pathlib import Path

import fec_control
import fec_report


class FecState:
    """Thread-safe holder shared between the control loop and the HTTP server.
    The loop sets the published snapshot; POST /fec sets desired mode/ratio."""

    # floor_ratio is keyword-only: it was added after `enabled`, and slotting a
    # new positional in front of a legacy back-compat arg would silently
    # reinterpret any existing FecState(mode, fixed, True) call.
    def __init__(self, mode=None, fixed_ratio=None, enabled=True, *,
                 floor_ratio=None):
        # Back-compat: an explicit enabled=False starts the relay in MODE_OFF
        # so callers that only know the legacy boolean still get sensible
        # behavior.
        self._lock = threading.Lock()
        if mode is None:
            mode = (fec_control.MODE_ADAPTIVE if enabled
                    else fec_control.MODE_OFF)
        self._mode = fec_control.normalize_mode(mode)
        self._fixed_ratio = fixed_ratio or fec_control.DEFAULT_FIXED_RATIO
        self._floor_ratio = floor_ratio or fec_control.DEFAULT_FLOOR_RATIO
        self._pushed_loss = None
        self._pushed_loss_ts = 0.0
        self._snapshot = {
            "enabled": self._mode != fec_control.MODE_OFF,
            "mode": self._mode,
            "fixed_ratio": self._fixed_ratio,
            "floor_ratio": self._floor_ratio,
            "ratio": None,
            "level": 0,
            "driving_loss_pct": None,
            "loss_source": None,
            "since": None,
            "wire": None,
        }

    def get_enabled(self):
        with self._lock:
            return self._mode != fec_control.MODE_OFF

    def set_enabled(self, value):
        with self._lock:
            self._mode = (fec_control.MODE_ADAPTIVE if bool(value)
                          else fec_control.MODE_OFF)

    def get_mode(self):
        with self._lock:
            return self._mode

    def set_mode(self, mode):
        with self._lock:
            self._mode = fec_control.normalize_mode(mode)

    def get_fixed_ratio(self):
        with self._lock:
            return self._fixed_ratio

    def set_fixed_ratio(self, ratio):
        with self._lock:
            self._fixed_ratio = ratio

    def get_floor_ratio(self):
        with self._lock:
            return self._floor_ratio

    def set_floor_ratio(self, ratio):
        with self._lock:
            self._floor_ratio = ratio

    def get_desired(self):
        """Return (mode, fixed_ratio, floor_ratio) atomically."""
        with self._lock:
            return self._mode, self._fixed_ratio, self._floor_ratio

    def set_pushed_loss(self, value, ts):
        with self._lock:
            self._pushed_loss = float(value)
            self._pushed_loss_ts = ts

    def get_pushed_loss(self, now, stale_after_s):
        """Client-pushed relay->client loss sample, or None if absent/stale."""
        with self._lock:
            if self._pushed_loss is None:
                return None
            if (now - self._pushed_loss_ts) > stale_after_s:
                return None
            return self._pushed_loss

    def publish(self, **fields):
        with self._lock:
            self._snapshot.update(fields)

    def snapshot(self):
        with self._lock:
            return dict(self._snapshot)


def start_fec_http(listen, state, stop_event=None):
    """Bind GET/POST /fec for the relay FEC controller. Bound to the management-overlay
    address via IP_FREEBIND (wins the boot race vs the overlay daemon, mirrors sbfd.py's
    /state listener). Returns the bound httpd, or None if listen is falsy / bind fails."""
    if not listen:
        return None
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            logging.debug("fec-http %s - %s", self.address_string(), fmt % args)

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/fec":
                self.send_error(404, "not found"); return
            self._json(200, state.snapshot())

        def do_POST(self):
            if self.path != "/fec":
                self.send_error(404, "not found"); return
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > 256:
                self.send_error(413, "payload too large"); return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": "invalid JSON"}); return
            if not isinstance(payload, dict):
                self._json(400, {"error": "payload must be an object"}); return
            mode_in = payload.get("mode")
            enabled_in = payload.get("enabled")
            fixed_in = payload.get("fixed_ratio")
            floor_in = payload.get("floor_ratio")
            loss_in = payload.get("client_loss_pct")
            if (mode_in is None and enabled_in is None and loss_in is None
                    and fixed_in is None and floor_in is None):
                self._json(400, {"error": "mode, enabled, client_loss_pct, "
                                          "fixed_ratio or floor_ratio required"}); return
            if mode_in is not None and mode_in not in fec_control.ALL_MODES:
                self._json(400, {"error": f"mode must be one of "
                                          f"{sorted(fec_control.ALL_MODES)}"}); return
            if enabled_in is not None and not isinstance(enabled_in, bool):
                self._json(400, {"error": "enabled must be true or false"}); return
            # Resolve every field BEFORE mutating any of it. A payload that
            # carries a good ratio and a bad client_loss_pct must leave the
            # relay untouched, not half-applied behind a 400.
            # Same resolution rule as the client's API boundary, so percent
            # entry works when POSTing to the relay directly too.
            ratio_updates = []
            for key, val, setter in (
                    ("fixed_ratio", fixed_in, state.set_fixed_ratio),
                    ("floor_ratio", floor_in, state.set_floor_ratio)):
                if val is None:
                    continue
                try:
                    ratio_updates.append((setter, fec_control.resolve_ratio(val)))
                except ValueError as e:
                    self._json(400, {"error": f"{key}: {e}"}); return
            if loss_in is not None:
                if (isinstance(loss_in, bool) or not isinstance(loss_in, (int, float))
                        or not (0.0 <= loss_in <= 100.0)):
                    self._json(400, {"error": "client_loss_pct must be a number 0..100"}); return
            for setter, ratio in ratio_updates:
                setter(ratio)
            if loss_in is not None:
                state.set_pushed_loss(loss_in, time.time())
            if mode_in is not None:
                state.set_mode(mode_in)
            elif enabled_in is not None:
                state.set_enabled(enabled_in)
            mode_now, fixed_now, floor_now = state.get_desired()
            self._json(200, {"ok": True, "mode": mode_now,
                             "fixed_ratio": fixed_now,
                             "floor_ratio": floor_now,
                             "enabled": mode_now != fec_control.MODE_OFF})

    host_str, port_str = listen.rsplit(":", 1)
    host = host_str.strip("[]")
    try:
        port = int(port_str)
    except ValueError:
        logging.error("fec http_listen port %r not numeric; disabled", port_str)
        return None

    class FreebindHTTPServer(ThreadingHTTPServer):
        def server_bind(self):
            ip_freebind = getattr(socket, "IP_FREEBIND", 15)
            try:
                self.socket.setsockopt(socket.IPPROTO_IP, ip_freebind, 1)
            except OSError as e:
                logging.warning("IP_FREEBIND setsockopt failed: %s", e)
            super().server_bind()

    try:
        httpd = FreebindHTTPServer((host, port), Handler)
    except OSError as e:
        logging.warning("fec HTTP bind failed (%s:%d): %s; continuing without it", host, port, e)
        return None
    threading.Thread(target=httpd.serve_forever, name="fec-http", daemon=True).start()
    logging.info("fec HTTP listener bound to %s:%d", host, port)
    return httpd


def read_worst_loss(state_path):
    """Return (worst_loss_pct_among_UP, up_count). (None, 0) if unreadable."""
    try:
        raw = json.loads(Path(state_path).read_text())
    except (OSError, ValueError):
        return None, 0
    losses = []
    up = 0
    for s in raw.get("sessions", {}).values():
        if s.get("state") == "UP":
            up += 1
            l = s.get("loss_pct")
            if l is not None:
                losses.append(l)
    return (max(losses) if losses else 0.0), up


def run_once(cfg, rt, current_ratio, enabled=True, mode=None, fixed_ratio=None,
             pushed_loss=None, *, floor_ratio=None):
    """One control tick. Returns (new_runtime, ratio_now_or_current).
    The adaptive engine always advances so the loss-tracked level stays fresh;
    apply_mode then maps it through the operator-chosen mode.

    pushed_loss is the fresh client-measured relay->client loss (the direction
    this leg repairs); when present it drives the level. Relay-local sbfd loss
    (opposite direction) is only the fallback for clients that don't push."""
    table = cfg["loss_table"]
    # The caller (the control loop) passes the operator-settable floor from
    # FecState; cfg is only the boot default, as for mode and fixed_ratio.
    if floor_ratio is None:
        floor_ratio = cfg.get("floor_ratio", fec_control.DEFAULT_FLOOR_RATIO)
    if mode is None:
        mode = fec_control.MODE_ADAPTIVE if enabled else fec_control.MODE_OFF
    if fixed_ratio is None:
        fixed_ratio = fec_control.DEFAULT_FIXED_RATIO

    hyst = fec_control.FecHysteresis(cfg["ramp_up_ticks"], cfg["ramp_down_hold_s"])
    local_worst, up = read_worst_loss(cfg["sbfd_state"])
    worst = pushed_loss if pushed_loss is not None else local_worst
    if worst is None:
        # No fresh loss sample: still honor explicit off/fixed overrides so the
        # operator can drive the relay without depending on sbfd state.
        if mode in (fec_control.MODE_OFF, fec_control.MODE_FIXED):
            forced = fec_control.OFF_RATIO if mode == fec_control.MODE_OFF else fixed_ratio
            if current_ratio != forced:
                if fec_control.write_fifo(cfg["fifo"], forced, logging):
                    logging.info("fec ratio -> %s (mode=%s, no loss sample)",
                                 forced, mode)
                    return rt, forced
        return rt, current_ratio
    target = fec_control.loss_to_level(worst, table)
    rt, _changed = fec_control.step_level(target, rt, hyst, time.time())
    adaptive_ratio = fec_control.level_to_ratio(rt.current_level, table)
    ratio = fec_control.apply_mode(mode, adaptive_ratio,
                                   fixed_ratio=fixed_ratio,
                                   floor_ratio=floor_ratio)
    if ratio != current_ratio:
        if fec_control.write_fifo(cfg["fifo"], ratio, logging):
            logging.info("fec ratio -> %s (mode=%s worst_loss=%.1f%% up=%d)",
                         ratio, mode, worst, up)
            return rt, ratio
    return rt, current_ratio


def run(cfg, stop_event=None, state=None, wire_tracker=None):
    if stop_event is None:
        stop_event = threading.Event()
    if state is None:
        # Seed from cfg, not the module defaults: run() now sources the floor
        # from state, so a state built here must still honor the configured
        # boot values or a direct run() call would silently ignore them.
        state = FecState(
            mode=fec_control.normalize_mode(cfg.get("mode")),
            fixed_ratio=cfg.get("fixed_ratio", fec_control.DEFAULT_FIXED_RATIO),
            floor_ratio=cfg.get("floor_ratio", fec_control.DEFAULT_FLOOR_RATIO))
    rt = fec_control.FecRuntime(0, 0, time.time())
    current_ratio = None
    since = None
    pushed_stale_after = float(cfg.get("pushed_loss_stale_after_s", 90.0))
    while not stop_event.is_set():
        mode, fixed_ratio, floor_ratio = state.get_desired()
        pushed = state.get_pushed_loss(time.time(), pushed_stale_after)
        prev = current_ratio
        rt, current_ratio = run_once(cfg, rt, current_ratio,
                                     mode=mode, fixed_ratio=fixed_ratio,
                                     floor_ratio=floor_ratio,
                                     pushed_loss=pushed)
        if current_ratio != prev:
            since = time.time()
        driving = pushed
        if driving is None:
            driving, _up = read_worst_loss(cfg["sbfd_state"])
        now = time.time()
        state.publish(enabled=mode != fec_control.MODE_OFF,
                      mode=mode, fixed_ratio=fixed_ratio,
                      floor_ratio=floor_ratio,
                      ratio=current_ratio,
                      level=rt.current_level, driving_loss_pct=driving,
                      loss_source=("client_push" if pushed is not None
                                   else "local_sbfd"),
                      since=since,
                      wire=(wire_tracker.snapshot(now) if wire_tracker else None))
        stop_event.wait(cfg["poll_interval_s"])


def main():
    ap = argparse.ArgumentParser(description="udpspeeder adaptive FEC controller (relay)")
    ap.add_argument("-c", "--config", required=True)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = json.loads(Path(args.config).read_text())
    stop = threading.Event()
    initial_mode = fec_control.normalize_mode(cfg.get("mode"))
    initial_fixed = cfg.get("fixed_ratio", fec_control.DEFAULT_FIXED_RATIO)
    initial_floor = cfg.get("floor_ratio", fec_control.DEFAULT_FLOOR_RATIO)
    state = FecState(mode=initial_mode, fixed_ratio=initial_fixed,
                     floor_ratio=initial_floor)
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    start_fec_http(cfg.get("http_listen"), state, stop)
    wire_tracker = fec_report.FecWireTracker(
        "server_to_client", cfg.get("wire_stale_after_s", 30.0))
    fec_report.start_wire_tailer(cfg.get("wire_unit", "udpspeeder-server"), wire_tracker, stop)
    run(cfg, stop, state, wire_tracker=wire_tracker)


if __name__ == "__main__":
    main()
