#!/usr/bin/env python3
"""hotspot_watchdog.py — wan1 hotspot auto-reboot watchdog for PathFuse.

Detects an unusable wan1 (private/NAT IP from the hotspot = cellular modem
detached, or wan1 BFD session DOWN), and after a dwell reboots the Netgear
Nighthawk M6 Pro via its admin API, with bounded retries and ntfy alerts.
Spec: docs/superpowers/specs/2026-07-05-hotspot-watchdog-design.md

--scheduled-reboot exit-code contract
------------------------------------
The maintenance sequencer drives this mode as a subprocess and decides, from
the exit code alone, whether it is safe to go on and reboot the OTHER WAN. The
two "no reboot happened" codes are therefore NOT interchangeable — collapsing
them is how both WANs end up down at once:

  0  ISSUED    the hotspot accepted the restart. wan1 is on its way down.
  1  UNTOUCHED the hotspot was PROVABLY never disturbed: we never got as far as
               POSTing the restart at all (admin UI unreachable, admin secret
               unreadable, login rejected). wan1 is exactly where it was.
  2  SKIPPED   a guard declined (no carrier / peer not UP / dry-run). Same
               guarantee as 1: nothing was disturbed.
  3  UNKNOWN   the restart WAS POSTed and we could not confirm what came back.

Code 3 is the subtle one, and it is not an edge case: it is the ORDINARY
outcome of a reboot that WORKED. The hotspot tears the connection down as it
goes, so the very success we are trying to observe is what destroys the
response we would observe it with. It is also what an unrecognised or
error-looking redirect target produces. Nothing in this process can tell a
landed reboot from a lost one, so 3 says exactly that and hands the question
to the only thing that can answer it: the link. The caller must go and WATCH
wan1 (maintenance_reboot enters await_up(require_down_first=True)) rather than
read its current, possibly stale, "UP" and call the WAN untouched.
"""

import argparse
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
from typing import Optional, Tuple

import notify as notify_mod

log = logging.getLogger("hotspot_watchdog")

# --scheduled-reboot's exit codes. See the module docstring for the contract;
# maintenance_reboot.py mirrors these (WD_ISSUED &c) and a test pins the two
# copies together.
EXIT_ISSUED = 0
EXIT_UNTOUCHED = 1
EXIT_SKIPPED = 2
EXIT_ATTEMPTED_UNKNOWN = 3

# -- Samplers -----------------------------------------------------------------


def is_rfc1918(ip: str) -> bool:
    """True for the three RFC1918 blocks (10/8, 172.16/12, 192.168/16).

    Written as integer octet tests rather than ip_network literals because the
    repo's push gate rejects IP literals outside the RFC-reserved example
    ranges. Deliberately NOT ipaddress.is_private: that also matches the
    100.64/10 shared-address block, which is exactly what a healthy cellular
    WAN gets from carrier-grade NAT — treating it as "private" would reboot a
    working link."""
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)


def parse_wan_ipv4(ip_json: str, iface: str):
    """First IPv4 on iface from `ip -j addr show` output, or None."""
    try:
        data = json.loads(ip_json)
    except (json.JSONDecodeError, TypeError):
        return None
    for entry in data if isinstance(data, list) else []:
        if entry.get("ifname") != iface:
            continue
        for ai in entry.get("addr_info", []):
            if ai.get("family") == "inet" and ai.get("local"):
                return ai["local"]
    return None


def read_wan_private(iface: str):
    """True/False = wan IPv4 is RFC1918; None = no IPv4 or `ip` failed."""
    try:
        out = subprocess.run(["ip", "-j", "addr", "show", iface],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("ip addr show %s failed: %s", iface, e)
        return None
    if out.returncode != 0:
        log.warning("ip addr show %s rc=%d", iface, out.returncode)
        return None
    ip = parse_wan_ipv4(out.stdout, iface)
    return None if ip is None else is_rfc1918(ip)


def parse_bfd_states(raw: str, iface: str, peer_iface: str, now: float,
                     max_age_s: float):
    """(wan_up, peer_up) as Optional[bool]; (None, None) on stale/invalid data."""
    try:
        d = json.loads(raw)
        ts = float(d["timestamp"])
        sessions = d["sessions"]
    except (json.JSONDecodeError, TypeError, KeyError, ValueError):
        return (None, None)
    if now - ts > max_age_s:
        return (None, None)
    if not isinstance(sessions, dict):
        return (None, None)
    states = {}
    for s in sessions.values():
        if isinstance(s, dict) and "iface" in s:
            states[s["iface"]] = (s.get("state") == "UP")
    return (states.get(iface), states.get(peer_iface))


def read_bfd_states(path: str, iface: str, peer_iface: str, now: float,
                    max_age_s: float):
    try:
        with open(path) as f:
            raw = f.read()
    except OSError:
        return (None, None)
    return parse_bfd_states(raw, iface, peer_iface, now, max_age_s)


def read_carrier(iface: str, sys_root="/sys/class/net"):
    """True/False = physical link presence; None = unknown (iface gone,
    admin-down, or read error). Unknown must not suppress reboots."""
    try:
        raw = Path(f"{sys_root}/{iface}/carrier").read_text().strip()
    except OSError:
        return None
    return {"1": True, "0": False}.get(raw)


@dataclass
class Sample:
    wan_private: bool
    wan_bfd_up: Optional[bool]
    peer_bfd_up: Optional[bool]
    wan_carrier: Optional[bool] = None


@dataclass
class Action:
    kind: str
    title: str = ""
    priority: str = ""
    message: str = ""


class _Stdin:
    """Marker for a field value curl must read from stdin, never from argv."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class NetgearClient:
    """Nighthawk M6 Pro admin API via curl subprocess (API v2.0, verified live).

    curl is used (not urllib) because reaching the hotspot's admin address
    requires binding to the wan iface (SO_BINDTODEVICE) — the main routing
    table sends that prefix out the default WAN.

    The admin password is handed to curl on STDIN (`--data-urlencode name@-`),
    never in argv: argv is world-readable in /proc, so an inline `name=value`
    leaks the secret to every process table on the box. curl URL-encodes the
    stdin bytes exactly as it would an inline value and keeps the field in its
    argv position, so the request on the wire is byte-for-byte what it was.
    """

    # A /Forms/config POST answers 302 for BOTH outcomes — the M6 honours the
    # posted ok_redirect and err_redirect — so "curl exited 0" says only that
    # the hotspot replied, not that it accepted the form. Judge the Location it
    # hands back instead: an error redirect names an error target.
    _ERROR_REDIRECT_RE = re.compile(r"error|err_|fail", re.IGNORECASE)

    def __init__(self, admin_url, iface, cookie_jar, curl_bin="curl",
                 timeout_s=10.0, runner=subprocess.run):
        self.admin_url = admin_url.rstrip("/")
        self.iface = iface
        self.cookie_jar = cookie_jar
        self.curl_bin = curl_bin
        self.timeout_s = timeout_s
        self.runner = runner

    @classmethod
    def _is_error_redirect(cls, url) -> bool:
        """True iff the hotspot redirected the POST at an error target. An empty
        Location is NOT an error: absence of evidence only."""
        return bool(url) and bool(cls._ERROR_REDIRECT_RE.search(url))

    def _curl(self, extra, stdin_data=None):
        # --fail: HTTP 4xx/5xx exits 22 instead of 0, so an error page from the
        # hotspot can't masquerade as a successful POST. 3xx still passes --
        # hence _is_error_redirect() above for the POSTs that answer with one.
        argv = [self.curl_bin, "-s", "--fail", "-m", str(self.timeout_s),
                "--interface", self.iface,
                "-c", self.cookie_jar, "-b", self.cookie_jar] + extra
        kw = {"capture_output": True, "text": True,
              "timeout": self.timeout_s + 5}
        if stdin_data is not None:
            kw["input"] = stdin_data     # no trailing newline: curl would encode it
        try:
            out = self.runner(argv, **kw)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("curl failed: %s", e)
            return None
        return out.stdout if out.returncode == 0 else None

    def _reset_cookies(self):
        """Drop any existing session cookie. Without this a STALE Admin cookie
        in the jar makes the post-login model.json read back userRole=Admin even
        when the password we just posted was rejected — a failed login reported
        as a success, which then credits a reboot that never happened."""
        try:
            Path(self.cookie_jar).unlink()
        except OSError:
            pass                          # absent (the normal case) or unremovable

    def fetch_model(self):
        out = self._curl(["-L", f"{self.admin_url}/api/model.json?internalapi=1"])
        if out is None:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            log.warning("model.json response was not JSON")
            return None

    def _sec_token(self):
        m = self.fetch_model()
        try:
            return m["session"]["secToken"], m
        except (TypeError, KeyError):
            return None, m

    def _post_config(self, token, fields):
        """POST fields to /Forms/config. Returns the redirect target the hotspot
        handed back (possibly ""), or None if the request itself failed."""
        data, stdin_data = [], None
        for k, v in [("token", token)] + fields:
            if isinstance(v, _Stdin):
                data += ["--data-urlencode", f"{k}@-"]
                stdin_data = v.value      # at most one such field per request
            else:
                data += ["--data-urlencode", f"{k}={v}"]
        # -o devnull + -w: the form's response body is a redirect stub we never
        # read, so swap stdout for the one thing we DO need to judge it.
        return self._curl(data + ["-o", os.devnull, "-w", "%{redirect_url}",
                                  f"{self.admin_url}/Forms/config"],
                          stdin_data=stdin_data)

    def login(self, password) -> bool:
        self._reset_cookies()
        token, _ = self._sec_token()
        if token is None:
            return False
        redirect = self._post_config(token, [
            ("session.password", _Stdin(password)),
            ("err_redirect", "/error.json"),
            ("ok_redirect", "/success.json")])
        if redirect is None or self._is_error_redirect(redirect):
            log.warning("admin login rejected (redirect=%r)", redirect)
            return False
        m = self.fetch_model()
        role = (m or {}).get("session", {}).get("userRole")
        return role == "Admin"

    def reboot(self) -> bool:
        token, _ = self._sec_token()
        if token is None:
            return False
        redirect = self._post_config(token, [("general.shutdown", "restart")])
        if redirect is None:
            log.warning("reboot POST got no usable HTTP response")
            return False
        if self._is_error_redirect(redirect):
            # The hotspot took the POST and bounced it at its error page: the
            # reboot did NOT happen. Reporting it as issued would both lie in
            # the alert and burn an attempt from the episode budget.
            log.warning("reboot POST redirected to an error target: %r", redirect)
            return False
        return True

    @staticmethod
    def diagnostics(model) -> str:
        if not model:
            return "hotspot state unavailable"
        wwan = model.get("wwan", {})
        power = model.get("power", {})
        return (f"wwan={wwan.get('connection')}/{wwan.get('connectionText')} "
                f"bars={wwan.get('signalStrength', {}).get('bars')} "
                f"battTemp={power.get('batteryTemperature')}C "
                f"tempCritical={power.get('deviceTempCritical')} "
                f"charge={power.get('battChargeLevel')}%")


class WatchdogPolicy:
    """Pure state machine. No I/O; `now` is always injected (wall clock)."""

    def __init__(self, *, dwell_s, grace_s, max_reboots_per_episode,
                 holdoff_s, healthy_reset_s, startup_grace_s, start_time):
        self.dwell_s = dwell_s
        self.grace_s = grace_s
        self.max_reboots = max_reboots_per_episode
        self.holdoff_s = holdoff_s
        self.healthy_reset_s = healthy_reset_s
        self.startup_grace_s = startup_grace_s
        self.start_time = start_time
        # episode state
        self.unusable_since = None      # start of current continuous unusable run
        self.episode_started = None     # first unusable moment of the fired episode
        self.healthy_since = None
        self.grace_until = 0.0
        self.holdoff_until = 0.0
        self.reboots_issued = 0
        self.degraded_notified = False
        self.ambiguous_notified = False
        self.gave_up_notified = False
        self.link_dead_notified = False

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _usable(s: Sample) -> bool:
        return not s.wan_private and s.wan_bfd_up is not False

    @staticmethod
    def _reboot_eligible(s: Sample) -> bool:
        return s.wan_private or (s.wan_bfd_up is False and s.peer_bfd_up is not False)

    def _episode_fired(self) -> bool:
        return self.degraded_notified

    def step(self, sample: Sample, now: float) -> list:
        if self._usable(sample):
            return self._step_usable(now)
        return self._step_unusable(sample, now)

    def _step_usable(self, now: float):
        self.unusable_since = None
        if self.healthy_since is None:
            self.healthy_since = now
        if self._episode_fired() and now - self.healthy_since >= self.healthy_reset_s:
            mins = (self.healthy_since - self.episode_started) / 60.0
            self._reset_episode()
            return [Action("notify", "wan1 recovered", "default",
                           f"wan1 healthy for {self.healthy_reset_s / 60:.0f}m "
                           f"(outage lasted ~{mins:.0f}m)")]
        return []

    def _step_unusable(self, sample: Sample, now: float):
        self.healthy_since = None
        if self.unusable_since is None:
            self.unusable_since = now
        if now - self.start_time < self.startup_grace_s:
            return []
        if now < self.grace_until or now < self.holdoff_until:
            return []
        if now - self.unusable_since < self.dwell_s:
            return []

        actions = []
        if not self.degraded_notified:
            self.degraded_notified = True
            self.episode_started = self.unusable_since
            actions.append(Action(
                "notify", "wan1 degraded", "high",
                f"wan1 unusable for {self.dwell_s / 60:.0f}m "
                f"(private_ip={sample.wan_private} bfd_up={sample.wan_bfd_up})"))
        if not self._reboot_eligible(sample):
            if not self.ambiguous_notified:
                self.ambiguous_notified = True
                actions.append(Action(
                    "notify", "wan1+wan2 both down", "high",
                    "skipping hotspot reboot (likely relay/upstream outage)"))
            return actions
        if sample.wan_carrier is False:
            # link itself is dead: the admin API is unreachable by definition,
            # so don't burn the reboot budget on guaranteed failures — alert
            # once and wait for carrier (or a human) to come back
            if not self.link_dead_notified:
                self.link_dead_notified = True
                actions.append(Action(
                    "notify", "hotspot link dead", "urgent",
                    "wan1 has no carrier — API reboot impossible; "
                    "check hotspot power/cable (manual intervention required)"))
            return actions
        if self.reboots_issued >= self.max_reboots:
            self.holdoff_until = now + self.holdoff_s
            self.reboots_issued = 0
            if not self.gave_up_notified:
                self.gave_up_notified = True
                actions.append(Action(
                    "notify", "hotspot watchdog giving up", "urgent",
                    f"{self.max_reboots} reboots did not restore wan1; "
                    f"holding off {self.holdoff_s / 3600:.1f}h"))
            return actions
        self.reboots_issued += 1
        self.grace_until = now + self.grace_s
        self.unusable_since = now + self.grace_s   # fresh dwell after grace
        actions.append(Action("reboot"))
        return actions

    _PERSIST_FIELDS = ("unusable_since", "episode_started", "healthy_since",
                       "grace_until", "holdoff_until", "reboots_issued",
                       "degraded_notified", "ambiguous_notified",
                       "gave_up_notified", "link_dead_notified")

    def to_dict(self):
        return {k: getattr(self, k) for k in self._PERSIST_FIELDS}

    @classmethod
    def from_dict(cls, d, **kwargs):
        p = cls(**kwargs)
        for k in cls._PERSIST_FIELDS:
            if k in d:
                setattr(p, k, d[k])
        return p

    def _reset_episode(self):
        self.unusable_since = None
        self.episode_started = None
        self.grace_until = 0.0
        self.holdoff_until = 0.0
        self.reboots_issued = 0
        self.degraded_notified = False
        self.ambiguous_notified = False
        self.gave_up_notified = False
        self.link_dead_notified = False


# -- Config / executor / daemon ------------------------------------------------

@dataclass
class WdConfig:
    iface: str
    peer_iface: str
    sbfd_state_path: str
    state_max_age_s: float
    admin_url: str
    secret_path: str
    poll_interval_s: float
    dwell_s: float
    grace_s: float
    max_reboots_per_episode: int
    holdoff_s: float
    healthy_reset_s: float
    startup_grace_s: float
    state_path: str
    notify_bin: str
    dry_run: bool


def load_config(path: str) -> WdConfig:
    with open(path) as f:
        raw = json.load(f)
    cfg = WdConfig(
        iface=raw.get("iface", "wan1"),
        peer_iface=raw.get("peer_iface", "wan2"),
        sbfd_state_path=raw.get("sbfd_state_path", "/run/sbfd/state.json"),
        state_max_age_s=float(raw.get("state_max_age_s", 30)),
        admin_url=raw["admin_url"],
        secret_path=raw["secret_path"],
        poll_interval_s=float(raw.get("poll_interval_s", 30)),
        dwell_s=float(raw.get("dwell_s", 600)),
        grace_s=float(raw.get("grace_s", 600)),
        max_reboots_per_episode=int(raw.get("max_reboots_per_episode", 3)),
        holdoff_s=float(raw.get("holdoff_s", 7200)),
        healthy_reset_s=float(raw.get("healthy_reset_s", 1800)),
        startup_grace_s=float(raw.get("startup_grace_s", 300)),
        state_path=raw.get("state_path", "/run/hotspot-watchdog/state.json"),
        notify_bin=raw.get("notify_bin", notify_mod.DEFAULT_COMMAND),
        dry_run=bool(raw.get("dry_run", True)),
    )
    # Finiteness FIRST: json.loads accepts the barewords NaN and Infinity, and
    # every comparison against NaN is False — so a NaN dwell_s sails straight
    # through a `<= 0` check and then makes `now - unusable_since < dwell_s`
    # false forever (the watchdog never fires) while a NaN grace_s makes the
    # holdoff comparisons false too (it fires every poll). Reject both here.
    for k in ("state_max_age_s", "poll_interval_s", "dwell_s", "grace_s",
              "holdoff_s", "healthy_reset_s", "startup_grace_s"):
        v = getattr(cfg, k)
        if not math.isfinite(v):
            raise ValueError(f"{k} must be a finite number, got {v!r}")
    for k in ("poll_interval_s", "dwell_s", "grace_s", "holdoff_s",
              "healthy_reset_s"):
        if getattr(cfg, k) <= 0:
            raise ValueError(f"{k} must be > 0")
    # These two may legitimately be 0 (no startup grace / accept any state age).
    for k in ("state_max_age_s", "startup_grace_s"):
        if getattr(cfg, k) < 0:
            raise ValueError(f"{k} must be >= 0")
    if cfg.max_reboots_per_episode < 1:
        raise ValueError("max_reboots_per_episode must be >= 1")
    return cfg


def notify(notify_bin, title, priority, message):
    """Send via spool-notify (it spools on failure itself); never raises."""
    try:
        subprocess.run([notify_bin, title, priority, message],
                       capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("spool-notify failed: %s", e)


def _read_secret(path):
    try:
        return Path(path).read_text().strip() or None
    except OSError as e:
        log.warning("cannot read secret %s: %s", path, e)
        return None


class Executor:
    """Turns policy Actions into side effects. Owns dry_run and API errors."""

    def __init__(self, cfg: WdConfig, client, notify_fn=notify):
        self.cfg = cfg
        self.client = client
        self.notify_fn = notify_fn

    def _notify(self, title, priority, message):
        log.info("notify: %s | %s", title, message)
        self.notify_fn(self.cfg.notify_bin, title, priority, message)

    def execute(self, actions):
        for a in actions:
            if a.kind == "notify":
                self._notify(a.title, a.priority, a.message)
            elif a.kind == "reboot":
                self._do_reboot()

    def _do_reboot(self):
        model = self.client.fetch_model()
        diag = self.client.diagnostics(model)
        if self.cfg.dry_run:
            self._notify("dry-run: would reboot hotspot", "high", diag)
            return
        if model is None:
            self._notify("hotspot reboot API error", "high",
                         f"admin UI unreachable at {self.cfg.admin_url} "
                         f"(no HTTP response)")
            return
        pw = _read_secret(self.cfg.secret_path)
        if pw is None:
            self._notify("hotspot reboot API error", "high",
                         f"cannot read admin secret "
                         f"({self.cfg.secret_path}); {diag}")
            return
        if not self.client.login(pw):
            self._notify("hotspot reboot API error", "high",
                         f"admin password rejected — check "
                         f"{self.cfg.secret_path}; {diag}")
            return
        if self.client.reboot():
            self._notify("hotspot reboot issued", "high", diag)
        else:
            self._notify("hotspot reboot API error", "high",
                         f"reboot POST failed; {diag}")


def _load_policy_state(cfg, kwargs):
    try:
        d = json.loads(Path(cfg.state_path).read_text())
        return WatchdogPolicy.from_dict(d, **kwargs)
    except (OSError, json.JSONDecodeError, TypeError):
        return WatchdogPolicy(**kwargs)


def _save_policy_state(cfg, policy) -> bool:
    """Persist episode state. Returns True iff it is durably on disk — callers
    gate the reboot on that (see _persist_then_execute)."""
    try:
        p = Path(cfg.state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(policy.to_dict()))
        tmp.replace(p)
        return True
    except OSError as e:
        log.warning("cannot persist state: %s", e)
        return False


def _persist_then_execute(cfg, policy, executor, actions):
    """Make the episode state durable BEFORE any reboot leaves the box.

    policy.step() has already consumed the attempt (reboots_issued += 1, grace
    armed) by the time it hands back a reboot Action. Persisting after the
    reboot went out — as this did — means a crash, an OOM kill or a power cut in
    that gap loses the increment: the daemon restarts, reloads a state file that
    never saw the attempt, and can exceed max_reboots_per_episode. So write
    first, and if the write is not durable, DROP the reboot. A missed reboot
    costs one dwell; an unbounded reboot loop costs the link.
    """
    saved = _save_policy_state(cfg, policy)
    if saved or not any(a.kind == "reboot" for a in actions):
        executor.execute(actions)
        return
    # The in-memory policy still holds the consumed attempt and the armed grace,
    # so this cannot spin: no further reboot is proposed until grace_s elapses.
    log.error("suppressing hotspot reboot: episode state is not durable (%s)",
              cfg.state_path)
    kept = [a for a in actions if a.kind != "reboot"]
    kept.append(Action("notify", "hotspot reboot suppressed", "urgent",
                       f"cannot persist watchdog state to {cfg.state_path} — "
                       f"refusing to reboot with an untracked budget"))
    executor.execute(kept)


def take_sample(cfg, now) -> Sample:
    private = read_wan_private(cfg.iface)
    wan_up, peer_up = read_bfd_states(cfg.sbfd_state_path, cfg.iface,
                                      cfg.peer_iface, now, cfg.state_max_age_s)
    return Sample(wan_private=bool(private), wan_bfd_up=wan_up,
                  peer_bfd_up=peer_up, wan_carrier=read_carrier(cfg.iface))


def _scheduled_reboot_verbose(cfg: WdConfig, client, now: float):
    """Implementation shared by scheduled_reboot() and the CLI. Returns
    (issued, reason, code) where `code` is the --scheduled-reboot exit code
    documented in the module docstring: EXIT_ISSUED, EXIT_UNTOUCHED,
    EXIT_SKIPPED or EXIT_ATTEMPTED_UNKNOWN.

    Only the CLI needs `code`; scheduled_reboot() below drops it to keep the
    (issued, reason) contract its other callers depend on. But the CALLER'S
    SAFETY lives in it, so the boundary between the codes is drawn with care:

      * Everything ABOVE client.reboot() is a case in which no restart was ever
        POSTed. Those are EXIT_UNTOUCHED (or EXIT_SKIPPED for a guard), and the
        caller may believe them: wan1 is where it was.

      * client.reboot() returning False is NOT such a case. By then we have
        logged in and posted the restart; what failed is our reading of the
        ANSWER. A hotspot that ACCEPTS the restart drops the connection as it
        goes down, which is precisely how that answer goes missing — so this is
        what a SUCCESSFUL reboot usually looks like from here. It is also what
        the 302-target heuristic produces when it misjudges an unfamiliar
        redirect. Reporting either as "never touched" would hand the caller a
        licence to go and reboot the other WAN while this one is on its way
        down. So: EXIT_ATTEMPTED_UNKNOWN, and let the link settle it.
    """
    if read_carrier(cfg.iface) is False:
        return (False, f"{cfg.iface} has no carrier — admin API unreachable",
                EXIT_SKIPPED)
    _wan_up, peer_up = read_bfd_states(cfg.sbfd_state_path, cfg.iface,
                                       cfg.peer_iface, now, cfg.state_max_age_s)
    if peer_up is not True:
        return (False, (f"peer {cfg.peer_iface} is not UP (={peer_up}) — "
                        f"refusing to disturb the last standing WAN"),
                EXIT_SKIPPED)
    if cfg.dry_run:
        return False, "dry-run: would reboot", EXIT_SKIPPED
    if client.fetch_model() is None:
        return (False, f"admin UI unreachable at {cfg.admin_url}",
                EXIT_UNTOUCHED)
    pw = _read_secret(cfg.secret_path)
    if pw is None:
        return (False, f"cannot read admin secret ({cfg.secret_path})",
                EXIT_UNTOUCHED)
    if not client.login(pw):
        return False, "login rejected — check the admin secret", EXIT_UNTOUCHED
    if not client.reboot():
        return (False, "reboot POST was attempted but its outcome is UNKNOWN "
                       "(no usable response) — the hotspot may well have taken "
                       "it and be going down now; watch the link",
                EXIT_ATTEMPTED_UNKNOWN)
    return True, "reboot issued", EXIT_ISSUED


def scheduled_reboot(cfg: WdConfig, client, now: float) -> Tuple[bool, str]:
    """One-shot, guarded reboot for the daily maintenance window.

    Deliberately does NOT read or write the daemon's policy state: a scheduled
    reboot is not an outage episode, and must neither consume the episode
    budget nor be consumed by a holdoff. Returns (issued, reason).

    `issued` is True only for a CONFIRMED reboot. It is False both for "never
    touched" and for "posted, outcome unknown", which is why the CLI (and the
    maintenance sequencer behind it) uses the exit code, not this bool."""
    issued, reason, _code = _scheduled_reboot_verbose(cfg, client, now)
    return issued, reason


_running = True


def _stop(signum, frame):
    global _running
    _running = False


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="hotspot_watchdog.py")
    ap.add_argument("-c", "--config", required=True)
    ap.add_argument("--once", action="store_true",
                    help="print one sample + verdict and exit")
    ap.add_argument("--test-login", action="store_true",
                    help="verify admin API login with the stored secret")
    ap.add_argument("--test-reboot", action="store_true",
                    help="login and reboot the hotspot NOW (supervised test)")
    ap.add_argument("--scheduled-reboot", action="store_true",
                    help="guarded one-shot reboot for the maintenance window. "
                         "Exit 0=reboot issued; 1=hotspot provably untouched "
                         "(never POSTed: admin UI unreachable, secret "
                         "unreadable, login rejected); 2=skipped by a guard; "
                         "3=restart was POSTed but its outcome is UNKNOWN — "
                         "which is what a reboot that WORKED looks like, since "
                         "the hotspot drops the connection as it goes down. On "
                         "3 the caller must watch the link, and must NOT treat "
                         "the WAN as undisturbed")
    args = ap.parse_args()
    cfg = load_config(args.config)
    client = NetgearClient(cfg.admin_url, cfg.iface,
                           "/run/hotspot-watchdog/cookies.txt")

    if args.once:
        s = take_sample(cfg, time.time())
        print(json.dumps({"wan_private": s.wan_private,
                          "wan_bfd_up": s.wan_bfd_up,
                          "peer_bfd_up": s.peer_bfd_up,
                          "wan_carrier": s.wan_carrier,
                          "usable": WatchdogPolicy._usable(s)}))
        return 0
    if args.test_login:
        pw = _read_secret(cfg.secret_path)
        if pw is None:
            print("FAIL: secret unreadable")
            return 1
        ok = client.login(pw)
        print("LOGIN OK" if ok else "LOGIN FAILED")
        return 0 if ok else 1
    if args.test_reboot:
        pw = _read_secret(cfg.secret_path)
        if pw is None or not client.login(pw):
            print("FAIL: login")
            return 1
        ok = client.reboot()
        print("REBOOT ISSUED" if ok else "REBOOT POST FAILED")
        return 0 if ok else 1
    if args.scheduled_reboot:
        # Separate session jar: the daemon may be mid-login with the shared one.
        # /run/hotspot-watchdog/ is normally provided by systemd's
        # RuntimeDirectory=, which systemd removes once the daemon stops --
        # create it here so a one-shot run with the daemon down still has
        # somewhere for curl -c to persist the login cookie.
        Path("/run/hotspot-watchdog").mkdir(parents=True, exist_ok=True)
        sc = NetgearClient(cfg.admin_url, cfg.iface,
                           "/run/hotspot-watchdog/cookies-scheduled.txt")
        _issued, reason, code = _scheduled_reboot_verbose(
            cfg, sc, time.time())
        print(reason)
        return code

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    start = time.time()
    pkw = dict(dwell_s=cfg.dwell_s, grace_s=cfg.grace_s,
               max_reboots_per_episode=cfg.max_reboots_per_episode,
               holdoff_s=cfg.holdoff_s, healthy_reset_s=cfg.healthy_reset_s,
               startup_grace_s=cfg.startup_grace_s, start_time=start)
    policy = _load_policy_state(cfg, pkw)
    executor = Executor(cfg, client)
    log.info("watchdog started (dry_run=%s iface=%s)", cfg.dry_run, cfg.iface)
    while _running:
        t0 = time.time()
        try:
            actions = policy.step(take_sample(cfg, t0), t0)
            # Persist every poll (not just when actions fire) so a daemon
            # restart mid-dwell doesn't reset the dwell timer; /run is
            # RAM-backed. This happens BEFORE the actions run because a reboot
            # action has already consumed budget that must not be lost — see
            # _persist_then_execute. Nothing in execute() mutates the policy, so
            # writing first records exactly the same state it used to.
            _persist_then_execute(cfg, policy, executor, actions)
        except Exception as e:  # noqa: BLE001 - keep the daemon alive
            log.error("poll error: %s", e)
        deadline = t0 + cfg.poll_interval_s
        while _running and time.time() < deadline:
            time.sleep(min(1.0, max(0.1, deadline - time.time())))
    log.info("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
