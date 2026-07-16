#!/usr/bin/env python3
"""Daily maintenance reboot for the two WANs.

Reboots wan1 (cellular hotspot, via hotspot_watchdog's admin-API client) and
then wan2 (satellite terminal, via grpcurl), sequentially and only ever one at
a time, so at least one WAN is up by construction. Fired hourly by a systemd
timer; exits immediately unless the current local hour is the operator's
configured hour, which lets the schedule be changed from the UI without
rewriting the unit.

Silent on a normal night: while a leg is in flight it publishes a maintenance
window that sbfd-ctl reads to suppress that WAN's alerts, and it holds that
window open across the post-reboot settle, when a freshly booted link is still
flapping. It pages only when a WAN goes down and fails to come back; a reboot
that was never issued left the link untouched, and says so quietly."""
import argparse
import base64
import enum
import fcntl
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Optional

log = logging.getLogger("maintenance-reboot")

DEFAULT_NOTIFY = "/usr/local/sbin/spool-notify"
DEFAULT_LOCK = "/run/sbfd-ctl/maintenance.lock"
PUBLISHED_MAX_AGE_S = 60.0

# hotspot_watchdog.py --scheduled-reboot's exit-code contract, mirrored rather
# than imported: the watchdog runs as a SUBPROCESS, so what this module can
# actually rely on is the code on the wire, and a test pins the two copies
# together. The distinction that matters is between WD_UNTOUCHED and
# WD_ATTEMPTED_UNKNOWN — see reboot_wan1.
WD_ISSUED = 0
WD_UNTOUCHED = 1
WD_SKIPPED = 2
WD_ATTEMPTED_UNKNOWN = 3


class Outcome(enum.Enum):
    """How a leg ended, structurally.

    The question that decides what the operator hears is "was the WAN's link
    actually disturbed?", and the answer must never be inferred by
    prefix-matching a human sentence, nor by the RESULT OF A REQUEST. Both are
    guesses about the link rather than observations of it, and both guess wrong
    in the direction that hurts: a rejected reboot request reads like a failure
    though the WAN never went down, and a request that failed because the device
    is already dead reads like "nothing was disturbed" though the WAN is off the
    air. Every classification here is made by looking at BFD — see
    classify_by_link."""

    RECOVERED = "recovered"        # rebooted, and observed back up
    SKIPPED = "skipped"            # deliberately not touched; the link is fine
    NOT_ISSUED = "not_issued"      # reboot didn't take; the link is still UP
    NOT_RETURNED = "not_returned"  # the WAN went down and did not come back


class LegResult(NamedTuple):
    """What a leg reports back. `ok` and `reason` are the human-facing pair;
    `status` is what the caller branches on."""

    ok: bool
    status: Outcome
    reason: str


def _leg(status: Outcome, reason: str) -> LegResult:
    return LegResult(status is Outcome.RECOVERED, status, reason)


@dataclass
class Wan1Cfg:
    iface: str
    watchdog_bin: str
    watchdog_config: str


@dataclass
class Wan2Cfg:
    iface: str
    grpcurl_bin: str
    addr: str
    min_uptime_s: float


@dataclass
class MrConfig:
    published_state: str
    sbfd_state_path: str
    window_path: str
    lock_path: str
    wan1: Wan1Cfg
    wan2: Wan2Cfg
    recovery_deadline_s: float
    settle_s: float
    notify_bin: str
    notify_topic: str
    dry_run: bool
    control_topic: str = ""
    ntfy_auth_path: str = "/etc/spool-notify.auth"


def load_config(path: str) -> MrConfig:
    with open(path) as f:
        raw = json.load(f)
    w1, w2 = raw["wan1"], raw["wan2"]
    cfg = MrConfig(
        published_state=raw.get("published_state", "/run/sbfd-ctl/state.json"),
        sbfd_state_path=raw.get("sbfd_state_path", "/run/sbfd/state.json"),
        window_path=raw.get("window_path",
                            "/run/sbfd-ctl/maintenance_window.json"),
        lock_path=raw.get("lock_path", DEFAULT_LOCK),
        wan1=Wan1Cfg(iface=w1.get("iface", "wan1"),
                     watchdog_bin=w1["watchdog_bin"],
                     watchdog_config=w1["watchdog_config"]),
        wan2=Wan2Cfg(iface=w2.get("iface", "wan2"),
                     grpcurl_bin=w2.get("grpcurl_bin",
                                        "/usr/local/bin/grpcurl"),
                     addr=w2["addr"],
                     min_uptime_s=float(w2.get("min_uptime_s", 43200))),
        recovery_deadline_s=float(raw.get("recovery_deadline_s", 600)),
        settle_s=float(raw.get("settle_s", 30)),
        notify_bin=raw.get("notify_bin", DEFAULT_NOTIFY),
        notify_topic=raw.get("notify_topic", "pathfuse"),
        dry_run=bool(raw.get("dry_run", True)),
        control_topic=raw.get("control_topic", ""),
        ntfy_auth_path=raw.get("ntfy_auth_path", "/etc/spool-notify.auth"),
    )
    # The two ifaces must be real and DISTINCT. peer_of() pairs each WAN with
    # the other one; if both names are the same string it hands a WAN back
    # ITSELF as its own peer, and peer_is_up() — the never-both-down guard —
    # degenerates into "is the WAN I am about to reboot currently up?", which
    # is true precisely when it is about to stop being true.
    for name, iface in (("wan1.iface", cfg.wan1.iface),
                        ("wan2.iface", cfg.wan2.iface)):
        if not isinstance(iface, str) or not iface.strip():
            raise ValueError(f"{name} must be a non-empty string")
    if cfg.wan1.iface == cfg.wan2.iface:
        raise ValueError("wan1.iface and wan2.iface must differ — a WAN "
                         "cannot be its own peer")
    # The leg LABELS are load-bearing, not just cosmetic: `--only {wan1,wan2}`,
    # the start-gate's `set(legs) | {w1, w2}` peer check, the ntfy control
    # allow-list (`reboot-<wan>`), and the scoped sudoers all key off the
    # literal names "wan1"/"wan2". An iface renamed to anything else would
    # quietly POST a command outside the allow-list — a dead button, no error.
    # Pin the invariant every valid config already satisfies.
    if cfg.wan1.iface != "wan1" or cfg.wan2.iface != "wan2":
        raise ValueError(
            "wan1.iface / wan2.iface must be exactly 'wan1' / 'wan2': the "
            "maintenance-reboot leg labels (CLI --only, the ntfy allow-list "
            "commands, the scoped sudoers, and the start-gate) are fixed to "
            "these names")
    # control_topic is interpolated verbatim into the ntfy `Actions:` curl
    # header built by _reboot_button, so a stray comma / space / CRLF from an
    # operator typo could malform (or inject a second) header field. No
    # message-derived value ever reaches it — this is operator self-harm, not
    # an attacker path — but pin the charset the topic is meant to be anyway.
    # Empty stays valid: empty control_topic means "no button", by design.
    if cfg.control_topic and not re.fullmatch(r"[A-Za-z0-9_-]+", cfg.control_topic):
        raise ValueError(
            "control_topic must contain only [A-Za-z0-9_-] (or be empty for "
            "no reboot button): it is interpolated into the ntfy Actions header")
    # Finiteness before positivity: json.loads accepts bareword Infinity/NaN,
    # and every comparison against NaN is False, so a NaN deadline would sail
    # through a bare `<= 0` check and then make await_up's bound meaningless.
    for k in ("recovery_deadline_s", "settle_s"):
        v = getattr(cfg, k)
        if not math.isfinite(v):
            raise ValueError(f"{k} must be finite")
        if v <= 0:
            raise ValueError(f"{k} must be > 0")
    if not math.isfinite(cfg.wan2.min_uptime_s):
        raise ValueError("wan2.min_uptime_s must be finite")
    if cfg.wan2.min_uptime_s < 0:
        raise ValueError("wan2.min_uptime_s must be >= 0")
    return cfg


def _fresh(ts, now: float, max_age_s: float) -> bool:
    """Is `ts` a usable timestamp no older (and no newer) than max_age_s?

    Rejects bools — isinstance(True, int) is True in Python, and a JSON `true`
    must never be read as "one second past the epoch". Rejects non-finite
    values, which json.loads happily produces from bareword NaN/Infinity: a
    NaN timestamp makes `abs(now - ts) > max_age_s` False, so an arbitrarily
    stale file would read as FRESH, and a stale file is how a WAN that is
    already down gets read as a phantom UP."""
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    if not math.isfinite(ts):
        return False
    age = abs(now - ts)
    return math.isfinite(age) and age <= max_age_s


def read_published(path: str, now: float,
                   max_age_s: float = PUBLISHED_MAX_AGE_S) -> Optional[dict]:
    """sbfd-ctl's published state, or None if absent/stale/unparseable.

    Fail-safe: a schedule we cannot confirm is current is not a licence to
    reboot, so every failure mode here means "skip tonight"."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if not _fresh(raw.get("ts", raw.get("timestamp")), now, max_age_s):
        return None
    return raw


def should_run(published: dict, now_local_hour: int) -> tuple:
    """(ok, reason). The timer fires hourly; this is the gate that makes it
    daily, so the hour can move from the UI without a daemon-reload."""
    m = (published or {}).get("maintenance")
    if m is None:
        m = {}
    if not isinstance(m, dict):
        return False, f"published maintenance is not an object ({m!r})"
    if not m.get("configured"):
        return False, "maintenance reboot not configured"
    if not m.get("enabled"):
        return False, "maintenance reboot disabled by operator"
    hour = m.get("hour")
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        return False, f"published hour is not valid ({hour!r})"
    if now_local_hour != hour:
        return False, f"not the configured hour ({now_local_hour} != {hour})"
    return True, f"hour {hour}"


def read_wan_states(path: str, now: float, max_age_s: float = 30.0) -> dict:
    """iface -> BFD state. Empty dict when the file is missing or stale, which
    every caller must treat as "not UP"."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    if not _fresh(raw.get("timestamp"), now, max_age_s):
        return {}
    sessions = raw.get("sessions")
    if not isinstance(sessions, dict):
        return {}
    out = {}
    for s in sessions.values():
        if not isinstance(s, dict):
            continue
        iface = s.get("iface")
        if not isinstance(iface, str):
            continue
        out[iface] = s.get("state", "UNKNOWN")
    return out


def peer_of(cfg: MrConfig, wan: str) -> str:
    if wan == cfg.wan1.iface:
        return cfg.wan2.iface
    if wan == cfg.wan2.iface:
        return cfg.wan1.iface
    raise ValueError(f"unrecognized WAN iface {wan!r}")


GRPC_METHOD = "SpaceX.API.Device.Device/Handle"
GRPC_TIMEOUT_S = 20


def _as_number(v):
    """Coerce a protojson field to a number.

    grpcurl renders int64/uint64 fields (e.g. uptimeS) as JSON strings, not
    plain numbers, so a naive isinstance(v, (int, float)) check silently
    drops them. Accept a numeric string too. Reject bools explicitly —
    isinstance(True, int) is True in Python, and a bool must never be
    mistaken for a device-reported count. Anything else (None, list, dict,
    a non-numeric string) returns None.

    float() also accepts "nan"/"inf"/"-inf"/"Infinity" and only raises on
    genuinely non-numeric strings, so a string that parses must still be
    checked for finiteness: a non-finite value is not a number this module
    can trust, and callers (bootcount's int(n), uptime_s's threshold
    comparisons) must never see one."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not math.isfinite(v):
            return None
        return v
    if isinstance(v, str):
        try:
            n = float(v)
        except ValueError:
            return None
        return n if math.isfinite(n) else None
    return None


class DishClient:
    """Talks to the wan2 terminal's gRPC API by shelling out to grpcurl.

    grpcurl rather than a generated stub because the device serves protobuf
    reflection: no .proto files to vendor, nothing to re-sync when the vendor
    changes the schema, and no protobuf dependency in a stdlib-only repo."""

    def __init__(self, cfg: Wan2Cfg, runner=subprocess.run):
        self.cfg = cfg
        self._run = runner

    def _call(self, payload: dict) -> Optional[dict]:
        argv = [self.cfg.grpcurl_bin, "-plaintext",
                "-max-time", str(GRPC_TIMEOUT_S),
                "-d", json.dumps(payload),
                self.cfg.addr, GRPC_METHOD]
        try:
            r = self._run(argv, capture_output=True, text=True,
                          timeout=GRPC_TIMEOUT_S + 10)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("grpcurl failed: %s", e)
            return None
        if r.returncode != 0:
            log.warning("grpcurl rc=%s: %s", r.returncode,
                        (r.stderr or "").strip()[:200])
            return None
        try:
            resp = json.loads(r.stdout or "{}")
        except ValueError:
            log.warning("grpcurl returned non-JSON")
            return None
        if not isinstance(resp, dict):
            log.warning("grpcurl body is not an object (%r)", type(resp).__name__)
            return None
        return resp

    def status(self) -> Optional[dict]:
        """The status object, {} when the device reported none, or None when
        the payload is malformed.

        A malformed payload is treated as UNAVAILABLE, never as an empty-but-
        valid one: every level of this structure is attacker-shaped input from
        a device whose firmware changes under us, and `.get()` on a list is an
        AttributeError out of whichever accessor happened to touch it."""
        resp = self._call({"get_status": {}})
        if resp is None:
            return None
        st = resp.get("dishGetStatus")
        if st is None:
            return {}
        if not isinstance(st, dict):
            log.warning("dishGetStatus is not an object (%s)",
                        type(st).__name__)
            return None
        return st

    @staticmethod
    def _sub(st, key) -> Optional[dict]:
        """A nested object of the status payload, or None when it is malformed
        (which every caller must treat as "no reading", not as a default)."""
        v = st.get(key)
        if v is None:
            return {}
        if not isinstance(v, dict):
            log.warning("%s is not an object (%s)", key, type(v).__name__)
            return None
        return v

    def bootcount(self, st: Optional[dict] = None) -> Optional[int]:
        """The reboot receipt. BFD coming back says the path recovered; only a
        bumped bootcount says the device actually rebooted.

        `st` lets the caller read the count out of a snapshot it already has,
        so the baseline it compares against can be taken from the same status
        read as the reboot decision itself."""
        st = self.status() if st is None else st
        if not isinstance(st, dict) or not st:
            return None
        info = self._sub(st, "deviceInfo")
        if not info:
            return None
        n = _as_number(info.get("bootcount"))
        return int(n) if n is not None else None

    def uptime_s(self) -> Optional[float]:
        st = self.status()
        if not st:
            return None
        state = self._sub(st, "deviceState")
        if not state:
            return None
        n = _as_number(state.get("uptimeS"))
        return float(n) if n is not None else None

    def update_staged(self, st: Optional[dict] = None) -> bool:
        """A firmware update is staged and waiting for a reboot to apply it.
        Note swupdateRebootReady is omitted when false (proto3), so a missing
        key means False."""
        st = self.status() if st is None else st
        if not isinstance(st, dict) or not st:
            return False
        if st.get("swupdateRebootReady") is True:
            return True
        secs = _as_number(st.get("secondsUntilSwupdateRebootPossible"))
        return secs is not None and secs >= 0

    def update_in_flight(self, st: Optional[dict] = None) -> bool:
        """The device is fetching or writing firmware — do not touch it."""
        st = self.status() if st is None else st
        if not isinstance(st, dict) or not st:
            return False
        return st.get("softwareUpdateState") in ("FETCHING", "APPLYING")

    def reboot(self) -> bool:
        """True means grpcurl accepted and sent the reboot command, NOT that
        the device rebooted — the real proof is a bootcount delta, which the
        sequencer checks separately."""
        return self._call({"reboot": {}}) is not None

    def apply_update(self) -> bool:
        """Initiate the staged update; the device reboots as part of applying
        it. Preferred over a plain reboot when an update is staged — a plain
        reboot discards it, and the next night would find it staged again."""
        return self._call({"update": {"schedule_reboot": True}}) is not None


def acquire_lock(path: str) -> Optional[int]:
    """Take the exclusive, non-blocking run lock. Returns an open fd (hold it
    for the whole run; closing it releases the lock), or None if we did not get
    it.

    systemd stops the timer from overlapping ITSELF, but nothing stops an
    operator running `--now` by hand while the timer's run is in flight — and
    `--now` skips the schedule gate entirely. Two runs interleaved is how BOTH
    WANs go down at once: run A finishes leg 1 (wan1 back UP, both WANs read
    UP) and enters leg 2; run B's gate sees both UP and issues wan1's reboot;
    A's peer check still reads the stale pre-reboot UP for wan1 — the hotspot's
    admin API returns before the carrier drops — and issues wan2's. Both links
    then drop. B's close_window() would clobber A's window on the way in, too,
    so the resulting outage would page unsuppressed.

    Failing to CREATE the lock file counts as not holding it: we cannot prove
    we are the only run, and an absent observation must never authorize an
    irreversible act."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        log.warning("cannot open lock file %s: %s", path, e)
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:              # BlockingIOError when already held
        os.close(fd)
        log.info("lock %s is held by another run (%s)", path, e)
        return None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass                          # the lock is what matters, not the pid
    return fd


def release_lock(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        os.close(fd)                  # closing the fd releases the flock
    except OSError as e:
        log.warning("cannot release lock: %s", e)


def _atomic_write_text(path: str, body: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(body)
    os.replace(tmp, p)


def open_window(cfg: MrConfig, wan: str, now: float, ttl_s: float) -> None:
    """Declare that `wan` is about to be disturbed on purpose. sbfd-ctl reads
    this and suppresses that WAN's down/up/switch alerts until `until`. The TTL
    is a backstop: if this process dies mid-leg, suppression expires on its own
    rather than muting a WAN forever."""
    _atomic_write_text(cfg.window_path, json.dumps({
        "wan": wan, "until": now + ttl_s,
        "reason": "daily maintenance reboot",
    }))


WATCHDOG_TIMEOUT_S = 120           # the wan1 one-shot subprocess's own bound
GRPC_CALL_S = GRPC_TIMEOUT_S + 10  # what DishClient._call allows per round-trip
WINDOW_MARGIN_S = 60.0


def leg1_window_ttl(cfg: MrConfig) -> float:
    """A TTL that bounds the WHOLE wan1 leg, not just its recovery poll.

    The window must outlive the leg. `recovery_deadline_s + settle_s` did not:
    before await_up even starts, the leg can sit in the watchdog subprocess for
    its full timeout. A window that expires mid-leg un-suppresses exactly the
    post-reboot BFD flap it exists to hide, so a slow-but-successful night
    would page. (It fails open — a spurious alert, never a missed one — which
    is why this is a broken promise rather than a danger.)"""
    return (WATCHDOG_TIMEOUT_S + cfg.recovery_deadline_s + cfg.settle_s
            + WINDOW_MARGIN_S)


def leg2_window_ttl(cfg: MrConfig) -> float:
    """Same, for wan2, whose leg is mostly gRPC round-trips.

    Worst case, in order: three status/uptime round-trips before the decision,
    the reboot request, a full recovery deadline whose last extra_ok bootcount
    call can overrun it, the fallback's notify() and its reboot request, a
    second deadline that can overrun the same way — seven round-trips in all —
    and then the settle."""
    return (7 * GRPC_CALL_S + NOTIFY_TIMEOUT_S
            + 2 * cfg.recovery_deadline_s + cfg.settle_s + WINDOW_MARGIN_S)


def close_window(cfg: MrConfig) -> None:
    try:
        Path(cfg.window_path).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("cannot remove window file: %s", e)


NOTIFY_TIMEOUT_S = 30


def notify(cfg: MrConfig, title: str, priority: str, message: str,
           actions: str | None = None) -> None:
    """Never raises: a failed notification must not abort a reboot sequence.

    It does, though, have to LEAVE A TRACE. The spool helper exits 0 even when
    it merely spooled the message, so a nonzero exit means something local is
    broken, and discarding the CompletedProcess made a dropped page look
    exactly like a delivered one."""
    if cfg.dry_run:
        log.info("dry-run notify: %s | %s", title, message)
        return
    env = dict(os.environ, NOTIFY_TOPIC=cfg.notify_topic)
    if actions:
        env["NOTIFY_ACTIONS"] = actions
    try:
        r = subprocess.run([cfg.notify_bin, title, priority, message],
                           env=env, capture_output=True, text=True,
                           timeout=NOTIFY_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("notify failed: %s", e)
        return
    if r.returncode != 0:
        log.warning("notify exited %s: %s | %s", r.returncode,
                    title, (r.stderr or "").strip()[:200])


POLL_S = 5.0


def await_up(cfg: MrConfig, wan: str, deadline_s: float, extra_ok=None,
             sleep=time.sleep, clock=time.monotonic, states_fn=None,
             require_down_first: bool = False) -> bool:
    """Poll until `wan` is UP on BFD (and `extra_ok()` if given), or give up.

    extra_ok exists because BFD returning only proves the path came back — for
    wan2 we additionally require a bumped bootcount, which proves the device
    actually rebooted rather than the request being silently dropped.

    require_down_first is the same idea for a WAN that has no such receipt.
    Issuing a reboot is not the same as the device going down: the hotspot's
    admin API acknowledges the request and returns immediately, and the carrier
    can take longer to drop than the first poll takes to arrive. Accepting that
    first sample would mean accepting the STALE pre-reboot UP as proof of
    recovery, and the sequencer would then go on to reboot the other WAN while
    this one was still on its way down — both WANs off the air at once. So the
    caller can demand that `wan` be OBSERVED not-UP before any later UP counts:
    for a WAN with no reboot receipt, the observed down IS the receipt.

    Bounded by deadline_s in every case, including an extra_ok that never
    passes: this must not be able to hang the run."""
    states_fn = states_fn or read_wan_states
    seen_down = not require_down_first
    start = clock()
    while clock() - start < deadline_s:
        sleep(POLL_S)
        states = states_fn(cfg.sbfd_state_path, time.time())
        if not states:
            # read_wan_states returns {} for every unreadable case: file
            # missing, unparseable, stale past its freshness window, sbfd
            # restarting, the clock stepped under us. That is the ABSENCE of an
            # observation, not an observation of DOWN — and for a WAN whose
            # only reboot receipt IS the observed down, crediting it would
            # forge that receipt and re-open the stranding bug. Knowing nothing
            # advances nothing; poll again.
            continue
        if states.get(wan) != "UP":
            # A FRESH read in which the session is absent, or reports anything
            # but UP, is a real observation of not-UP. That still counts.
            seen_down = True
            continue
        if seen_down and (extra_ok is None or extra_ok()):
            return True
    return False


def peer_is_up(cfg: MrConfig, wan: str) -> bool:
    """The second, independent last-WAN-standing check.

    run_once already refuses to start unless BOTH WANs are UP, and it aborts
    the run if leg 1 does not recover — but neither of those catches a peer
    that dies on its own, for unrelated reasons, part-way through the night.
    So each leg re-reads BFD and re-checks its peer immediately before it
    issues a reboot. Anything other than a fresh UP (missing file, stale file,
    absent session) reads as "not UP" and the leg skips."""
    peer = peer_of(cfg, wan)
    states = read_wan_states(cfg.sbfd_state_path, time.time())
    return states.get(peer) == "UP"


def classify_by_link(cfg: MrConfig, wan: str, up_status: Outcome,
                     up_reason: str, down_reason: str) -> LegResult:
    """Classify a leg that did not recover by OBSERVING the link — never by the
    result of a request.

    A request's result says nothing about the link. `client.reboot()` can fail
    precisely BECAUSE the device is already dead (a firmware update bricked it,
    grpcurl cannot reach it), and calling that "not issued — the link was never
    disturbed" would swallow a real outage. Conversely, a WAN that stayed UP for
    the whole deadline while its reboot receipt never arrived was never
    disturbed at all, and paging "did not return" for a WAN that never left is
    a false page. Only BFD can tell the two apart, so ask BFD.

    up_status is what to report when the link is verifiably still UP —
    NOT_ISSUED for a reboot that did not take, SKIPPED when we deliberately
    declined to issue one. A link that is not UP is always NOT_RETURNED: it went
    down and has not come back, which is the one condition worth a page."""
    if read_wan_states(cfg.sbfd_state_path, time.time()).get(wan) == "UP":
        return _leg(up_status, up_reason)
    return _leg(Outcome.NOT_RETURNED, down_reason)


def reboot_wan1(cfg: MrConfig, now: float, runner=subprocess.run,
                sleep=time.sleep, clock=time.monotonic) -> LegResult:
    """Delegate to the watchdog's guarded one-shot: it owns the admin-API
    client and the carrier/peer guards already. We re-check the peer here too —
    the redundancy is deliberate, since this module, not the watchdog, is what
    decides to disturb a WAN tonight.

    The watchdog's one-shot returns as soon as the hotspot's admin API ACCEPTS
    the reboot, which is well before the device drops carrier. wan1 has no
    reboot receipt the way wan2 has a bootcount, so recovery is only credited
    once wan1 has been observed DOWN and then UP again — see await_up's
    require_down_first.

    Which exit codes go down that path is the whole safety story. TWO do:

      WD_ISSUED (0)            the restart was accepted, and
      WD_ATTEMPTED_UNKNOWN (3) the restart was POSTed and the answer was lost.

    Code 3 must be handled EXACTLY like code 0, because the commonest cause of
    it is a reboot that WORKED: the hotspot tears the connection down as it
    goes, taking the response with it, and its carrier then takes the better
    part of a minute to actually drop. Classifying it off BFD there and then
    would read the STALE pre-reboot UP, call wan1 untouched, and clear leg 2 to
    reboot the terminal — while wan1 was seconds from going down. Both WANs
    off the air. So on 3 we do not ask what the request returned; we go and
    WATCH the link, which is the only thing that can tell a landed reboot from
    a lost one. await_up(require_down_first=True) then sorts it out for us: a
    down-then-up is RECOVERED, a continuous UP for the whole deadline really
    was a reboot that never landed (NOT_ISSUED, and leg 2 is then safe), and a
    down that never returns is NOT_RETURNED, which pages and aborts.

    Only WD_UNTOUCHED (1) — the watchdog never got as far as POSTing anything —
    may be classified immediately, because there is no reboot in flight to be
    stale about."""
    w1 = cfg.wan1.iface
    if not peer_is_up(cfg, w1):
        return _leg(Outcome.SKIPPED,
                    f"skipped: peer {peer_of(cfg, w1)} is not UP — refusing "
                    f"to disturb the last standing WAN")
    argv = [cfg.wan1.watchdog_bin, "-c", cfg.wan1.watchdog_config,
            "--scheduled-reboot"]
    if cfg.dry_run:
        log.info("dry-run: would run %s", " ".join(argv))
        return _leg(Outcome.SKIPPED, "dry-run")
    try:
        r = runner(argv, capture_output=True, text=True,
                   timeout=WATCHDOG_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        # The watchdog invocation itself can fail to return precisely BECAUSE
        # the hotspot is already going down under it (the admin API call hangs
        # or the process is killed mid-request) — ask the link, not the result.
        return classify_by_link(
            cfg, w1, Outcome.NOT_ISSUED, f"watchdog invocation failed: {e}",
            f"{w1} is down and the watchdog could not reboot it: {e}")
    out = (r.stdout or "").strip()
    if r.returncode == WD_SKIPPED:
        return _leg(Outcome.SKIPPED, f"skipped by guard: {out}")
    if r.returncode not in (WD_ISSUED, WD_ATTEMPTED_UNKNOWN):
        # WD_UNTOUCHED, or any code this module does not recognise. The watchdog
        # never POSTed a restart, so nothing is in flight and BFD cannot be
        # stale on our account — classify off the link now. That still means
        # ASKING the link rather than trusting the exit code: the watchdog can
        # fail to reach an admin API precisely BECAUSE the hotspot is already
        # dead, and calling that "never disturbed" would swallow a real outage.
        return classify_by_link(
            cfg, w1, Outcome.NOT_ISSUED, f"reboot failed: {out}",
            "wan1 is down and the watchdog could not reboot it")
    # WD_ISSUED or WD_ATTEMPTED_UNKNOWN: a restart went out and wan1 may be on
    # its way down RIGHT NOW. The only honest reading of the link is one that
    # has seen it go down first.
    if await_up(cfg, w1, cfg.recovery_deadline_s, sleep=sleep, clock=clock,
                require_down_first=True):
        return _leg(Outcome.RECOVERED, "rebooted and recovered")
    # await_up gave up. Which of the two failures was it? Ask the link, not the
    # request: if wan1 is still UP it never dropped, and saying "did not return"
    # about a WAN that never left would be a false page.
    return classify_by_link(
        cfg, w1, Outcome.NOT_ISSUED,
        f"{w1} never went down within {int(cfg.recovery_deadline_s)}s — "
        f"no reboot observed",
        f"{w1} did not return within {int(cfg.recovery_deadline_s)}s")


def reboot_wan2(cfg: MrConfig, now: float, client=None,
                sleep=time.sleep, clock=time.monotonic) -> LegResult:
    """Reboot the terminal, preferring a staged firmware update when there is
    one. If the update does not apply, fall back to exactly one plain reboot."""
    client = client or DishClient(cfg.wan2)
    w2 = cfg.wan2.iface
    # Cheap pre-checks first, off an early snapshot: they exist to bail out
    # before we spend any more round-trips on a terminal we are not going to
    # touch tonight.
    st = client.status()
    if st is None:
        # "Unreachable" is not, on its own, a safe skip: the terminal can be
        # unreachable BECAUSE IT IS DOWN, and a silent rc-0 skip would swallow
        # a real wan2 outage. A skip is only honest if the LINK is verifiably
        # up — so ask the link, exactly as every other unproven case does.
        return classify_by_link(
            cfg, w2, Outcome.SKIPPED, "skipping: terminal unreachable",
            f"{w2} is down and the terminal is not answering")
    if client.update_in_flight(st):
        return _leg(Outcome.SKIPPED, "skipping: firmware update in flight")
    up = client.uptime_s()
    if up is not None and up < cfg.wan2.min_uptime_s:
        return _leg(Outcome.SKIPPED,
                    f"skipping: uptime {int(up)}s < minimum "
                    f"{int(cfg.wan2.min_uptime_s)}s — rebooted recently")

    # ---- the decision snapshot -----------------------------------------
    # Everything above came from ONE status read taken several seconds and
    # several gRPC round-trips ago. A firmware update that entered
    # FETCHING/APPLYING in that gap would not be in it — and interrupting a
    # firmware write is how terminals get bricked. An unnoticed reboot in that
    # gap would poison the receipt just as badly: `before` would be the
    # pre-reboot count, so the very next read would look like a bump we caused.
    # So in-flight, the bootcount baseline and the staged decision all come
    # from a FRESH read taken immediately before the irreversible act: only the
    # peer check (a local file read) and the dry-run branch sit between it and
    # the reboot.
    st = client.status()
    if st is None:
        return classify_by_link(
            cfg, w2, Outcome.SKIPPED,
            "skipping: terminal stopped answering before the reboot",
            f"{w2} is down and the terminal is not answering")
    if client.update_in_flight(st):
        return _leg(Outcome.SKIPPED, "skipping: firmware update in flight")
    before = client.bootcount(st)
    if before is None:
        # No readable receipt means no way to prove the device actually
        # restarted, so BFD coming back would be the only evidence — and that
        # only proves the path recovered. Fail safe: do not reboot blind. But
        # the bootcount can also be unreadable because the terminal is dying,
        # so this is a skip only if the link says so.
        return classify_by_link(
            cfg, w2, Outcome.SKIPPED,
            "skipping: bootcount unreadable — cannot verify a reboot",
            f"{w2} is down and its bootcount is unreadable")
    staged = client.update_staged(st)
    if cfg.dry_run:
        return _leg(Outcome.SKIPPED,
                    f"dry-run: would {'apply update' if staged else 'reboot'}")

    def rebooted():
        bc = client.bootcount()
        return bc is not None and bc > before

    # The peer re-check goes here, as late as it can: immediately before the
    # only irreversible act in this function.
    if not peer_is_up(cfg, w2):
        return _leg(Outcome.SKIPPED,
                    f"skipping: peer {peer_of(cfg, w2)} is not UP — "
                    f"refusing to disturb the last standing WAN")
    deadline = int(cfg.recovery_deadline_s)
    issued = client.apply_update() if staged else client.reboot()
    if not issued:
        # A rejected request does NOT mean the terminal is still up: the request
        # can fail precisely because the device is already dead. Ask the link.
        return classify_by_link(
            cfg, w2, Outcome.NOT_ISSUED, "reboot request failed",
            f"{w2} is down and the reboot request failed — the terminal is "
            f"not answering")
    if await_up(cfg, w2, cfg.recovery_deadline_s, extra_ok=rebooted,
                sleep=sleep, clock=clock):
        return _leg(Outcome.RECOVERED,
                    "update applied" if staged else "rebooted and recovered")
    if not staged:
        # await_up gave up for one of two reasons, and only the link says which:
        # the terminal is down and did not come back (an outage), or it stayed
        # UP throughout and the bootcount never bumped — the silently-dropped
        # reboot the receipt exists to catch, in which nothing was disturbed.
        return classify_by_link(
            cfg, w2, Outcome.NOT_ISSUED,
            f"{w2} stayed UP and its bootcount never bumped within "
            f"{deadline}s — the reboot was dropped",
            f"{w2} did not return within {deadline}s")
    # The staged update did not apply. One plain reboot, then verify again.
    # Time has passed since the last peer check — a whole recovery deadline of
    # it — and the fallback is a second chance to take out the last standing
    # WAN. Re-check the peer immediately before issuing it, exactly as above,
    # and BEFORE announcing a fallback we might not go on to issue.
    if not peer_is_up(cfg, w2):
        return classify_by_link(
            cfg, w2, Outcome.SKIPPED,
            f"skipping fallback reboot: peer {peer_of(cfg, w2)} is not UP",
            f"{w2} is down after the update and the fallback reboot was "
            f"declined: peer {peer_of(cfg, w2)} is not UP")
    notify(cfg, "📶 Terminal update did not apply", "default",
           "falling back to a plain reboot")
    if not client.reboot():
        # The classic bricked terminal: the update took it down for good, and
        # the fallback request fails BECAUSE it is dead. Under-paging this as
        # "not rebooted tonight" would leave a real outage unannounced.
        return classify_by_link(
            cfg, w2, Outcome.NOT_ISSUED, "fallback reboot request failed",
            f"{w2} is down after the update and the fallback reboot request "
            f"failed — the terminal is not answering")
    if await_up(cfg, w2, cfg.recovery_deadline_s, extra_ok=rebooted,
                sleep=sleep, clock=clock):
        return _leg(Outcome.RECOVERED, "update failed; plain reboot recovered")
    return classify_by_link(
        cfg, w2, Outcome.NOT_ISSUED,
        f"{w2} stayed UP and its bootcount never bumped after the fallback "
        f"reboot — the reboot was dropped",
        f"{w2} did not return after fallback reboot")


def _reboot_button(cfg: MrConfig, wan: str) -> str | None:
    """An ntfy `http` action that publishes `reboot-<wan>` to the control topic.
    Returns None (no button) when control is not configured or auth is
    unreadable — a missing button must never block the page."""
    if not cfg.control_topic:
        return None
    try:
        vals = {}
        with open(cfg.ntfy_auth_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith(("NTFY_USER=", "NTFY_PASS=", "NTFY_BASE=")):
                    k, _, v = line.partition("=")
                    vals[k] = v
        base = vals["NTFY_BASE"].rstrip("/")
        token = base64.b64encode(
            f'{vals["NTFY_USER"]}:{vals["NTFY_PASS"]}'.encode()).decode()
    except (OSError, KeyError) as e:
        log.warning("no reboot button (auth unreadable): %s", e)
        return None
    # base is interpolated verbatim into the Actions header below, so a comma,
    # semicolon, or any whitespace/control char in NTFY_BASE (an operator typo
    # in the auth file — not attacker- or message-derived) would malform or
    # inject a second action field. Treat that like unreadable auth: fail open
    # (no button, page still sent), consistent with the control_topic charset
    # guard in load_config.
    if any(c in base for c in ",;") or any(c.isspace() or ord(c) < 0x20
                                           for c in base):
        log.warning("no reboot button (NTFY_BASE has a delimiter/whitespace/"
                    "control char): %r", base)
        return None
    # Short-form http action; commas/semicolons in our values are absent by
    # construction (base is validated just above, topic is [A-Za-z0-9_-] via
    # load_config, token is base64). clear=true dismisses the notification on a
    # successful publish.
    return (f"http, Reboot {wan} now, {base}/{cfg.control_topic}, "
            f"method=POST, headers.Authorization=Basic {token}, "
            f"body=reboot-{wan}, clear=true")


def report_leg(cfg: MrConfig, wan: str, res: LegResult) -> None:
    """Tell the operator what happened to a leg that did not recover — and tell
    them the truth. Only a WAN that went down and stayed down is an outage worth
    a high-priority page; a reboot that was never issued left the link exactly
    where it was, and must not be announced as a WAN that did not come back."""
    button = _reboot_button(cfg, wan)
    if res.status is Outcome.NOT_RETURNED:
        notify(cfg, f"⚠️ {wan} did not return from maintenance reboot",
               "high", res.reason, actions=button)
    else:
        notify(cfg, f"📶 {wan} was not rebooted tonight", "default",
               res.reason, actions=button)


def run_once(cfg: MrConfig, now: float, sleep=time.sleep,
             legs=("wan1", "wan2")) -> int:
    """Leg 1 (wan1), then leg 2 (wan2), never both at once — and never leg 2
    while wan1 is known to be down. `sleep` is injectable so an end-to-end test
    never has to wait out the real settle.

    `legs` selects which leg(s) actually run — `("wan1",)` and `("wan2",)`
    reboot a single WAN on demand, and the default `("wan1", "wan2")` is the
    nightly full cycle. Restricting `legs` never relaxes the safety gate: see
    the `required` set below.

    The caller MUST hold the run lock (see acquire_lock): the very first thing
    this does is delete any window file, which would clobber a concurrent run's
    live suppression window."""
    # A window file outlives the process that wrote it: a previous run that was
    # SIGKILLed (or a box that lost power mid-leg) leaves one behind, and the
    # early-skip paths below return without ever entering the try/finally that
    # would clear it. Clear it here, before any gate, so a stale window cannot
    # keep suppressing a WAN's alerts across skipped runs until its TTL expires.
    close_window(cfg)

    states = read_wan_states(cfg.sbfd_state_path, now)
    w1, w2 = cfg.wan1.iface, cfg.wan2.iface
    # The selected legs must be UP, and so must every OTHER WAN (the peer we
    # must not strand). For a full cycle that is both; for a single leg it is
    # the target plus its peer — never disturb the last standing WAN.
    required = set(legs) | {w1, w2}
    for wan in sorted(required):
        st = states.get(wan)
        if st is None:
            # No fresh observation AT ALL for this WAN: the state file is
            # missing, stale, or omits it. That is an ABSENCE, not a WAN we can
            # see is down — and the rule this whole sequencer turns on is that
            # an absent observation must never SILENCE a page. A state path that
            # quietly went missing is exactly how the nightly reboot no-op'd,
            # unnoticed, night after night. Page, then skip.
            log.warning("skipping: no fresh state for %s from %s — blind, "
                        "refusing to reboot", wan, cfg.sbfd_state_path)
            notify(cfg,
                   "⚠️ Maintenance reboot skipped — WAN state unreadable",
                   "high",
                   f"No fresh state for {wan} from {cfg.sbfd_state_path}. The "
                   f"nightly reboot was skipped rather than risk stranding a "
                   f"WAN. Check that sbfd's state directory exists and the "
                   f"daemon is publishing state.")
            return 0
        if st != "UP":
            log.info("skipping: %s is %s, not UP — refusing to disturb the "
                     "last standing WAN", wan, st)
            return 0

    # Exit 1 is reserved for an OUTAGE: a WAN we took down that did not come
    # back. A leg that never disturbed its link (NOT_ISSUED) is reported to the
    # operator but is NOT a unit failure — marking the unit `failed` in
    # `systemctl status` for a benign, link-untouched condition trains the
    # operator to ignore red, which is how a real outage gets missed.
    outage = False
    try:
        # Leg 1. A leg 1 that left wan1 DOWN aborts the run: never proceed to
        # wan2 while wan1 is still down. Carrying on with "the other WAN
        # anyway" is exactly how both WANs end up down at once.
        if "wan1" in legs:
            open_window(cfg, w1, now, leg1_window_ttl(cfg))
            try:
                res = reboot_wan1(cfg, now)
                # The settle happens INSIDE the window. A WAN that has just
                # booted is not done: its BFD session commonly flaps
                # UP/DOWN/UP as the link finishes coming up, and those
                # transitions are precisely what the window exists to
                # suppress. Closing at the first UP and settling afterwards
                # would fire alerts on a normal night.
                if res.ok:
                    sleep(cfg.settle_s)
            finally:
                close_window(cfg)
            log.info("wan1: %s", res.reason)
            if res.status is Outcome.NOT_RETURNED:
                # The one leg-1 outcome that stops the run: wan1 went down
                # and did not come back. Rebooting wan2 now would take out
                # the last WAN.
                report_leg(cfg, w1, res)
                return 1
            if not res.ok and res.status is not Outcome.SKIPPED:
                # NOT_ISSUED. classify_by_link only ever returns it with wan1
                # VERIFIABLY still UP on BFD — the reboot did not take (the
                # admin password rotated, say) and the link was never
                # disturbed. Report it, but do NOT abort: aborting would mean
                # wan2 never gets rebooted again either, silently
                # reinstating the very problem this feature exists to solve
                # (a terminal sitting on a staged firmware update forever)
                # for as long as wan1's reboot stays broken.
                report_leg(cfg, w1, res)

        # Leg 2. Note what does NOT hold here: a leg-1 SKIPPED does not prove
        # wan1 is up. The watchdog's own `no carrier` guard exits 2 — a skip —
        # precisely BECAUSE wan1 is broken. What makes leg 2 safe is not any
        # inference about leg 1 but leg 2's own peer_is_up() re-check, which
        # re-reads BFD immediately before it issues anything and refuses to
        # disturb wan2 unless wan1 is fresh-UP right then.
        if "wan2" in legs:
            open_window(cfg, w2, time.time(), leg2_window_ttl(cfg))
            try:
                res = reboot_wan2(cfg, time.time())
                if res.ok:
                    sleep(cfg.settle_s)
            finally:
                close_window(cfg)
            log.info("wan2: %s", res.reason)
            if not res.ok and res.status is not Outcome.SKIPPED:
                report_leg(cfg, w2, res)
                # Only a WAN that went down and stayed down is a failure of
                # the run.
                outage = res.status is Outcome.NOT_RETURNED
        return 1 if outage else 0
    finally:
        # A window left open would suppress that WAN's alerts indefinitely —
        # the one failure mode that could hide a real outage. Closed here on
        # every path: success, failure, and unexpected exception.
        close_window(cfg)


def _exit_on_sigterm(signum, frame):
    """`systemctl stop` (and a timer's own timeout) sends SIGTERM, whose default
    action kills the process outright — skipping run_once's finally and leaving
    the maintenance window open until its TTL expires, with that WAN's alerts
    suppressed the whole time. Raising SystemExit instead unwinds the stack, so
    the window is closed on the way out."""
    raise SystemExit(128 + signum)


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _exit_on_sigterm)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    install_signal_handlers()
    ap = argparse.ArgumentParser(prog="maintenance_reboot.py")
    ap.add_argument("-c", "--config", required=True)
    ap.add_argument("--now", action="store_true",
                    help="ignore the scheduled hour and run immediately")
    ap.add_argument("--dry-run", action="store_true",
                    help="force dry_run regardless of config")
    ap.add_argument("--only", choices=["wan1", "wan2"], default=None,
                    help="reboot only this WAN (default: full cycle)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.dry_run:
        cfg.dry_run = True

    # Held for the WHOLE run, released on every exit path. Without it, an
    # operator's `--now` can interleave with the timer's run and put both WANs
    # down at once (see acquire_lock). If we do not get it we do NOTHING: no
    # peer checks, no window deletion, no reboots — and exit 0, because a run
    # skipped for safety is a success, not a failure.
    lock = acquire_lock(cfg.lock_path)
    if lock is None:
        log.info("skipping: another maintenance run is in progress")
        return 0
    try:
        now = time.time()
        if not args.now:
            pub = read_published(cfg.published_state, now)
            if pub is None:
                log.info("skipping: published state missing or stale")
                return 0
            ok, why = should_run(pub, time.localtime(now).tm_hour)
            if not ok:
                log.info("skipping: %s", why)
                return 0
        legs = (args.only,) if args.only else ("wan1", "wan2")
        return run_once(cfg, now, legs=legs)
    finally:
        release_lock(lock)


if __name__ == "__main__":
    sys.exit(main())
