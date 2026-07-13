#!/usr/bin/env python3
"""Daily maintenance reboot for the two WANs.

Reboots wan1 (cellular hotspot, via hotspot_watchdog's admin-API client) and
then wan2 (satellite terminal, via grpcurl), sequentially and only ever one at
a time, so at least one WAN is up by construction. Fired hourly by a systemd
timer; exits immediately unless the current local hour is the operator's
configured hour, which lets the schedule be changed from the UI without
rewriting the unit.

Silent on a normal night: while a leg is in flight it publishes a maintenance
window that sbfd-ctl reads to suppress that WAN's alerts. It only speaks when a
WAN fails to come back."""
import argparse
import json
import logging
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("maintenance-reboot")

DEFAULT_NOTIFY = "/usr/local/sbin/spool-notify"
PUBLISHED_MAX_AGE_S = 60.0


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
    wan1: Wan1Cfg
    wan2: Wan2Cfg
    recovery_deadline_s: float
    settle_s: float
    notify_bin: str
    notify_topic: str
    dry_run: bool


def load_config(path: str) -> MrConfig:
    with open(path) as f:
        raw = json.load(f)
    w1, w2 = raw["wan1"], raw["wan2"]
    cfg = MrConfig(
        published_state=raw.get("published_state", "/run/sbfd-ctl/state.json"),
        sbfd_state_path=raw.get("sbfd_state_path", "/run/sbfd/state.json"),
        window_path=raw.get("window_path",
                            "/run/sbfd-ctl/maintenance_window.json"),
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
    )
    for k in ("recovery_deadline_s", "settle_s"):
        if getattr(cfg, k) <= 0:
            raise ValueError(f"{k} must be > 0")
    if cfg.wan2.min_uptime_s < 0:
        raise ValueError("wan2.min_uptime_s must be >= 0")
    return cfg


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
    ts = raw.get("ts", raw.get("timestamp"))
    if not isinstance(ts, (int, float)) or abs(now - ts) > max_age_s:
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
    ts = raw.get("timestamp")
    if not isinstance(ts, (int, float)) or abs(now - ts) > max_age_s:
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
        resp = self._call({"get_status": {}})
        if resp is None:
            return None
        return resp.get("dishGetStatus") or {}

    def bootcount(self) -> Optional[int]:
        """The reboot receipt. BFD coming back says the path recovered; only a
        bumped bootcount says the device actually rebooted."""
        st = self.status()
        if not st:
            return None
        bc = (st.get("deviceInfo") or {}).get("bootcount")
        n = _as_number(bc)
        return int(n) if n is not None else None

    def uptime_s(self) -> Optional[float]:
        st = self.status()
        if not st:
            return None
        up = (st.get("deviceState") or {}).get("uptimeS")
        n = _as_number(up)
        return float(n) if n is not None else None

    def update_staged(self, st: Optional[dict] = None) -> bool:
        """A firmware update is staged and waiting for a reboot to apply it.
        Note swupdateRebootReady is omitted when false (proto3), so a missing
        key means False."""
        st = self.status() if st is None else st
        if not st:
            return False
        if st.get("swupdateRebootReady") is True:
            return True
        secs = _as_number(st.get("secondsUntilSwupdateRebootPossible"))
        return secs is not None and secs >= 0

    def update_in_flight(self, st: Optional[dict] = None) -> bool:
        """The device is fetching or writing firmware — do not touch it."""
        st = self.status() if st is None else st
        if not st:
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


def close_window(cfg: MrConfig) -> None:
    try:
        Path(cfg.window_path).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("cannot remove window file: %s", e)


def notify(cfg: MrConfig, title: str, priority: str, message: str) -> None:
    """Never raises: a failed notification must not abort a reboot sequence."""
    if cfg.dry_run:
        log.info("dry-run notify: %s | %s", title, message)
        return
    env = dict(os.environ, NOTIFY_TOPIC=cfg.notify_topic)
    try:
        subprocess.run([cfg.notify_bin, title, priority, message],
                       env=env, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("notify failed: %s", e)


POLL_S = 5.0


def await_up(cfg: MrConfig, wan: str, deadline_s: float, extra_ok=None,
             sleep=time.sleep, clock=time.monotonic, states_fn=None) -> bool:
    """Poll until `wan` is UP on BFD (and `extra_ok()` if given), or give up.

    extra_ok exists because BFD returning only proves the path came back — for
    wan2 we additionally require a bumped bootcount, which proves the device
    actually rebooted rather than the request being silently dropped.

    Bounded by deadline_s in every case, including an extra_ok that never
    passes: this must not be able to hang the run."""
    states_fn = states_fn or read_wan_states
    start = clock()
    while clock() - start < deadline_s:
        sleep(POLL_S)
        states = states_fn(cfg.sbfd_state_path, time.time())
        if states.get(wan) == "UP" and (extra_ok is None or extra_ok()):
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


def reboot_wan1(cfg: MrConfig, now: float, runner=subprocess.run,
                sleep=time.sleep, clock=time.monotonic) -> tuple:
    """Delegate to the watchdog's guarded one-shot: it owns the admin-API
    client and the carrier/peer guards already. We re-check the peer here too —
    the redundancy is deliberate, since this module, not the watchdog, is what
    decides to disturb a WAN tonight."""
    w1 = cfg.wan1.iface
    if not peer_is_up(cfg, w1):
        return False, (f"skipped: peer {peer_of(cfg, w1)} is not UP — refusing "
                       f"to disturb the last standing WAN")
    argv = [cfg.wan1.watchdog_bin, "-c", cfg.wan1.watchdog_config,
            "--scheduled-reboot"]
    if cfg.dry_run:
        log.info("dry-run: would run %s", " ".join(argv))
        return False, "dry-run"
    try:
        r = runner(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"watchdog invocation failed: {e}"
    out = (r.stdout or "").strip()
    if r.returncode == 2:
        return False, f"skipped by guard: {out}"
    if r.returncode != 0:
        return False, f"reboot failed: {out}"
    if not await_up(cfg, w1, cfg.recovery_deadline_s, sleep=sleep, clock=clock):
        return False, (f"{w1} did not return within "
                       f"{int(cfg.recovery_deadline_s)}s")
    return True, "rebooted and recovered"


def reboot_wan2(cfg: MrConfig, now: float, client=None,
                sleep=time.sleep, clock=time.monotonic) -> tuple:
    """Reboot the terminal, preferring a staged firmware update when there is
    one. If the update does not apply, fall back to exactly one plain reboot."""
    client = client or DishClient(cfg.wan2)
    w2 = cfg.wan2.iface
    st = client.status()
    if st is None:
        return False, "skipping: terminal unreachable"
    if client.update_in_flight(st):
        return False, "skipping: firmware update in flight"
    up = client.uptime_s()
    if up is not None and up < cfg.wan2.min_uptime_s:
        return False, (f"uptime {int(up)}s < minimum "
                       f"{int(cfg.wan2.min_uptime_s)}s — rebooted recently")
    before = client.bootcount()
    if before is None:
        # No readable receipt means no way to prove the device actually
        # restarted, so BFD coming back would be the only evidence — and that
        # only proves the path recovered. Fail safe: skip rather than reboot
        # blind and then report a success we cannot stand behind.
        return False, "skipping: bootcount unreadable — cannot verify a reboot"
    staged = client.update_staged(st)
    if cfg.dry_run:
        return False, f"dry-run: would {'apply update' if staged else 'reboot'}"

    def rebooted():
        bc = client.bootcount()
        return bc is not None and bc > before

    # The peer re-check goes here, as late as it can: immediately before the
    # only irreversible act in this function.
    if not peer_is_up(cfg, w2):
        return False, (f"skipping: peer {peer_of(cfg, w2)} is not UP — "
                       f"refusing to disturb the last standing WAN")
    issued = client.apply_update() if staged else client.reboot()
    if not issued:
        return False, "reboot request failed"
    if await_up(cfg, w2, cfg.recovery_deadline_s, extra_ok=rebooted,
                sleep=sleep, clock=clock):
        return True, "update applied" if staged else "rebooted and recovered"
    if not staged:
        return False, (f"{w2} did not return within "
                       f"{int(cfg.recovery_deadline_s)}s")
    # The staged update did not apply. One plain reboot, then verify again.
    notify(cfg, "📶 Terminal update did not apply", "default",
           "falling back to a plain reboot")
    if not peer_is_up(cfg, w2):
        return False, (f"skipping fallback reboot: peer {peer_of(cfg, w2)} is "
                       f"not UP")
    if not client.reboot():
        return False, "fallback reboot request failed"
    if await_up(cfg, w2, cfg.recovery_deadline_s, extra_ok=rebooted,
                sleep=sleep, clock=clock):
        return True, "update failed; plain reboot recovered"
    return False, f"{w2} did not return after fallback reboot"


def _is_skip(why: str) -> bool:
    """A skipped leg is a success, not a failure: tonight's reboot is never
    worth risking the link. Skips are logged; only a WAN that fails to come
    back pages the operator."""
    return why.startswith(("dry-run", "skipped", "skipping", "uptime"))


def run_once(cfg: MrConfig, now: float, sleep=time.sleep) -> int:
    """Leg 1 (wan1), then leg 2 (wan2) — and leg 2 only ever with wan1 verified
    back UP. `sleep` is injectable so an end-to-end test never has to wait out
    the real settle."""
    states = read_wan_states(cfg.sbfd_state_path, now)
    w1, w2 = cfg.wan1.iface, cfg.wan2.iface
    for wan in (w1, w2):
        if states.get(wan) != "UP":
            log.info("skipping: %s is %s, not UP — refusing to disturb the "
                     "last standing WAN", wan, states.get(wan))
            return 0

    try:
        # Leg 1. A failed leg 1 aborts the run: never proceed to wan2 while
        # wan1 is still down. Carrying on with "the other WAN anyway" is
        # exactly how both WANs end up down at once.
        open_window(cfg, w1, now, cfg.recovery_deadline_s + cfg.settle_s)
        ok, why = reboot_wan1(cfg, now)
        close_window(cfg)
        if not ok:
            if not _is_skip(why):
                notify(cfg, f"⚠️ {w1} did not return from maintenance reboot",
                       "high", why)
                return 1
            # A skip means wan1 was never disturbed, so it is still up and
            # leg 2 may proceed — with its own peer re-check to confirm.
            log.info("wan1: %s", why)
        else:
            log.info("wan1: %s", why)
            sleep(cfg.settle_s)

        # Leg 2, reached only with wan1 either untouched or verified back UP.
        open_window(cfg, w2, time.time(),
                    cfg.recovery_deadline_s * 2 + cfg.settle_s)
        ok, why = reboot_wan2(cfg, time.time())
        close_window(cfg)
        if not ok and not _is_skip(why):
            notify(cfg, f"⚠️ {w2} did not return from maintenance reboot",
                   "high", why)
            return 1
        log.info("wan2: %s", why)
        return 0
    finally:
        # A window left open would suppress that WAN's alerts indefinitely —
        # the one failure mode that could hide a real outage. Closed here on
        # every path: success, failure, and unexpected exception.
        close_window(cfg)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="maintenance_reboot.py")
    ap.add_argument("-c", "--config", required=True)
    ap.add_argument("--now", action="store_true",
                    help="ignore the scheduled hour and run immediately")
    ap.add_argument("--dry-run", action="store_true",
                    help="force dry_run regardless of config")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.dry_run:
        cfg.dry_run = True

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
    return run_once(cfg, now)


if __name__ == "__main__":
    sys.exit(main())
