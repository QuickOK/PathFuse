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
                 floor_ratio=None, profile_names=frozenset({"default"})):
        # Back-compat: an explicit enabled=False starts the relay in MODE_OFF
        # so callers that only know the legacy boolean still get sensible
        # behavior.
        self._lock = threading.Lock()
        if mode is None:
            mode = (fec_control.MODE_ADAPTIVE if enabled
                    else fec_control.MODE_OFF)
        self._mode = fec_control.normalize_mode(mode)
        # Coerce both: main() seeds these straight from the hand-editable
        # config, and an uncoerced value would show up in GET /fec even though
        # run_once resolves it before the FIFO ever sees it.
        self._fixed_ratio = fec_control.safe_ratio(
            fixed_ratio, fec_control.DEFAULT_FIXED_RATIO, logging)
        self._floor_ratio = fec_control.safe_ratio(
            floor_ratio, fec_control.DEFAULT_FLOOR_RATIO, logging)
        self._pushed_loss = None
        self._pushed_loss_ts = 0.0
        self.profile_names = frozenset(profile_names) | {"default"}
        self._pushed_profile = None
        self._pushed_signal_floor = False
        self._pushed_location_level = 0
        self._pushed_link_ts = 0.0
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
            "rx": None,
            "ladder": None,
            "profile": "default",
            "profile_source": "default",
            "signal_floor_active": False,
            "location_level": 0,
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

    def set_desired(self, mode=None, fixed_ratio=None, floor_ratio=None,
                    enabled=None):
        """Apply several desired-state fields under ONE lock acquisition.

        Setting them one at a time lets the control loop observe a torn triple
        (new floor with the old mode) for a tick. Only non-None fields change."""
        with self._lock:
            if fixed_ratio is not None:
                self._fixed_ratio = fixed_ratio
            if floor_ratio is not None:
                self._floor_ratio = floor_ratio
            if mode is not None:
                self._mode = fec_control.normalize_mode(mode)
            elif enabled is not None:
                self._mode = (fec_control.MODE_ADAPTIVE if bool(enabled)
                              else fec_control.MODE_OFF)
            # Return what we just applied, under the same lock: a follow-up
            # get_desired() could observe a concurrent request's values and
            # report back something this caller never set.
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

    def set_pushed_link(self, profile, signal_floor, ts, location_level=None):
        """Per-WAN policy pushed by the client; one timestamp for the group."""
        with self._lock:
            if profile is not None:
                self._pushed_profile = profile
            if signal_floor is not None:
                self._pushed_signal_floor = bool(signal_floor)
            if location_level is not None:
                self._pushed_location_level = int(location_level)
            self._pushed_link_ts = ts

    def get_pushed_link(self, now, stale_after_s):
        """(profile, signal_floor, location_level), or (None, False, 0) when
        never pushed or stale — stale MUST also drop the signal floor and the
        location level, not just the table."""
        with self._lock:
            if self._pushed_profile is None or \
                    (now - self._pushed_link_ts) > stale_after_s:
                return None, False, 0
            return (self._pushed_profile, self._pushed_signal_floor,
                    self._pushed_location_level)

    def publish(self, **fields):
        with self._lock:
            self._snapshot.update(fields)

    def snapshot(self):
        # Overlay the live desired fields: publish() only runs once per control
        # tick, so a POST that changed the mode/ratios would otherwise be
        # invisible to GET /fec until the next tick.
        with self._lock:
            snap = dict(self._snapshot)
            snap.update(mode=self._mode, fixed_ratio=self._fixed_ratio,
                        floor_ratio=self._floor_ratio,
                        enabled=self._mode != fec_control.MODE_OFF)
            return snap


def start_fec_http(listen, state, stop_event=None):
    """Bind GET/POST /fec for the relay FEC controller. Bound to the management-overlay
    address via IP_FREEBIND (wins the boot race vs the overlay daemon, mirrors sbfd.py's
    /state listener). Returns the bound httpd, or None if listen is falsy / bind fails."""
    if not listen:
        return None
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        # sbfd-ctl GETs this once per second across the management overlay (and
        # POSTs on change); keep-alive spares each poll a handshake+teardown.
        # Safe because every response carries a Content-Length -- _json sets
        # one, and send_error sets its own plus `Connection: close`, which is
        # what keeps the 404/413 paths that answer WITHOUT reading the request
        # body from leaving it to be parsed as the next request.
        protocol_version = "HTTP/1.1"
        # Bounds the thread an abandoned connection would otherwise hold
        # forever (default timeout is an unbounded blocking read).
        timeout = 30
        # Without this, every keep-alive response pays ~40ms: the handler's
        # header and body writes are separate, Nagle holds the second, and the
        # client sits on its delayed-ACK timer. See sbfd.py's state listener.
        disable_nagle_algorithm = True

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
            profile_in = payload.get("wan_profile")
            signal_in = payload.get("signal_floor")
            location_in = payload.get("location_level")
            if (mode_in is None and enabled_in is None and loss_in is None
                    and fixed_in is None and floor_in is None
                    and profile_in is None and signal_in is None
                    and location_in is None):
                self._json(400, {"error": "mode, enabled, client_loss_pct, "
                                          "fixed_ratio, floor_ratio, wan_profile, "
                                          "signal_floor or location_level "
                                          "required"}); return
            if mode_in is not None and mode_in not in fec_control.ALL_MODES:
                self._json(400, {"error": f"mode must be one of "
                                          f"{sorted(fec_control.ALL_MODES)}"}); return
            if enabled_in is not None and not isinstance(enabled_in, bool):
                self._json(400, {"error": "enabled must be true or false"}); return
            if profile_in is not None and (
                    not isinstance(profile_in, str)
                    or profile_in not in state.profile_names):
                self._json(400, {"error": f"wan_profile must be one of "
                                          f"{sorted(state.profile_names)}"}); return
            if signal_in is not None and not isinstance(signal_in, bool):
                self._json(400, {"error": "signal_floor must be true or false"}); return
            # bool is an int subclass, so it must be excluded explicitly — a
            # stray `true` would otherwise be stored as level 1 and quietly
            # lift this leg a rung.
            if location_in is not None and (
                    isinstance(location_in, bool)
                    or not isinstance(location_in, int) or location_in < 0):
                self._json(400, {"error": "location_level must be a "
                                          "non-negative integer"}); return
            # Resolve every field BEFORE mutating any of it. A payload that
            # carries a good ratio and a bad client_loss_pct must leave the
            # relay untouched, not half-applied behind a 400.
            # Same resolution rule as the client's API boundary, so percent
            # entry works when POSTing to the relay directly too.
            ratio_updates = {}
            for key, val in (("fixed_ratio", fixed_in),
                             ("floor_ratio", floor_in)):
                if val is None:
                    continue
                try:
                    ratio_updates[key] = fec_control.resolve_ratio(val)
                except ValueError as e:
                    self._json(400, {"error": f"{key}: {e}"}); return
            if loss_in is not None:
                if (isinstance(loss_in, bool) or not isinstance(loss_in, (int, float))
                        or not (0.0 <= loss_in <= 100.0)):
                    self._json(400, {"error": "client_loss_pct must be a number 0..100"}); return
            # One lock acquisition for the whole desired triple, so the control
            # loop can never read a half-applied request.
            mode_now, fixed_now, floor_now = state.set_desired(
                mode=mode_in, enabled=enabled_in, **ratio_updates)
            if loss_in is not None:
                state.set_pushed_loss(loss_in, time.time())
            if (profile_in is not None or signal_in is not None
                    or location_in is not None):
                state.set_pushed_link(profile_in, signal_in, time.time(),
                                      location_level=location_in)
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


_warned_profile_values = set()


def _coerce_profile_field(value, fallback, field, caster):
    """Coerce a per-profile numeric field (ramp_up_ticks/ramp_down_hold_s)
    from the hand-editable relay config, falling back to the cellular
    default on anything caster can't handle. This runs every tick inside
    run_once, so a bad value (e.g. "garbage") must never raise and kill the
    daemon — but it also must not warn every tick forever, so it's debounced
    once per distinct (field, value), mirroring fec_control.safe_ratio's
    debounce."""
    try:
        return caster(value)
    except (TypeError, ValueError):
        key = (field, repr(value))
        if key not in _warned_profile_values:
            _warned_profile_values.add(key)
            logging.warning("wan_profile.%s=%r unusable; falling back to %r",
                            field, value, fallback)
        return fallback


def resolve_relay_profile(cfg, name):
    """(loss_table, hysteresis, signal_floor_fec) for a pushed profile name.
    None/'default'/unknown all resolve to the base config — the relay must
    degrade to today's behavior whenever the push is absent or stale. A
    profile that IS registered (even as an empty override dict) falls back to
    the cellular defaults per-field, not the base config — an operator who
    lists a WAN name has opted that link into cellular-aware defaults. The
    floor is NOT profile-resolved here: it rides the existing pushed
    floor_ratio, which the client already computes profile-aware."""
    p = (cfg.get("wan_profiles") or {}).get(name) if name else None
    if p is None:
        return (cfg["loss_table"],
                fec_control.FecHysteresis(cfg["ramp_up_ticks"],
                                          cfg["ramp_down_hold_s"]),
                fec_control.DEFAULT_SIGNAL_FLOOR_FEC)
    ramp_up_ticks = _coerce_profile_field(
        p.get("ramp_up_ticks", 1), 1, "ramp_up_ticks", int)
    ramp_down_hold_s = _coerce_profile_field(
        p.get("ramp_down_hold_s", 60.0), 60.0, "ramp_down_hold_s", float)
    return (p.get("loss_table", fec_control.DEFAULT_CELL_LOSS_TABLE),
            fec_control.FecHysteresis(ramp_up_ticks, ramp_down_hold_s),
            p.get("signal_floor_fec", fec_control.DEFAULT_SIGNAL_FLOOR_FEC))


def run_once(cfg, rt, current_ratio, enabled=True, mode=None, fixed_ratio=None,
             pushed_loss=None, *, floor_ratio=None, pushed_profile=None,
             pushed_signal_floor=False, pushed_location_level=0):
    """One control tick. Returns (new_runtime, ratio_now_or_current).
    The adaptive engine always advances so the loss-tracked level stays fresh;
    apply_mode then maps it through the operator-chosen mode.

    pushed_loss is the fresh client-measured relay->client loss (the direction
    this leg repairs); when present it drives the level. Relay-local sbfd loss
    (opposite direction) is only the fallback for clients that don't push.

    pushed_location_level is the floor the client's location daemon asks for
    at the place the vehicle is standing in. Raise-only and clamped to this
    profile's table, so an absent, stale or over-tall level is inert."""
    table, hyst, sf_fec = resolve_relay_profile(cfg, pushed_profile)
    # The caller (the control loop) passes the operator-settable floor from
    # FecState; cfg is only the boot default, as for mode and fixed_ratio.
    if floor_ratio is None:
        floor_ratio = cfg.get("floor_ratio", fec_control.DEFAULT_FLOOR_RATIO)
    floor_ratio = fec_control.safe_ratio(
        floor_ratio, fec_control.DEFAULT_FLOOR_RATIO, logging)
    fixed_ratio = fec_control.safe_ratio(
        fixed_ratio, fec_control.DEFAULT_FIXED_RATIO, logging)
    if mode is None:
        mode = fec_control.MODE_ADAPTIVE if enabled else fec_control.MODE_OFF

    local_worst, up = read_worst_loss(cfg["sbfd_state"])
    worst = pushed_loss if pushed_loss is not None else local_worst
    if worst is None:
        # No fresh loss sample: still honor the operator's mode so the relay can
        # be driven without depending on sbfd state. Mapping the last known
        # level through apply_mode covers every mode uniformly — including a
        # min_adaptive floor the operator just raised, which would otherwise not
        # take effect until a loss sample arrived. The signal floor must still
        # lift the level here too, or a degraded radio with no fresh loss
        # sample would silently lose its floor.
        level = fec_control.apply_signal_floor(
            rt.current_level, pushed_signal_floor, table, sf_fec)
        level = fec_control.apply_location_floor(
            level, pushed_location_level, table)
        forced = fec_control.apply_mode(
            mode, fec_control.level_to_ratio(level, table),
            fixed_ratio=fixed_ratio, floor_ratio=floor_ratio)
        if current_ratio != forced:
            if fec_control.write_fifo(cfg["fifo"], forced, logging):
                logging.info("fec ratio -> %s (mode=%s, no loss sample)",
                             forced, mode)
                return rt, forced
        return rt, current_ratio
    target = fec_control.loss_to_level(worst, table)
    rt, _changed = fec_control.step_level(target, rt, hyst, time.time())
    level = fec_control.apply_signal_floor(
        rt.current_level, pushed_signal_floor, table, sf_fec)
    level = fec_control.apply_location_floor(
        level, pushed_location_level, table)
    adaptive_ratio = fec_control.level_to_ratio(level, table)
    ratio = fec_control.apply_mode(mode, adaptive_ratio,
                                   fixed_ratio=fixed_ratio,
                                   floor_ratio=floor_ratio)
    if ratio != current_ratio:
        if fec_control.write_fifo(cfg["fifo"], ratio, logging):
            logging.info("fec ratio -> %s (mode=%s worst_loss=%.1f%% up=%d)",
                         ratio, mode, worst, up)
            return rt, ratio
    return rt, current_ratio


def fec_state_from_cfg(cfg):
    """Build a FecState seeded from cfg's boot defaults.

    run() sources the mode and both ratios from state, so a state built without
    these would silently ignore the configured values. FecState coerces the
    ratios, so a hand-edited config can't put a raw value on the control path."""
    return FecState(
        mode=fec_control.normalize_mode(cfg.get("mode")),
        fixed_ratio=cfg.get("fixed_ratio", fec_control.DEFAULT_FIXED_RATIO),
        floor_ratio=cfg.get("floor_ratio", fec_control.DEFAULT_FLOOR_RATIO),
        profile_names=frozenset((cfg.get("wan_profiles") or {}).keys())
        | {"default"})


def run(cfg, stop_event=None, state=None, wire_tracker=None):
    if stop_event is None:
        stop_event = threading.Event()
    if state is None:
        state = fec_state_from_cfg(cfg)
    rt = fec_control.FecRuntime(0, 0, time.time())
    current_ratio = None
    since = None
    last_profile = "default"
    last_location_level = 0
    pushed_stale_after = float(cfg.get("pushed_loss_stale_after_s", 90.0))
    while not stop_event.is_set():
        mode, fixed_ratio, floor_ratio = state.get_desired()
        pushed = state.get_pushed_loss(time.time(), pushed_stale_after)
        pushed_profile, pushed_sf, pushed_loc = state.get_pushed_link(
            time.time(), pushed_stale_after)
        if pushed_loc != last_location_level:
            logging.info("fec location level -> %d", pushed_loc)
            last_location_level = pushed_loc
        profile_name = pushed_profile or "default"
        if profile_name != last_profile:
            # Translate the runtime's level across the profile switch: the OLD
            # level index means something different on the NEW table (e.g.
            # level 2 is 8:4 on the base table but 12:1 on the cell table), so
            # re-derive it from the ratio actually on the wire rather than
            # carrying the raw index forward.
            new_table, _, _ = resolve_relay_profile(cfg, profile_name)
            rt = fec_control.FecRuntime(
                fec_control.ratio_to_level(current_ratio or "8:0", new_table),
                0, time.time())
            logging.info("fec profile -> %s", profile_name)
            last_profile = profile_name
        prev = current_ratio
        rt, current_ratio = run_once(cfg, rt, current_ratio,
                                     mode=mode, fixed_ratio=fixed_ratio,
                                     floor_ratio=floor_ratio,
                                     pushed_loss=pushed,
                                     pushed_profile=pushed_profile,
                                     pushed_signal_floor=pushed_sf,
                                     pushed_location_level=pushed_loc)
        if current_ratio != prev:
            since = time.time()
        driving = pushed
        if driving is None:
            driving, _up = read_worst_loss(cfg["sbfd_state"])
        now = time.time()
        # Resolve the profile again purely to publish the ladder: run_once
        # resolves it internally and the table is dict lookups, so re-resolving
        # is cheaper than threading it back out through the return tuple.
        pub_table, _, _ = resolve_relay_profile(cfg, pushed_profile)
        ladder = fec_control.ladder_state(
            mode, current_ratio,
            # Coerce as run_once does: an unusable floor is applied as the
            # default, so the ladder must place it on the same rung.
            fec_control.safe_ratio(floor_ratio, fec_control.DEFAULT_FLOOR_RATIO,
                                   logging),
            pub_table)
        state.publish(enabled=mode != fec_control.MODE_OFF,
                      mode=mode, fixed_ratio=fixed_ratio,
                      floor_ratio=floor_ratio,
                      ratio=current_ratio,
                      level=rt.current_level, ladder=ladder,
                      driving_loss_pct=driving,
                      loss_source=("client_push" if pushed is not None
                                   else "local_sbfd"),
                      since=since,
                      profile=profile_name,
                      profile_source=("pushed" if pushed_profile else "default"),
                      signal_floor_active=pushed_sf,
                      location_level=pushed_loc,
                      wire=(wire_tracker.snapshot(now) if wire_tracker else None),
                      rx=(wire_tracker.rx_snapshot(now) if wire_tracker else None))
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
    state = fec_state_from_cfg(cfg)
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    start_fec_http(cfg.get("http_listen"), state, stop)
    wire_tracker = fec_report.FecWireTracker(
        "server_to_client", cfg.get("wire_stale_after_s", 30.0))
    fec_report.start_wire_tailer(cfg.get("wire_unit", "udpspeeder-server"), wire_tracker, stop)
    run(cfg, stop, state, wire_tracker=wire_tracker)


if __name__ == "__main__":
    main()
