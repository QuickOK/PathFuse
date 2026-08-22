#!/usr/bin/env python3
"""
sbfd - software BFD-equivalent liveness daemon.

Symmetric: same binary runs on the client and relay (server).
One UDP socket per configured session. Each session is a peering
between (local_iface, peer_addr:port) identified by a session_id.

Wire format (32 bytes, network byte order):
    offset  size  field
      0      4    magic       0xBFD15AFE
      4      1    version     1
      5      1    flags       bit 0: state UP
      6      2    session_id
      8      8    seq
     16      8    tx_time_us
     24      4    rx_seq
     28      4    rx_age_us
"""

import argparse
import json
import logging
import os
import select
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import IntEnum
from pathlib import Path
from typing import Optional

# -- Protocol constants -------------------------------------------------------

MAGIC = 0xBFD15AFE
VERSION = 1
PACKET_FMT = "!IBBHQQII"  # 4+1+1+2+8+8+4+4 = 32
PACKET_SIZE = struct.calcsize(PACKET_FMT)
assert PACKET_SIZE == 32

FLAG_UP = 0x01

# A failing tx path retries every tick; log it loudly but not once per packet.
SEND_ERROR_LOG_INTERVAL_S = 60.0
# Consecutive send failures before we suspect the socket itself, and the
# floor between rebuild attempts once we do.
SEND_ERROR_REBUILD_THRESHOLD = 8
SOCKET_REBUILD_INTERVAL_S = 60.0
# A backwards jump in the peer's sequence larger than any plausible network
# reordering means the peer restarted, not that the packet is stale.
SEQ_RESTART_REWIND = 64

# -- State machine -----------------------------------------------------------

class State(IntEnum):
    DOWN = 0
    INIT = 1
    UP = 2

# -- Configuration -----------------------------------------------------------

@dataclass
class SessionConfig:
    session_id: int
    name: str                  # human label e.g. "wan1"
    local_iface: Optional[str] # for SO_BINDTODEVICE; None = any
    peer_host: str
    peer_port: int
    tx_interval_ms: int = 500
    detect_mult: int = 3
    # No up_threshold here on purpose: a "consecutive hits before UP" knob was
    # parsed but never read by any transition, so setting it did nothing. Flap
    # damping lives in sbfd-ctl (policy.failback_hold_s, dynamic hysteresis).
    # load_config ignores the key if a deployed config still carries it.

@dataclass
class DaemonConfig:
    bind_host: str = "0.0.0.0"
    bind_port: int = 3784
    state_file: str = "/run/sbfd/state.json"
    state_write_interval_s: float = 1.0
    state_listen: Optional[str] = None  # e.g. "100.64.0.2:9275"; None disables
    sessions: list = field(default_factory=list)

# -- Per-session runtime state -----------------------------------------------

@dataclass
class Session:
    cfg: SessionConfig
    state: State = State.DOWN
    state_since: float = 0.0
    # Start tx_seq at 1 so the value 0 is unambiguously "never received"
    # in the rx-side replay filter and the rx_seq cross-correlation field.
    tx_seq: int = 1
    last_rx_seq: int = 0
    last_rx_time: float = 0.0
    consecutive_miss: int = 0
    consecutive_hit: int = 0
    rtt_ewma_us: float = 0.0
    loss_ewma_pct: float = 0.0
    last_tx_time: float = 0.0
    sock: Optional[socket.socket] = None
    peer_sockaddr: Optional[tuple] = None
    last_peer_flags: int = 0
    # Tx-failure bookkeeping: a send that fails every tick is invisible
    # otherwise, and it is exactly what a wedged interface socket looks like.
    consecutive_send_errors: int = 0
    last_send_error_log: float = 0.0
    last_socket_rebuild: float = 0.0

    # For loss math: track expected vs received over a window
    rx_count_window: int = 0
    expected_count_window: int = 0
    window_start: float = 0.0

# -- Helpers -----------------------------------------------------------------

def now_us() -> int:
    return int(time.time_ns() // 1000)

def now_s() -> float:
    return time.time()

def pack_packet(magic: int, version: int, flags: int, session_id: int,
                seq: int, tx_time_us: int, rx_seq: int, rx_age_us: int) -> bytes:
    # struct format I = unsigned 4 bytes; seq is 8 bytes (Q); rx_seq we keep as 4
    return struct.pack(PACKET_FMT, magic, version, flags, session_id,
                       seq, tx_time_us, rx_seq & 0xFFFFFFFF, rx_age_us & 0xFFFFFFFF)

def unpack_packet(data: bytes):
    if len(data) != PACKET_SIZE:
        return None
    return struct.unpack(PACKET_FMT, data)

def ewma(old: float, new: float, alpha: float = 0.2) -> float:
    if old == 0.0:
        return new
    return alpha * new + (1 - alpha) * old

# -- Socket setup ------------------------------------------------------------

def make_session_socket(cfg: SessionConfig, bind_host: str, bind_port: int) -> socket.socket:
    """Create a per-session UDP socket bound to a specific interface (if requested).

    SO_BINDTODEVICE requires CAP_NET_RAW (root or capability). On non-Linux or
    when running as a non-privileged user without the capability, we fall back
    to no interface binding and let the kernel route by destination.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if cfg.local_iface:
        try:
            s.setsockopt(socket.SOL_SOCKET, 25, cfg.local_iface.encode() + b"\x00")  # SO_BINDTODEVICE = 25
            logging.info("session %s bound to interface %s", cfg.name, cfg.local_iface)
        except (PermissionError, OSError) as e:
            logging.warning("session %s: could not bind to %s (%s); using kernel routing",
                            cfg.name, cfg.local_iface, e)
    # Bind to local port so peer can reach us. Each session gets its own port
    # offset by session_id to avoid collisions when multiple sessions share an iface.
    local_port = bind_port + cfg.session_id
    s.bind((bind_host, local_port))
    s.setblocking(False)
    return s

def maybe_rebuild_socket(sess: Session, cfg: DaemonConfig) -> bool:
    """Re-create a session socket whose sends keep failing. Returns True if it did.

    A socket bound with SO_BINDTODEVICE outlives the interface configuration it
    was bound under. After a link flap or a lease change every sendto on it can
    fail for the life of the process while receives on the same socket keep
    working -- so the detect timer never fires and the session sits UP against
    a peer that stopped hearing us. Restarting the daemon is what clears that;
    this does the same thing without dropping the other sessions.
    """
    if sess.consecutive_send_errors < SEND_ERROR_REBUILD_THRESHOLD:
        return False
    now = now_s()
    if (sess.last_socket_rebuild != 0.0
            and now - sess.last_socket_rebuild < SOCKET_REBUILD_INTERVAL_S):
        return False
    sess.last_socket_rebuild = now

    # Build the replacement before dropping the incumbent: a duplicate UDP bind
    # is permitted, so there is no window in which the session has no socket.
    try:
        new_sock = make_session_socket(sess.cfg, cfg.bind_host, cfg.bind_port)
    except OSError as e:
        logging.warning("session %s: socket rebuild failed: %s", sess.cfg.name, e)
        return False

    logging.warning("session %s: re-created socket after %d send failures",
                    sess.cfg.name, sess.consecutive_send_errors)
    old = sess.sock
    sess.sock = new_sock
    sess.consecutive_send_errors = 0
    if old is not None:
        try:
            old.close()
        except OSError:
            pass
    return True

# -- State transitions -------------------------------------------------------

def transition(sess: Session, new_state: State, reason: str):
    if sess.state == new_state:
        return
    old = sess.state
    sess.state = new_state
    sess.state_since = now_s()
    if new_state == State.DOWN:
        # The peer's sequence space is deliberately NOT forgotten here. A
        # restarted peer is recognised by the rewind test in on_rx, whereas
        # clearing the watermark would let packets that predate this
        # transition walk the session straight back up.
        sess.last_rx_time = 0.0
        sess.rx_count_window = 0
        sess.expected_count_window = 0
        sess.window_start = now_s()
    logging.info("session %s: %s -> %s (%s)", sess.cfg.name, old.name, new_state.name, reason)

def on_rx(sess: Session, packet: tuple, src_addr: tuple):
    magic, version, flags, sid, seq, tx_time_us, rx_seq, rx_age_us = packet

    if magic != MAGIC:
        return
    if version != VERSION:
        return
    if sid != sess.cfg.session_id:
        return

    # Update peer address (handles cellular IP changes)
    sess.peer_sockaddr = src_addr

    # Replay/reorder filter. A small rewind is reordering and is dropped; a
    # large one means the peer restarted its sequence space, so we accept it
    # and resynchronise rather than filtering the peer out permanently.
    if sess.last_rx_seq - SEQ_RESTART_REWIND < seq <= sess.last_rx_seq:
        return

    # Loss math: count gap if any
    if sess.last_rx_seq != 0:
        gap = seq - sess.last_rx_seq - 1
        if gap > 0:
            sess.expected_count_window += gap  # we expected those, didn't get them
    sess.expected_count_window += 1
    sess.rx_count_window += 1

    # RTT estimate: peer told us when they last heard from us (rx_seq, rx_age_us).
    # If rx_seq matches a packet we sent recently, RTT = (now - our send time of rx_seq) - rx_age_us.
    # We don't keep per-seq send-time history (KISS), so approximate using the most recent send.
    if rx_seq != 0 and rx_seq == (sess.tx_seq - 1) & 0xFFFFFFFF and sess.last_tx_time > 0:
        rtt_us = (now_us() - int(sess.last_tx_time * 1_000_000)) - rx_age_us
        if 0 < rtt_us < 60_000_000:  # sanity: < 60s
            sess.rtt_ewma_us = ewma(sess.rtt_ewma_us, rtt_us)

    sess.last_rx_seq = seq
    sess.last_rx_time = now_s()
    sess.last_peer_flags = flags
    sess.consecutive_hit += 1
    sess.consecutive_miss = 0

    # State machine
    if sess.state == State.DOWN:
        transition(sess, State.INIT, "received packet from peer")
    elif sess.state == State.INIT:
        if flags & FLAG_UP:
            # Peer is hearing us; bidirectional confirmed
            transition(sess, State.UP, "peer reports UP, bidirectional")
    elif sess.state == State.UP:
        if not (flags & FLAG_UP):
            # The peer has stopped hearing us. However well we hear it, the
            # path is one-way and the session is not UP -- without this the
            # detect timer (which only counts *our* rx misses) keeps us UP
            # indefinitely while the far end has long since gone DOWN.
            transition(sess, State.DOWN, "peer stopped reporting UP (one-way path)")

def check_timeouts(sess: Session):
    """Called once per tx tick to advance miss counters."""
    # Reference time = last rx, or state-entry time if we've never received.
    ref = sess.last_rx_time if sess.last_rx_time > 0 else (sess.state_since or now_s())
    elapsed = now_s() - ref
    sess.consecutive_miss = int(elapsed * 1000 / sess.cfg.tx_interval_ms)

    if sess.state in (State.UP, State.INIT):
        if sess.consecutive_miss >= sess.cfg.detect_mult:
            reason = f"{sess.consecutive_miss} consecutive misses"
            if sess.state == State.INIT:
                reason += " in INIT"
            transition(sess, State.DOWN, reason)
            sess.consecutive_hit = 0

def update_loss_ewma(sess: Session):
    """Recompute loss percentage over rolling window."""
    elapsed = now_s() - sess.window_start
    if elapsed >= 5.0 and sess.expected_count_window > 0:
        loss_pct = 100.0 * (1.0 - sess.rx_count_window / sess.expected_count_window)
        sess.loss_ewma_pct = ewma(sess.loss_ewma_pct, loss_pct, alpha=0.3)
        sess.rx_count_window = 0
        sess.expected_count_window = 0
        sess.window_start = now_s()

# -- Tx -----------------------------------------------------------------------

def send_packet(sess: Session):
    flags = 0
    # We tell the peer we're UP if WE consider the session UP or INIT (i.e. we're hearing them).
    # In real BFD this distinguishes Init/Up bits; we collapse it: bit 0 = "I'm hearing you".
    if sess.state in (State.INIT, State.UP):
        flags |= FLAG_UP

    rx_age_us = 0
    if sess.last_rx_time > 0:
        rx_age_us = int((now_s() - sess.last_rx_time) * 1_000_000)
        rx_age_us = min(rx_age_us, 0xFFFFFFFF)

    pkt = pack_packet(
        MAGIC, VERSION, flags, sess.cfg.session_id,
        sess.tx_seq, now_us(),
        sess.last_rx_seq, rx_age_us
    )

    # Destination: prefer observed peer (tracks cellular IP changes; also
    # the only valid target when peer_host is an unspecified placeholder).
    dest = sess.peer_sockaddr
    if dest is None:
        if sess.cfg.peer_host in ("", "0.0.0.0"):
            # No peer known yet and no configured destination to fall back to.
            return
        if not hasattr(sess, "_dest"):
            try:
                addrinfo = socket.getaddrinfo(sess.cfg.peer_host, sess.cfg.peer_port,
                                              socket.AF_INET, socket.SOCK_DGRAM)
                sess._dest = addrinfo[0][4]
            except socket.gaierror as e:
                logging.warning("session %s: DNS resolution failed for %s: %s",
                                sess.cfg.name, sess.cfg.peer_host, e)
                return
        dest = sess._dest

    try:
        sess.sock.sendto(pkt, dest)
    except OSError as e:
        # Network unreachable, EPERM from a filter rule, a socket bound to an
        # interface that has since been reconfigured. Any of these can persist
        # for as long as the process lives, so say so at a level that is
        # actually visible -- rate-limited, since we retry every tick.
        sess.consecutive_send_errors += 1
        now = now_s()
        if (sess.last_send_error_log == 0.0
                or now - sess.last_send_error_log >= SEND_ERROR_LOG_INTERVAL_S):
            sess.last_send_error_log = now
            logging.warning("session %s: sendto failed: %s (%d failures in a row)",
                            sess.cfg.name, e, sess.consecutive_send_errors)
        return

    if sess.consecutive_send_errors:
        logging.info("session %s: sendto recovered after %d failures",
                     sess.cfg.name, sess.consecutive_send_errors)
        sess.consecutive_send_errors = 0
        sess.last_send_error_log = 0.0
    sess.last_tx_time = now_s()
    sess.tx_seq = (sess.tx_seq + 1) & 0xFFFFFFFFFFFFFFFF

# -- State file --------------------------------------------------------------

def write_state_file(cfg: DaemonConfig, sessions: list):
    state_path = Path(cfg.state_file)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError) as e:
        # We run unprivileged, so we cannot recreate a directory whose parent
        # is root-owned (e.g. /run): if the state dir is removed out from under
        # a running daemon, this fails on every tick and we stop publishing
        # state. A silently-unpublished state file is how downstream readers
        # (the maintenance-reboot gate, sbfd-ctl) go blind unnoticed — so make
        # noise, but only on the transition into failure (this runs ~1/s).
        _warn_state_publish_broken(f"cannot create state dir {state_path.parent}", e)
        return

    out = {
        "timestamp": now_s(),
        "sessions": {}
    }
    for sess in sessions:
        rx_age = (now_s() - sess.last_rx_time) if sess.last_rx_time > 0 else None
        out["sessions"][sess.cfg.name] = {
            "session_id": sess.cfg.session_id,
            "state": sess.state.name,
            "state_since": sess.state_since,
            "uptime_s": now_s() - sess.state_since if sess.state_since > 0 else 0,
            "tx_seq": sess.tx_seq,
            "last_rx_seq": sess.last_rx_seq,
            "last_rx_age_s": rx_age,
            "consecutive_miss": sess.consecutive_miss,
            "consecutive_hit": sess.consecutive_hit,
            "rtt_ms": round(sess.rtt_ewma_us / 1000.0, 2) if sess.rtt_ewma_us > 0 else None,
            "loss_pct": round(sess.loss_ewma_pct, 2),
            "peer": (f"{sess.peer_sockaddr[0]}:{sess.peer_sockaddr[1]}"
                     if sess.peer_sockaddr
                     else f"{sess.cfg.peer_host}:{sess.cfg.peer_port}"),
            "iface": sess.cfg.local_iface,
        }

    tmp = state_path.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(out, f, indent=2)
        os.replace(tmp, state_path)
    except (PermissionError, OSError) as e:
        _warn_state_publish_broken(f"state file write to {state_path} failed", e)
        return
    # A clean write after a failure: announce recovery once, so the log shows a
    # matched broken/recovered pair rather than an unexplained silence.
    if write_state_file._broken:
        logging.warning("state publishing recovered: writing %s again", state_path)
        write_state_file._broken = False


# Tracks whether state publishing is currently failing, so the warnings above
# fire on the broken->working edges only and never flood at the ~1/s write rate.
write_state_file._broken = False


def _warn_state_publish_broken(what: str, err: Exception):
    """Warn once on the transition into a state-publishing failure."""
    if not write_state_file._broken:
        logging.warning("%s: %s — WAN state is not being published; readers "
                        "will see no fresh state until this recovers", what, err)
        write_state_file._broken = True

# -- Optional HTTP /state listener --------------------------------------------

def start_state_listener(cfg: DaemonConfig):
    """If cfg.state_listen is set, spawn a thread serving GET /state.

    Returns the bound HTTPServer (or None if disabled). Caller does not need
    to hold a reference; the thread is daemonized.
    """
    if not cfg.state_listen:
        return None

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state_path = Path(cfg.state_file)

    class Handler(BaseHTTPRequestHandler):
        # sbfd-ctl polls this once per second across the management overlay. On
        # HTTP/1.0 every poll paid a TCP handshake and teardown -- more bytes
        # than the sub-1KB body itself over a metered link, plus an extra RTT
        # of staleness in a failover input. Keep-alive needs an accurate
        # Content-Length on every response: do_GET sets one, and send_error
        # sets its own plus `Connection: close`.
        protocol_version = "HTTP/1.1"
        # ThreadingHTTPServer holds a thread for a connection's whole life and
        # the default timeout is None (unbounded blocking read), so an
        # abandoned connection would pin one forever. handle_one_request turns
        # this timeout into a close.
        timeout = 30
        # Required for keep-alive to actually be faster. The handler writes
        # headers and body as two separate small writes; with Nagle on, the
        # second waits for an ACK while the client (http.client sets
        # TCP_NODELAY) sits on its 40ms delayed-ACK timer. The HTTP/1.0 close
        # used to flush it -- on a persistent connection every response would
        # pay ~40ms, measurably slower than the handshake it replaced.
        disable_nagle_algorithm = True

        def log_message(self, fmt, *args):
            logging.debug("state-http %s - %s", self.address_string(), fmt % args)

        def do_GET(self):
            if self.path != "/state":
                self.send_error(404, "not found")
                return
            try:
                data = state_path.read_bytes()
            except FileNotFoundError:
                self.send_error(503, "state file not yet written")
                return
            except OSError as e:
                self.send_error(500, f"read error: {e}")
                return
            # The on-disk file is indent=2 so an operator can read
            # /run/sbfd/state.json directly; the wire copy is compacted. About
            # 30% of the pretty payload is whitespace, and sbfd-ctl fetches
            # this once per second across the management overlay -- which in a
            # real deployment rides the metered WAN links. Fall back to the raw
            # bytes if it will not parse, so a malformed file is still served
            # verbatim (unchanged behavior) instead of becoming a 500.
            try:
                data = json.dumps(json.loads(data), separators=(",", ":")).encode()
            except (ValueError, UnicodeDecodeError):
                pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    if ":" not in cfg.state_listen:
        logging.error("state_listen must be host:port, got %r; HTTP listener disabled",
                      cfg.state_listen)
        return None
    host_str, port_str = cfg.state_listen.rsplit(":", 1)
    host = host_str.strip("[]")  # tolerate bracketed IPv6
    try:
        port = int(port_str)
    except ValueError:
        logging.error("state_listen port %r is not numeric; HTTP listener disabled", port_str)
        return None
    # IP_FREEBIND lets us bind to an address that doesn't yet exist on any
    # interface — e.g. the management overlay's 100.x address before the overlay daemon has finished
    # bringing the overlay interface up at boot. Without this, sbfd loses the boot race
    # against the overlay daemon and silently fails to expose /state until restart.
    class FreebindHTTPServer(ThreadingHTTPServer):
        def server_bind(self):
            ip_freebind = getattr(socket, "IP_FREEBIND", 15)
            try:
                self.socket.setsockopt(socket.IPPROTO_IP, ip_freebind, 1)
            except OSError as e:
                logging.warning("IP_FREEBIND setsockopt failed: %s; bind may fail "
                                "if address not yet assigned", e)
            super().server_bind()

    try:
        httpd = FreebindHTTPServer((host, port), Handler)
    except OSError as e:
        logging.warning("state HTTP listener bind failed (%s:%d): %s; continuing without it",
                        host, port, e)
        return None
    t = threading.Thread(target=httpd.serve_forever, name="state-http", daemon=True)
    t.start()
    logging.info("state HTTP listener bound to %s:%d", host, port)
    return httpd

# -- Main loop ---------------------------------------------------------------

def run(cfg: DaemonConfig):
    start_state_listener(cfg)
    sessions = []
    for sc in cfg.sessions:
        sock = make_session_socket(sc, cfg.bind_host, cfg.bind_port)
        sess = Session(cfg=sc, sock=sock, window_start=now_s())
        sessions.append(sess)
        logging.info("session %s configured: id=%d peer=%s:%d iface=%s",
                     sc.name, sc.session_id, sc.peer_host, sc.peer_port, sc.local_iface)

    fd_to_sess = {s.sock.fileno(): s for s in sessions}

    last_state_write = 0.0
    next_tx_at = {s.cfg.session_id: now_s() for s in sessions}

    while True:
        # Compute next event time
        now = now_s()
        soonest_tx = min(next_tx_at.values())
        timeout = max(0.0, soonest_tx - now)
        timeout = min(timeout, 0.1)  # cap so we run housekeeping at 10 Hz

        # Wait on sockets
        try:
            rlist, _, _ = select.select(list(fd_to_sess.keys()), [], [], timeout)
        except InterruptedError:
            continue

        for fd in rlist:
            sess = fd_to_sess[fd]
            try:
                while True:
                    data, src = sess.sock.recvfrom(2048)
                    pkt = unpack_packet(data)
                    if pkt is not None:
                        on_rx(sess, pkt, src)
            except BlockingIOError:
                pass
            except OSError as e:
                logging.debug("session %s recv error: %s", sess.cfg.name, e)

        now = now_s()

        # Send due packets, run timeout checks
        rebuilt_any = False
        for sess in sessions:
            if now >= next_tx_at[sess.cfg.session_id]:
                send_packet(sess)
                next_tx_at[sess.cfg.session_id] = now + sess.cfg.tx_interval_ms / 1000.0
                check_timeouts(sess)
                update_loss_ewma(sess)
                rebuilt_any |= maybe_rebuild_socket(sess, cfg)

        # A rebuild retires a file descriptor select() is still watching.
        if rebuilt_any:
            fd_to_sess = {s.sock.fileno(): s for s in sessions if s.sock is not None}

        # Periodic state file write
        if now - last_state_write >= cfg.state_write_interval_s:
            write_state_file(cfg, sessions)
            last_state_write = now

# -- Config loading ----------------------------------------------------------

def load_config(path: str) -> DaemonConfig:
    with open(path) as f:
        raw = json.load(f)
    sessions = []
    for s in raw.get("sessions", []):
        sessions.append(SessionConfig(
            session_id=s["session_id"],
            name=s["name"],
            local_iface=s.get("local_iface"),
            peer_host=s["peer_host"],
            peer_port=s["peer_port"],
            tx_interval_ms=s.get("tx_interval_ms", 500),
            detect_mult=s.get("detect_mult", 3),
        ))
    return DaemonConfig(
        bind_host=raw.get("bind_host", "0.0.0.0"),
        bind_port=raw.get("bind_port", 3784),
        state_file=raw.get("state_file", "/run/sbfd/state.json"),
        state_write_interval_s=raw.get("state_write_interval_s", 1.0),
        state_listen=raw.get("state_listen"),
        sessions=sessions,
    )

def main():
    ap = argparse.ArgumentParser(description="sbfd - software BFD daemon")
    ap.add_argument("-c", "--config", required=True, help="path to JSON config")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config(args.config)
    if not cfg.sessions:
        logging.error("no sessions configured")
        sys.exit(1)

    try:
        run(cfg)
    except KeyboardInterrupt:
        logging.info("shutting down")

if __name__ == "__main__":
    main()
