#!/usr/bin/env python3
"""Adaptive FEC controller for the OVH side (OVH->truck direction).
Reads OVH sbfd loss, drives the udpspeeder-server FIFO. Loss-driven only."""
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
    The loop sets the published snapshot; POST /fec sets desired_enabled."""

    def __init__(self, enabled=True):
        self._lock = threading.Lock()
        self._enabled = bool(enabled)
        self._snapshot = {"enabled": bool(enabled), "ratio": None, "level": 0,
                          "driving_loss_pct": None, "since": None, "wire": None}

    def get_enabled(self):
        with self._lock:
            return self._enabled

    def set_enabled(self, value):
        with self._lock:
            self._enabled = bool(value)

    def publish(self, **fields):
        with self._lock:
            self._snapshot.update(fields)

    def snapshot(self):
        with self._lock:
            return dict(self._snapshot)


def start_fec_http(listen, state, stop_event=None):
    """Bind GET/POST /fec for the OVH FEC controller. Tailscale-bound via
    IP_FREEBIND (wins the boot race vs tailscaled, mirrors sbfd.py's /state
    listener). Returns the bound httpd, or None if listen is falsy / bind fails."""
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
            if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
                self._json(400, {"error": "enabled must be true or false"}); return
            state.set_enabled(payload["enabled"])
            self._json(200, {"ok": True, "enabled": payload["enabled"]})

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


def run_once(cfg, rt, current_ratio, enabled=True):
    """One control tick. Returns (new_runtime, ratio_now_or_current).
    When enabled is False, force the off tier (8:0) immediately and freeze
    (never stop the udpspeeder process — that would black-hole the tunnel)."""
    table = cfg["loss_table"]
    if not enabled:
        off_ratio = fec_control.level_to_ratio(0, table)
        if current_ratio != off_ratio:
            if fec_control.write_fifo(cfg["fifo"], off_ratio, logging):
                logging.info("fec disabled -> %s (forced off)", off_ratio)
                return fec_control.FecRuntime(0, 0, time.time()), off_ratio
        return rt, current_ratio
    hyst = fec_control.FecHysteresis(cfg["ramp_up_ticks"], cfg["ramp_down_hold_s"])
    worst, up = read_worst_loss(cfg["sbfd_state"])
    if worst is None:
        return rt, current_ratio
    target = fec_control.loss_to_level(worst, table)
    rt, changed = fec_control.step_level(target, rt, hyst, time.time())
    ratio = fec_control.level_to_ratio(rt.current_level, table)
    if changed or current_ratio is None:
        if fec_control.write_fifo(cfg["fifo"], ratio, logging):
            logging.info("fec ratio -> %s (worst_loss=%.1f%% up=%d)", ratio, worst, up)
            return rt, ratio
    return rt, current_ratio


def run(cfg, stop_event=None, state=None, wire_tracker=None):
    if stop_event is None:
        stop_event = threading.Event()
    if state is None:
        state = FecState(enabled=True)
    rt = fec_control.FecRuntime(0, 0, time.time())
    current_ratio = None
    since = None
    while not stop_event.is_set():
        enabled = state.get_enabled()
        prev = current_ratio
        rt, current_ratio = run_once(cfg, rt, current_ratio, enabled=enabled)
        if current_ratio != prev:
            since = time.time()
        worst, _up = read_worst_loss(cfg["sbfd_state"])
        now = time.time()
        state.publish(enabled=enabled, ratio=current_ratio,
                      level=rt.current_level, driving_loss_pct=worst, since=since,
                      wire=(wire_tracker.snapshot(now) if wire_tracker else None))
        stop_event.wait(cfg["poll_interval_s"])


def main():
    ap = argparse.ArgumentParser(description="udpspeeder adaptive FEC controller (OVH)")
    ap.add_argument("-c", "--config", required=True)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = json.loads(Path(args.config).read_text())
    stop = threading.Event()
    state = FecState(enabled=True)
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    start_fec_http(cfg.get("http_listen"), state, stop)
    wire_tracker = fec_report.FecWireTracker(
        "server_to_client", cfg.get("wire_stale_after_s", 30.0))
    fec_report.start_wire_tailer(cfg.get("wire_unit", "udpspeeder-server"), wire_tracker, stop)
    run(cfg, stop, state, wire_tracker=wire_tracker)


if __name__ == "__main__":
    main()
