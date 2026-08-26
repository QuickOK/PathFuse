#!/usr/bin/env python3
"""
sbfd-ctl - failover controller for sbfd + engarde.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import fec_control
import fec_history
import fec_report
import notify

# -- Configuration -----------------------------------------------------------

VALID_MODES = {"full", "master_backup"}
VALID_POLICIES = {"static_primary", "dynamic", "static_configured"}
VALID_EGRESS_MODES = {"relay_vpn", "relay_direct", "local_direct"}


@dataclass
class WanCfg:
    iface: str
    session_id: int
    label: str


@dataclass
class RelayCfg:
    state_url: str
    fetch_interval_s: float = 1.0
    fetch_timeout_s: float = 2.0
    fec_url: Optional[str] = None


@dataclass
class EngardeCfg:
    server_ip: str
    server_port: int
    admin_url: Optional[str] = None


@dataclass
class NftCfg:
    table: str = "sbfd_ctl"
    family: str = "inet"


@dataclass
class EgressCfg:
    engarde_table: str = "engarde"
    wg_iface: str = "wg0"
    default_mode: str = "relay_vpn"


@dataclass
class PolicyCfg:
    default_mode: str = "full"
    default_master_policy: str = "static_primary"
    default_master_wan: str = "wan2"
    failback_hold_s: int = 30
    dynamic_rtt_margin_ms: float = 25.0
    dynamic_loss_margin_pct: float = 1.0
    dynamic_swap_dwell_s: float = 10.0
    manage_default_route: bool = False


@dataclass
class WanProfileCfg:
    """Per-WAN FEC policy: table + hysteresis + floor applied while this WAN
    is the fec driver. Defaults are the cellular spec values — a bare
    `"wan1": {}` profile is the intended common case."""
    name: str
    loss_table: list
    ramp_up_ticks: int
    ramp_down_hold_s: float
    floor_ratio: str
    signal_floor_fec: str


@dataclass
class FecCfg:
    enabled: bool
    fifo: str
    loss_table: list
    ramp_up_ticks: int
    ramp_down_hold_s: float
    full_mode_backoff_fec: str
    full_min_up_wans: int
    wire_unit: str = "udpspeeder-client"
    wire_stale_after_s: float = 30.0
    mode: str = fec_control.DEFAULT_MODE
    fixed_ratio: str = fec_control.DEFAULT_FIXED_RATIO
    floor_ratio: str = fec_control.DEFAULT_FLOOR_RATIO
    wan_profiles: dict = field(default_factory=dict)
    # Dwell on the DRIVER choice, mirroring policy.dynamic_swap_dwell_s for
    # the master. step_level damps the ratio, but nothing damped the driver, so
    # a momentary loss blip on the other link swapped the profile — and with it
    # the table and floor — beneath the adaptive engine. Only bites when more
    # than one WAN is active, i.e. full redundancy.
    driver_dwell_s: float = 120.0


@dataclass
class EnvironmentalCfg:
    enabled: bool
    auto_override_path: str
    auto_override_ttl_s: float = 180.0


@dataclass
class CellTelemetryCfg:
    """Reader side of cell_telemetry.py's state file. Thresholds live here
    (not in the collector) because the FEC controller is the one consumer."""
    state_path: str
    wan: str = "wan1"
    stale_after_s: float = 10.0
    rsrq_degrade_db: float = -12.0
    rsrq_recover_db: float = -10.0
    rsrp_degrade_dbm: float = -110.0
    rsrp_recover_dbm: float = -108.0
    handoff_path: str = "/run/sbfd-ctl/cell_handoff.json"
    handoff_ttl_s: float = 30.0


@dataclass
class LocationFecCfg:
    """Reader side of location_fec.py's floor file. `enabled` is the boot-time
    default; the operator's runtime toggle wins in either direction."""
    state_path: str
    enabled: bool = True
    stale_after_s: float = 30.0


@dataclass
class MaintenanceCfg:
    """Nightly one-at-a-time WAN reboot. `enabled`/`hour` here are boot-time
    DEFAULTS only — the operator's runtime overlay wins in either direction.
    sbfd-ctl resolves the pair and publishes it; maintenance_reboot.py reads
    the resolved values and never re-derives this precedence."""
    enabled: bool
    hour: int
    window_path: str = "/run/sbfd-ctl/maintenance_window.json"


@dataclass
class Config:
    wans: dict[str, WanCfg]
    relay: RelayCfg
    engarde: EngardeCfg
    nft: NftCfg
    policy: PolicyCfg
    ui_listen: str
    sbfd_local_state: str
    runtime_state: str
    persist_state: str
    published_state: str
    egress: EgressCfg = field(default_factory=EgressCfg)
    fec: Optional[FecCfg] = None
    environmental: Optional[EnvironmentalCfg] = None
    maintenance: Optional[MaintenanceCfg] = None
    cell: Optional[CellTelemetryCfg] = None
    location: Optional[LocationFecCfg] = None
    notifications: Optional["notify.NotifyCfg"] = None
    map: object = None                 # raw `map` config section (dict | None)


# -- Decision logic ----------------------------------------------------------

@dataclass
class DecideInput:
    mode: str
    policy: str
    master_wan_cfg: str
    eff_state: dict
    rtt_ms: dict
    loss_pct: dict
    master_up_since: Optional[float]
    currently_active: set
    now: float
    dynamic_master_current: Optional[str] = None
    dynamic_candidate: Optional[str] = None
    dynamic_candidate_since: Optional[float] = None


@dataclass
class DecideOutput:
    desired_active: set
    reason: str
    master_up_since: Optional[float]
    dynamic_master_current: Optional[str] = None
    dynamic_candidate: Optional[str] = None
    dynamic_candidate_since: Optional[float] = None
    # Policy-resolved master. Callers use this for the kernel default route and
    # the local_direct egress anchor instead of re-deriving from static config.
    effective_master: Optional[str] = None


def _best_link(eff_state: dict, rtt_ms: dict, loss_pct: dict, configured_master: str) -> str:
    candidates = list(eff_state.keys())
    def key(name):
        up = 0 if eff_state.get(name) == "UP" else 1
        rtt = rtt_ms.get(name, float("inf"))
        loss = loss_pct.get(name, float("inf"))
        not_master = 0 if name == configured_master else 1
        return (up, rtt, loss, not_master)
    candidates.sort(key=key)
    return candidates[0]


def _dynamic_pick(eff_state, rtt_ms, loss_pct, configured_master,
                  current_master, candidate, candidate_since,
                  rtt_margin_ms, loss_margin_pct, dwell_s, now):
    """
    Sticky master with asymmetric margin+dwell hysteresis.

    Two regimes:
    - On configured_master: a challenger must beat us by margin (rtt or loss)
      for >= dwell_s before we swap away. Standard symmetric ranker.
    - Off configured_master: configured_master is the implicit challenger
      whenever it is UP, unless current_master clearly beats it. After
      dwell_s of "configured is good enough", swap back home.

    Within both regimes, transient single-tick margin failures preserve any
    held candidate that is the configured master — this avoids dwell restart
    on RTT jitter near the margin boundary.
    """
    ups = [w for w in eff_state if eff_state.get(w) == "UP"]
    if not ups:
        return configured_master, None, None
    if len(ups) == 1:
        return ups[0], None, None

    if current_master is None or current_master not in ups:
        best = min(ups, key=lambda w: (
            loss_pct.get(w, 0.0),
            rtt_ms.get(w, float("inf")),
            0 if w == configured_master else 1,
        ))
        return best, None, None

    cur_rtt = rtt_ms.get(current_master, float("inf"))
    cur_loss = loss_pct.get(current_master, 0.0)
    challenger = None

    if current_master == configured_master:
        # On configured: standard "challenger must beat by margin".
        for w in ups:
            if w == current_master:
                continue
            w_rtt = rtt_ms.get(w, float("inf"))
            w_loss = loss_pct.get(w, 0.0)
            loss_better = w_loss + loss_margin_pct <= cur_loss
            rtt_better = (w_rtt + rtt_margin_ms <= cur_rtt
                          and w_loss <= cur_loss + loss_margin_pct)
            if loss_better or rtt_better:
                challenger = w
                break
    else:
        # Off configured: configured_master is implicit challenger unless
        # current_master clearly beats it (current beats configured by margin
        # on rtt, or has materially better loss).
        if configured_master in ups:
            cm_rtt = rtt_ms.get(configured_master, float("inf"))
            cm_loss = loss_pct.get(configured_master, 0.0)
            current_clearly_better = (
                (cur_rtt + rtt_margin_ms <= cm_rtt
                 and cur_loss <= cm_loss + loss_margin_pct)
                or (cur_loss + loss_margin_pct <= cm_loss)
            )
            if not current_clearly_better:
                challenger = configured_master

    if challenger is None:
        # Preserve held candidate if it's configured_master (jitter tolerance).
        if candidate is not None and candidate == configured_master:
            return current_master, candidate, candidate_since
        return current_master, None, None

    if candidate == challenger and candidate_since is not None:
        if now - candidate_since >= dwell_s:
            return challenger, None, None
        return current_master, candidate, candidate_since

    return current_master, challenger, now


def decide(cfg: Config, i: DecideInput) -> DecideOutput:
    if i.mode == "full":
        return DecideOutput(
            desired_active=set(cfg.wans.keys()),
            reason="full redundancy mode: both WANs active",
            master_up_since=None,
            effective_master=i.master_wan_cfg,
        )

    new_dyn_master = None
    new_dyn_cand = None
    new_dyn_cand_since = None
    if i.policy == "static_primary":
        master = cfg.policy.default_master_wan
    elif i.policy == "static_configured":
        master = i.master_wan_cfg
    elif i.policy == "dynamic":
        master, new_dyn_cand, new_dyn_cand_since = _dynamic_pick(
            i.eff_state, i.rtt_ms, i.loss_pct, cfg.policy.default_master_wan,
            i.dynamic_master_current, i.dynamic_candidate, i.dynamic_candidate_since,
            cfg.policy.dynamic_rtt_margin_ms, cfg.policy.dynamic_loss_margin_pct,
            cfg.policy.dynamic_swap_dwell_s, i.now,
        )
        new_dyn_master = master
    else:
        raise ValueError(f"unknown policy: {i.policy}")

    others = [w for w in cfg.wans.keys() if w != master]
    if not others:
        return DecideOutput(desired_active={master}, reason="single WAN configured",
                            master_up_since=i.master_up_since,
                            dynamic_master_current=new_dyn_master,
                            dynamic_candidate=new_dyn_cand,
                            dynamic_candidate_since=new_dyn_cand_since,
                            effective_master=master)
    backup = others[0]

    master_up = i.eff_state.get(master) == "UP"
    backup_up = i.eff_state.get(backup) == "UP"

    if not master_up:
        new_master_up_since = None
    elif i.master_up_since is None:
        new_master_up_since = i.now
    else:
        new_master_up_since = i.master_up_since

    if not master_up and not backup_up:
        return DecideOutput(
            desired_active=set(cfg.wans.keys()),
            reason="both WANs DOWN: safety fallback to full redundancy",
            master_up_since=new_master_up_since,
            dynamic_master_current=new_dyn_master,
            dynamic_candidate=new_dyn_cand,
            dynamic_candidate_since=new_dyn_cand_since,
            effective_master=master,
        )

    if not master_up and backup_up:
        return DecideOutput(
            desired_active={backup},
            reason=f"master ({master}) DOWN, fail-over to {backup}",
            master_up_since=new_master_up_since,
            dynamic_master_current=new_dyn_master,
            dynamic_candidate=new_dyn_cand,
            dynamic_candidate_since=new_dyn_cand_since,
            effective_master=master,
        )

    if master_up and i.currently_active == {backup} and i.policy != "dynamic":
        elapsed = i.now - new_master_up_since if new_master_up_since else 0.0
        if elapsed >= cfg.policy.failback_hold_s:
            return DecideOutput(
                desired_active={master},
                reason=f"master ({master}) UP for {elapsed:.0f}s, fail-back",
                master_up_since=new_master_up_since,
                dynamic_master_current=new_dyn_master,
                dynamic_candidate=new_dyn_cand,
                dynamic_candidate_since=new_dyn_cand_since,
                effective_master=master,
            )
        else:
            remaining = cfg.policy.failback_hold_s - elapsed
            return DecideOutput(
                desired_active={backup},
                reason=f"master ({master}) UP but in hysteresis hold ({remaining:.0f}s remaining)",
                master_up_since=new_master_up_since,
                dynamic_master_current=new_dyn_master,
                dynamic_candidate=new_dyn_cand,
                dynamic_candidate_since=new_dyn_cand_since,
                effective_master=master,
            )

    return DecideOutput(
        desired_active={master},
        reason=f"master ({master}) UP, steady state",
        master_up_since=new_master_up_since,
        dynamic_master_current=new_dyn_master,
        dynamic_candidate=new_dyn_cand,
        dynamic_candidate_since=new_dyn_cand_since,
        effective_master=master,
    )


# -- State sensing -----------------------------------------------------------

import os
import re
import time
import json as _json
import http.client
import urllib.parse
import urllib.request
import urllib.error


@dataclass
class WanSample:
    state: str
    rtt_ms: Optional[float]
    loss_pct: Optional[float]
    state_since: Optional[float] = None


@dataclass
class StateSnapshot:
    ok: bool
    per_wan: dict
    stale: bool = False
    stale_s: float = 0.0
    error: str = ""
    fetched_at: float = 0.0


def read_local_sbfd_state(path: str,
                          session_id_to_wan: dict,
                          stale_threshold_s: float = 10.0) -> StateSnapshot:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return StateSnapshot(ok=False, per_wan={}, error=f"missing: {path}")
    except OSError as e:
        return StateSnapshot(ok=False, per_wan={}, error=f"stat error: {e}")

    age = time.time() - st.st_mtime
    try:
        with open(path) as f:
            raw = _json.load(f)
    except (OSError, ValueError) as e:
        return StateSnapshot(ok=False, per_wan={}, error=f"parse error: {e}",
                             stale=age >= stale_threshold_s, stale_s=age)

    sessions = raw.get("sessions", {})
    by_sid = {}
    for sess_data in sessions.values():
        sid = sess_data.get("session_id")
        if sid is not None:
            by_sid[int(sid)] = sess_data

    per_wan = {}
    for sid, wan_name in session_id_to_wan.items():
        sd = by_sid.get(int(sid))
        if sd is None:
            per_wan[wan_name] = WanSample(state="UNKNOWN", rtt_ms=None, loss_pct=None)
        else:
            per_wan[wan_name] = WanSample(
                state=sd.get("state", "UNKNOWN"),
                rtt_ms=sd.get("rtt_ms"),
                loss_pct=sd.get("loss_pct"),
                state_since=sd.get("state_since"),
            )

    return StateSnapshot(
        ok=True,
        per_wan=per_wan,
        stale=age >= stale_threshold_s,
        stale_s=age,
        fetched_at=st.st_mtime,
    )


# -- Relay control-plane transport -------------------------------------------

# The controller loop GETs the relay's /state and /fec once per second across
# the management overlay, which in a real deployment rides the metered WAN
# links. urllib opens a fresh TCP connection per call, and for these sub-1KB
# payloads the handshake and teardown outweigh the data itself -- besides adding
# an RTT of staleness to a failover input. So connections are pooled per
# (scheme, host, port) and reused.
#
# A connection is checked OUT of the pool for the duration of a request and
# returned on success, so two callers can never interleave writes on one
# http.client connection and no lock is held across network I/O.
_relay_conns: dict = {}
_relay_conns_lock = threading.Lock()


def close_relay_conns():
    """Drop every pooled relay connection. Safe to call at any time."""
    with _relay_conns_lock:
        conns = list(_relay_conns.values())
        _relay_conns.clear()
    for conn in conns:
        try:
            conn.close()
        except OSError:
            pass


def _relay_request(url, method="GET", body=None, headers=None, timeout_s=2.0):
    """Request `url` over a pooled keep-alive connection.

    Returns (status, reason, body_bytes), and raises OSError / http.client
    exceptions on transport failure so callers keep their fail-open handling.

    A pooled socket the peer has already closed -- reaped by an idle timeout, or
    dropped when the relay daemon restarted -- still looks usable from this side
    and only fails on first write or read. That case is retried once on a fresh
    connection. A connection that was ALREADY fresh is never retried: the relay
    is simply unreachable, and a second attempt would silently double the
    caller's timeout budget inside a 0.5s control loop.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme {parts.scheme!r} in {url!r}")
    if not parts.hostname:
        raise ValueError(f"no host in URL {url!r}")
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    key = (parts.scheme, parts.hostname, parts.port)

    retried = False
    while True:
        with _relay_conns_lock:
            conn = _relay_conns.pop(key, None)
        was_pooled = conn is not None
        if not was_pooled:
            cls = (http.client.HTTPSConnection if parts.scheme == "https"
                   else http.client.HTTPConnection)
            conn = cls(parts.hostname, parts.port, timeout=timeout_s)

        try:
            if was_pooled:
                # A pooled connection carries the timeout it was built with; the
                # live socket needs the current one applied directly. Inside the
                # try because a socket the peer already closed raises EBADF
                # here, which is exactly the stale case the retry exists for.
                conn.timeout = timeout_s
                if conn.sock is not None:
                    conn.sock.settimeout(timeout_s)
            conn.request(method, target, body=body, headers=dict(headers or {}))
            resp = conn.getresponse()
            data = resp.read()
            will_close = resp.will_close
        except (http.client.HTTPException, OSError):
            try:
                conn.close()
            except OSError:
                pass
            if not was_pooled or retried:
                raise
            retried = True
            continue

        if will_close:
            # Peer declined keep-alive (an older HTTP/1.0 relay mid-upgrade, or
            # a proxy): don't pool a connection it has already dropped.
            try:
                conn.close()
            except OSError:
                pass
        else:
            with _relay_conns_lock:
                if key in _relay_conns:
                    conn.close()  # another caller pooled one while in flight
                else:
                    _relay_conns[key] = conn
        return resp.status, resp.reason, data


def fetch_remote_sbfd_state(url: str,
                            timeout_s: float,
                            session_id_to_wan: dict) -> StateSnapshot:
    """HTTP GET the relay /state endpoint. Fail-open on any error."""
    try:
        status, reason, body = _relay_request(url, timeout_s=timeout_s)
    except (http.client.HTTPException, OSError, ValueError) as e:
        return StateSnapshot(ok=False, per_wan={}, error=f"transport: {e}")
    if status != 200:
        return StateSnapshot(ok=False, per_wan={}, error=f"HTTP {status}: {reason}")

    try:
        raw = _json.loads(body.decode())
    except (ValueError, UnicodeDecodeError) as e:
        return StateSnapshot(ok=False, per_wan={}, error=f"parse: {e}")

    by_sid = {}
    for sd in raw.get("sessions", {}).values():
        sid = sd.get("session_id")
        if sid is not None:
            by_sid[int(sid)] = sd

    per_wan = {}
    for sid, wan_name in session_id_to_wan.items():
        sd = by_sid.get(int(sid))
        if sd is None:
            per_wan[wan_name] = WanSample(state="UNKNOWN", rtt_ms=None, loss_pct=None)
        else:
            per_wan[wan_name] = WanSample(
                state=sd.get("state", "UNKNOWN"),
                rtt_ms=sd.get("rtt_ms"),
                loss_pct=sd.get("loss_pct"),
                state_since=sd.get("state_since"),
            )

    return StateSnapshot(ok=True, per_wan=per_wan, fetched_at=time.time())


def merge_effective(local: StateSnapshot, remote: StateSnapshot) -> dict:
    """DOWN dominates: a WAN is DOWN if a contributing side reports DOWN.

    Freshness gating (CR #13): a fresh source always overrides a stale one, so a
    stale DOWN can never suppress a WAN that a fresh source reports UP. A stale
    source still contributes when the other side has nothing fresher — losing
    last-known state entirely would route total blindness into the "both DOWN"
    safety fallback. So a stale snapshot is used only if the other side is not
    fresh; if both are stale, last-known data (DOWN-dominant) is kept."""
    eff = {}
    local_fresh = local.ok and not local.stale
    remote_fresh = remote.ok and not remote.stale
    use_local = local.ok and (local_fresh or not remote_fresh)
    use_remote = remote.ok and (remote_fresh or not local_fresh)
    wans = set((local.per_wan if local.ok else {}).keys()) | \
           set((remote.per_wan if remote.ok else {}).keys())
    for w in wans:
        l = local.per_wan.get(w) if use_local else None
        r = remote.per_wan.get(w) if use_remote else None
        states = []
        if l: states.append(l.state)
        if r: states.append(r.state)
        if "DOWN" in states:
            eff[w] = "DOWN"
        elif "UP" in states:
            eff[w] = "UP"
        else:
            eff[w] = "UNKNOWN"
    return eff


import subprocess


# -- nftables actuator -------------------------------------------------------

def compute_nft_init(cfg: Config) -> list:
    """Return nft script lines that idempotently create the table+chain."""
    table = f"{cfg.nft.family} {cfg.nft.table}"
    return [
        f"create table {table}",
        f"create chain {table} egress_filter {{ type filter hook output priority 0; }}",
    ]


def compute_nft_diff(cfg: Config, desired_active: set, current_drops: set) -> list:
    """Return list of nft script lines to converge to (all_wans - desired_active)."""
    all_wans = set(cfg.wans.keys())
    desired_drops = all_wans - desired_active

    to_add = desired_drops - current_drops
    to_remove = current_drops - desired_drops

    actions = []
    table = f"{cfg.nft.family} {cfg.nft.table}"

    for wan in sorted(to_add):
        actions.append(
            f"add rule {table} egress_filter oifname {wan} "
            f"ip daddr {cfg.engarde.server_ip} udp dport {cfg.engarde.server_port} drop"
        )

    for wan in sorted(to_remove):
        actions.append(
            f"delete rule {table} egress_filter oifname {wan} "
            f"ip daddr {cfg.engarde.server_ip} udp dport {cfg.engarde.server_port} drop"
        )

    return actions


def list_current_drops(cfg: Config) -> set:
    try:
        out = subprocess.run(
            ["nft", "-a", "list", "chain", cfg.nft.family, cfg.nft.table, "egress_filter"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        logging.error("nft binary not found in PATH")
        return set()
    if out.returncode != 0:
        logging.debug("nft list chain failed: %s", out.stderr.strip())
        return set()

    drops = set()
    for line in out.stdout.splitlines():
        s = line.strip()
        if " drop" not in s or "oifname" not in s:
            continue
        toks = s.replace('"', " ").split()
        try:
            idx = toks.index("oifname")
            drops.add(toks[idx + 1])
        except (ValueError, IndexError):
            continue
    return drops


def apply_nft_init(cfg: Config) -> None:
    subprocess.run(
        ["nft", "delete", "table", cfg.nft.family, cfg.nft.table],
        capture_output=True, text=True,
    )
    script = "\n".join(compute_nft_init(cfg)) + "\n"
    r = subprocess.run(["nft", "-f", "-"], input=script,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"nft init failed: {r.stderr.strip()}")


def _resolve_delete_lines(cfg: Config, actions: list) -> list:
    """nft's `delete rule` only accepts a handle, never a match spec — a
    spec-form delete fails the whole `nft -f` batch (per-switch warning noise).
    Rewrite delete lines to handle form up front; a delete whose rule is
    already absent is dropped (the desired end-state already holds)."""
    out = []
    for line in actions:
        if not line.startswith("delete rule"):
            out.append(line)
            continue
        wan = _wan_from_rule_line(line)
        handle = _find_drop_handle(cfg, wan) if wan else None
        if handle is None:
            logging.debug("nft delete: drop rule for %s already absent; converged", wan)
            continue
        out.append(f"delete rule {cfg.nft.family} {cfg.nft.table} "
                   f"egress_filter handle {handle}")
    return out


def apply_nft_diff(cfg: Config, actions: list) -> None:
    if not actions:
        return
    actions = _resolve_delete_lines(cfg, actions)
    if not actions:
        return
    script = "\n".join(actions) + "\n"
    r = subprocess.run(["nft", "-f", "-"], input=script,
                       capture_output=True, text=True)
    if r.returncode == 0:
        return

    err = r.stderr.lower()
    if "delete" not in err and "no such" not in err and "could not process" not in err:
        raise RuntimeError(f"nft apply failed: {r.stderr.strip()}")

    logging.warning("nft -f - failed (%s); falling back to per-line", r.stderr.strip())
    for line in actions:
        r2 = subprocess.run(["nft"] + line.split(),
                            capture_output=True, text=True)
        if r2.returncode == 0:
            continue
        if line.startswith("delete rule") and " handle " not in line:
            wan = _wan_from_rule_line(line)
            handle = _find_drop_handle(cfg, wan) if wan else None
            if handle is not None:
                fallback = ["nft", "delete", "rule", cfg.nft.family, cfg.nft.table,
                            "egress_filter", "handle", str(handle)]
                r3 = subprocess.run(fallback, capture_output=True, text=True)
                if r3.returncode == 0:
                    continue
            else:
                # Rule is already absent (nft flush, restart, or external
                # change). The desired end-state — drop rule gone — already
                # holds, so this delete is a no-op. Treating it as an error
                # would keep desired != current and re-fire every tick.
                logging.debug("nft delete: drop rule for %s already absent; converged", wan)
                continue
        raise RuntimeError(f"nft action {line!r} failed: {r2.stderr.strip()}")


# -- Kernel default-route actuator -------------------------------------------
# Beats DHCP-installed defaults (typically metric 100/200) by installing our
# own at metric 50, so system traffic (the management overlay, NTP, package mgrs) follows
# the WAN sbfd-ctl considers best — not whichever WAN happened to be installed
# first by the DHCP client.

MANAGED_DEFAULT_METRIC = 50


def pick_default_wan(eff_state: dict, desired_active: set,
                     configured_master: str,
                     dynamic_master_current: Optional[str]) -> Optional[str]:
    """Choose the WAN to own the kernel default route. None if all DOWN."""
    ups = [w for w in eff_state if eff_state.get(w) == "UP"]
    if not ups:
        return None

    # If engarde is on a single WAN (master_backup), default follows it —
    # but only if that WAN is actually UP.
    active_ups = [w for w in desired_active if eff_state.get(w) == "UP"]
    if len(active_ups) == 1:
        return active_ups[0]

    # Multiple WANs UP-and-active (full mode, or degenerate): prefer the
    # dynamic picker's choice, then configured master, then first UP.
    if dynamic_master_current and dynamic_master_current in ups:
        return dynamic_master_current
    if configured_master in ups:
        return configured_master
    return sorted(ups)[0]


def worst_active_loss(loss, active_wans):
    """Worst loss among the WANs actually carrying traffic (all WANs when
    the active set is empty — mirrors fec_driver_wan's fallback)."""
    active = active_wans or set(loss.keys())
    return max((loss.get(w, 0.0) for w in active), default=0.0)


def fec_loss_map(local, remote, remote_fresh, wans):
    """Per-WAN loss driving the client->relay FEC leg, plus its source.

    sbfd loss_pct is RX-side, so the loss this leg's parity repairs
    (client->relay direction) is measured at the RELAY. Prefer the fetched
    relay snapshot; the locally measured loss (relay->client direction) is
    only a correlation proxy, used when the relay view is missing or stale."""
    use_remote = remote.ok and remote_fresh
    src, source = (remote, "relay") if use_remote else (local, "local")
    loss = {}
    for w in wans:
        s = src.per_wan.get(w) if src.ok else None
        loss[w] = s.loss_pct if (s and s.loss_pct is not None) else 0.0
    return loss, source


def compute_fec_target(fec_cfg, mode, eff, loss, active_wans, loss_table=None):
    """Pure: map mode/effective-state/loss to a FEC table level (the
    pre-fec-mode-override adaptive choice). Loss = worst among the WANs
    actually carrying traffic. loss_table overrides fec_cfg's (per-WAN
    profiles); None keeps the global table."""
    table = loss_table if loss_table is not None else fec_cfg.loss_table
    up_count = sum(1 for w, st in eff.items() if st == "UP")
    active_loss = worst_active_loss(loss, active_wans)
    return fec_control.mode_aware_level(
        mode, up_count, active_loss, table,
        fec_cfg.full_min_up_wans, fec_cfg.full_mode_backoff_fec)


def fec_driver_wan(loss, active_wans):
    """The active WAN whose loss feeds compute_fec_target (the max-loss one).
    Uses the same active fallback as compute_fec_target. None if no candidates.
    active_wans is a set, so its iteration order is hash-dependent; sort it
    first so a tie (e.g. both WANs at 0.0 loss) resolves deterministically
    instead of depending on set hash order — the driver now selects FEC
    policy, so nondeterminism here would make profile selection flaky."""
    active = active_wans or set(loss.keys())
    if not active:
        return None
    return max(sorted(active), key=lambda w: loss.get(w, 0.0))


def fec_driver_pick(loss, active_wans, current, candidate, candidate_since,
                    dwell_s, now, prev_active=None):
    """Sticky version of fec_driver_wan: (driver, candidate, candidate_since).

    The driver picks the WHOLE FEC policy — table, floor, hysteresis — so
    changing it swaps the ladder beneath the adaptive engine and reseeds its
    runtime. step_level damps the ratio against loss jitter, but nothing damped
    this, so on a duplicated stream where both links are near zero loss the
    driver flipped on noise: measured 82 profile switches in 24h, median 95s
    apart, every one of them in full redundancy where the ratio should not have
    moved at all.

    The challenger is always fec_driver_wan's pick — the worst active link,
    ties broken deterministically — and it must hold that position for dwell_s
    before taking over. Dwell alone is the whole mechanism: an excursion
    shorter than dwell_s is ignored, a sustained one is followed, and because a
    tie resolves to the same WAN every tick, the driver returns to the quiet
    state's pick once the excursion passes. That return matters beyond
    tidiness: the cell signal floor only engages while the cellular WAN is the
    driver, so a driver that never came home would silently disarm it.

    A loss MARGIN was tried here and removed: whenever no WAN cleared it, the
    same WAN challenged anyway as the canonical pick, so it never changed
    whether a flip was damped — only which WAN challenged when three or more
    were active, and there it picked the first by name rather than the worst.

    Only one WAN active — every mode but full redundancy — has no race to lose
    and short-circuits, so this cannot delay a master_backup failover.

    prev_active is last tick's set. Incumbency means "worst of THIS field for
    dwell_s", a claim a WAN that just joined never got to contest, so when the
    canonical pick is a newcomer it takes over at once instead of owing dwell.
    Entering full redundancy is that case: the second link joins, both are
    clean, and the tie hands the driver to a WAN the incumbent had no race
    against — waiting 120s there just runs the wrong profile on a live link.
    Only the newcomer skips the queue; a WAN that was already active keeps
    serving its dwell, and a departure leaves a challenge in flight untouched,
    so the anti-flap damping still covers every same-membership tick, which is
    where the measured thrash lived. Pass None (the default) when last tick's
    set is unknown and every WAN is treated as an incumbent.
    """
    active = active_wans or set(loss.keys())
    if not active:
        return None, None, None
    if len(active) == 1:
        return next(iter(active)), None, None
    if current is None or current not in active:
        # Nothing to be sticky about yet (boot, or the incumbent stopped
        # carrying traffic): take the raw ranking and start clean.
        return fec_driver_wan(loss, active), None, None

    challenger = fec_driver_wan(loss, active)
    if prev_active and challenger is not None and challenger not in prev_active:
        return challenger, None, None
    if challenger is None or challenger == current:
        return current, None, None
    if candidate == challenger and candidate_since is not None:
        # Same challenger as last tick: let its clock run rather than
        # restarting it, or a challenge could never accumulate dwell.
        if now - candidate_since >= dwell_s:
            return challenger, None, None
        return current, candidate, candidate_since
    return current, challenger, now


def resolve_fec_profile(fec_cfg, driver_wan):
    """(name, loss_table, hysteresis, profile_floor, signal_floor_fec) for the
    current fec driver WAN. profile_floor is None for the default profile so
    effective_fec_floor_ratio falls through to the config default."""
    p = fec_cfg.wan_profiles.get(driver_wan) if driver_wan else None
    if p is None:
        return ("default", fec_cfg.loss_table,
                fec_control.FecHysteresis(fec_cfg.ramp_up_ticks,
                                          fec_cfg.ramp_down_hold_s),
                None, fec_control.DEFAULT_SIGNAL_FLOOR_FEC)
    return (p.name, p.loss_table,
            fec_control.FecHysteresis(p.ramp_up_ticks, p.ramp_down_hold_s),
            p.floor_ratio, p.signal_floor_fec)


def fec_reseed_runtime(rt, old_table, new_table, now):
    """Client-side name for fec_control.reseed_runtime, which both legs share.
    The rationale (and the live incident behind it) lives with the helper."""
    return fec_control.reseed_runtime(rt, old_table, new_table, now)


def fec_profile_candidates(cfg: Config, ov: RuntimeOverlay):
    """[(loss_table, floor_ratio)] for every profile that could drive FEC.

    The driver WAN is chosen per tick, so any of these can be active moments
    from now; the UI's scale has to span all of them or its positions would
    shift under the operator on a driver change. Each floor goes through the
    same precedence the live path uses, so a runtime override is reflected in
    the scale rather than sitting off the end of it."""
    if not cfg.fec:
        return []
    out = [(cfg.fec.loss_table, effective_fec_floor_ratio(cfg, ov))]
    for p in cfg.fec.wan_profiles.values():
        out.append((p.loss_table,
                    effective_fec_floor_ratio(cfg, ov,
                                              profile_floor=p.floor_ratio)))
    return out


def should_post_fec(desired, last_acked, last_post_ts, now, heartbeat_s=30.0):
    """Reconcile decision: POST when desired differs from last-acked (assert until
    acked), on the first tick, or once the heartbeat elapses (defends against an
    relay restart that reverted to its default)."""
    if desired != last_acked:
        return True
    if last_post_ts is None:
        return True
    return (now - last_post_ts) >= heartbeat_s


def _relay_ladder_table(data, ladder_inputs):
    """The loss table to rebuild the relay's row against, or None to keep the
    relay's own ladder.

    Every setting the rebuild needs must be present IN THE RELAY'S REPORT.
    Substituting our desired values for missing ones would publish a floor and
    a reachable span the relay never claimed — which is precisely what this
    card must never do, since the two disagree exactly when it matters (an
    unacknowledged push, or a restart that reverted the relay to its config).
    A relay too old to report them keeps its own ladder instead.

    `profile` must also be a string: /fec is remote JSON, and an unhashable
    value would raise on the dict lookup and take the controller loop with it.
    """
    mode, floor = data.get("mode"), data.get("floor_ratio")
    profile = data.get("profile")
    if not isinstance(mode, str) or not isinstance(floor, str) or not floor:
        return None
    if mode == fec_control.MODE_FIXED and not isinstance(
            data.get("fixed_ratio"), str):
        return None
    if not isinstance(profile, str):
        return None
    if profile == "default":
        return ladder_inputs["default_table"]
    # A profile we no longer have (removed or renamed in config; the relay
    # keeps reporting the pushed name until it goes stale) cannot be modelled —
    # defaulting its table would claim rungs its real one caps below.
    return ladder_inputs["tables"].get(profile)


def relay_fec_direction(fetch, fetched_at, now, desired, last_acked, local_rx=None,
                        ladder_inputs=None):
    """Shape the relay->client published direction dict from a fetch_relay_fec result.

    `desired` / `last_acked` are the 7-tuple
    (mode, fixed_ratio, floor_ratio, loss_level, profile_name, signal_floor,
    location_level).
    We just compare them for equality to drive reconcile_pending — the relay
    echoes back what it actually applied."""
    fetch = fetch or {}
    data = fetch.get("data") or {}
    # The relay publishes a ladder of its own, but it is scaled to the relay's
    # view of one profile. Re-derive the row against OUR cross-profile scale so
    # both cards share positions; the relay's own stays as the fallback for a
    # client that could not build one (cfg.fec absent) and for anyone reading
    # /fec directly.
    #
    # Every INPUT comes from what the relay reports, not from what we desire.
    # While a push is unacknowledged the two disagree, and a relay restart
    # reverts it to its config floor for up to a heartbeat — deriving the span
    # from our desired settings would have this card claim reachability, or a
    # floor, that the relay is not honoring. Our values are the fallback only
    # for a relay too old to report them.
    ladder = data.get("ladder")
    table = _relay_ladder_table(data, ladder_inputs) if (
        ladder_inputs is not None and fetch.get("ok")) else None
    if table is not None:
        mode, floor = data["mode"], data["floor_ratio"]
        ladder = fec_control.ladder_view(
            ladder_inputs["scale"],
            # No pinned_level: the relay's run_once calls loss_to_level
            # directly and never consults mode_aware_level, so full-redundancy
            # backoff does not apply to this leg however our own is behaving.
            fec_control.reachable_ratios(
                mode, table, floor, fixed_ratio=data.get("fixed_ratio")),
            data.get("ratio"), floor, mode, pinned=False)
    return {
        "enabled": data.get("enabled"),
        "mode": data.get("mode"),
        "fixed_ratio": data.get("fixed_ratio"),
        # The relay's own floor, so the UI can show a mid-upgrade mismatch
        # rather than hiding it behind our locally-computed value.
        "floor_ratio": data.get("floor_ratio"),
        "ratio": data.get("ratio"),
        "level": data.get("level"),
        # The level the relay is applying, clamped to its own table — not the
        # one we asked for. None from a relay too old to report it. The two
        # legs legitimately differ mid-push, and differ for good after a clamp,
        # so this is the relay's own value, never ours echoed back.
        "location_level": data.get("location_level"),
        # Absent from a relay older than the ladder field; the UI falls back to
        # a fixed-width pip row rather than showing an empty one.
        "ladder": ladder,
        "driving_loss_pct": data.get("driving_loss_pct"),
        "loss_source": data.get("loss_source"),
        "since": data.get("since"),
        "ok": bool(fetch.get("ok")),
        "stale_s": (now - fetched_at) if fetched_at else None,
        "error": fetch.get("error"),
        "reconcile_pending": (last_acked != desired),
        "wire": data.get("wire"),
        # Decode outcomes for relay->client are measured by OUR decoder (rx is
        # RX-side), so they come from the local tracker, not the relay fetch.
        "rx": local_rx,
    }


def parse_default_gateway(json_text: str) -> Optional[str]:
    """First gateway from `ip -j route show default ...` output."""
    try:
        routes = _json.loads(json_text)
    except ValueError:
        return None
    for route in routes:
        gw = route.get("gateway")
        if gw:
            return gw
    return None


def parse_managed_default(json_text: str,
                          metric: int = MANAGED_DEFAULT_METRIC) -> Optional[tuple]:
    """Find our managed default (iface, gateway) at the given metric.

    `ip -j` omits the "metric" key when metric is 0, so callers asking for
    metric=0 must accept entries with no key.
    """
    try:
        routes = _json.loads(json_text)
    except ValueError:
        return None
    for route in routes:
        m = route.get("metric", 0)
        if m != metric:
            continue
        iface = route.get("dev")
        gw = route.get("gateway")
        if iface and gw:
            return (iface, gw)
    return None


def compute_route_action(desired_iface: Optional[str],
                         desired_gateway: Optional[str],
                         current: Optional[tuple]) -> Optional[tuple]:
    """Reconcile current managed default toward desired.

    Returns None | ('replace', iface, gw).
    Safety: if desired_iface is set but desired_gateway is None (couldn't
    read it), returns None — refuse to act on incomplete info.

    No-WAN-pickable (desired_iface None, e.g. all WANs DOWN/UNKNOWN ->
    egress_master None) PRESERVES the last-good managed default route rather
    than deleting it: deleting our sole default during a transient both-down
    blip caused episodic total outages. Clean-shutdown removal is handled
    separately by withdraw_managed_default().
    """
    if desired_iface is None:
        return None
    if desired_gateway is None:
        return None
    desired = (desired_iface, desired_gateway)
    if current == desired:
        return None
    return ("replace", desired_iface, desired_gateway)


def compute_engarde_table_action(egress_mode: str,
                                 master_iface: Optional[str],
                                 master_gw: Optional[str],
                                 current: Optional[dict],
                                 cfg: EgressCfg) -> Optional[dict]:
    """Reconcile the engarde-PBR-table default route toward the desired mode.

    Returns None | {"op": "replace", "via": str|None, "dev": str, "table": str}.
    Returns None when current state already matches desired (idempotent).

    For relay_vpn/relay_direct: desired = `default dev <wg_iface>` (today's state).
    For local_direct: desired = `default via <master_gw> dev <master_iface>`.
    Refuses to act on local_direct when master_gw is None (would black-hole).
    """
    if egress_mode in ("relay_vpn", "relay_direct"):
        desired = {"via": None, "dev": cfg.wg_iface}
    elif egress_mode == "local_direct":
        if master_gw is None or master_iface is None:
            return None
        desired = {"via": master_gw, "dev": master_iface}
    else:
        return None

    cur = current or {}
    if cur.get("via") == desired["via"] and cur.get("dev") == desired["dev"]:
        return None
    return {"op": "replace", "via": desired["via"], "dev": desired["dev"],
            "table": cfg.engarde_table}


def read_wan_gateway(iface: str) -> Optional[str]:
    r = subprocess.run(["ip", "-j", "route", "show", "default", "dev", iface],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return parse_default_gateway(r.stdout)


def read_managed_default(metric: int = MANAGED_DEFAULT_METRIC) -> Optional[tuple]:
    r = subprocess.run(["ip", "-j", "route", "show", "default"],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return None
    return parse_managed_default(r.stdout, metric=metric)


def apply_route_action(action: Optional[tuple],
                       metric: int = MANAGED_DEFAULT_METRIC) -> None:
    if action is None:
        return
    op, iface, gw = action
    if op == "replace":
        cmd = ["ip", "route", "replace", "default", "via", gw,
               "dev", iface, "metric", str(metric)]
    elif op == "delete":
        cmd = ["ip", "route", "del", "default", "via", gw,
               "dev", iface, "metric", str(metric)]
    else:
        return
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        # Delete-of-missing is fine; surface anything else.
        if op == "delete" and "no such" in r.stderr.lower():
            return
        raise RuntimeError(f"ip {op} failed: {r.stderr.strip()}")


def apply_engarde_table_action(action: Optional[dict]) -> None:
    """Apply a route action to a non-main PBR table. Idempotent via `ip route replace`.

    action shape: {"op": "replace", "via": str|None, "dev": str, "table": str}.
    Only "replace" is supported; engarde PBR table is never torn down at runtime.
    `replace` is unconditionally idempotent, so no special-case stderr handling needed.
    """
    if action is None:
        return
    op = action.get("op")
    if op != "replace":
        return
    cmd = ["ip", "route", "replace", "default"]
    via = action.get("via")
    if via is not None:
        cmd += ["via", via]
    cmd += ["dev", action["dev"], "table", action["table"]]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"ip route replace failed: {r.stderr.strip()}")


def read_engarde_table_default(table: str) -> Optional[dict]:
    """Read the default route in the named PBR table. Returns {"via":...,"dev":...} or None."""
    r = subprocess.run(
        ["ip", "-j", "route", "show", "default", "table", table],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        rows = _json.loads(r.stdout)
    except ValueError:
        return None
    if not rows:
        return None
    # Take first row; engarde table holds at most one default by construction.
    row = rows[0]
    return {"via": row.get("gateway"), "dev": row.get("dev")}


def _wan_from_rule_line(line: str) -> Optional[str]:
    toks = line.split()
    try:
        idx = toks.index("oifname")
        return toks[idx + 1].strip('"')
    except (ValueError, IndexError):
        return None


def _find_drop_handle(cfg: Config, wan: str) -> Optional[int]:
    out = subprocess.run(
        ["nft", "-a", "list", "chain", cfg.nft.family, cfg.nft.table, "egress_filter"],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        s = line.strip()
        if " drop" in s and "# handle " in s and "oifname" in s:
            toks = s.replace('"', " ").split()
            try:
                if toks[toks.index("oifname") + 1] == wan:
                    return int(s.rsplit("# handle ", 1)[1].strip())
            except (ValueError, IndexError):
                continue
    return None


# -- engarde exclusion sync ----------------------------------------------------
# The nft egress_filter silently eats engarde's packets on blocked WANs, so
# engarde-client retries the send every second and logs a write error each
# time. Its web API supports runtime interface exclusion; keeping engarde's
# exclusion set converged to the blocked set silences that loop at the source.
# Runtime exclusions are lost when engarde-client restarts, hence reconcile
# every tick. nft remains the enforcement backstop.

def parse_engarde_exclusions(raw: str) -> Optional[set]:
    """Interface names engarde currently excludes, from get-list JSON.
    None when the payload is junk (callers skip the sync this tick)."""
    try:
        d = json.loads(raw)
        return {i["name"] for i in d["interfaces"] if i.get("status") == "excluded"}
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def compute_exclusion_diff(wan_ifaces: set, desired_active_ifaces: set,
                           currently_excluded: set) -> tuple:
    """Pure: (to_exclude, to_include), limited to managed WAN ifaces — the
    config's own excludedInterfaces (lo, LAN, tunnels) are never touched."""
    desired_excluded = wan_ifaces - desired_active_ifaces
    to_exclude = desired_excluded - currently_excluded
    to_include = (currently_excluded & wan_ifaces) - desired_excluded
    return sorted(to_exclude), sorted(to_include)


def sync_engarde_exclusions(cfg: Config, desired_active: set) -> None:
    """Best-effort convergence of engarde's runtime exclusions; never raises."""
    url = cfg.engarde.admin_url
    if not url or "/get-list" not in url:
        return
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            current = parse_engarde_exclusions(resp.read().decode())
    except Exception as e:  # noqa: BLE001 - engarde may be restarting
        logging.debug("engarde get-list failed: %s", e)
        return
    if current is None:
        return
    wan_ifaces = {w.iface for w in cfg.wans.values()}
    active_ifaces = {cfg.wans[k].iface for k in desired_active if k in cfg.wans}
    to_exclude, to_include = compute_exclusion_diff(wan_ifaces, active_ifaces, current)
    base = url.rsplit("/get-list", 1)[0]
    for action, ifaces in (("exclude", to_exclude), ("include", to_include)):
        for iface in ifaces:
            try:
                req = urllib.request.Request(
                    f"{base}/{action}", method="POST",
                    data=json.dumps({"interface": iface}).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=2) as resp:
                    resp.read()
            except Exception as e:  # noqa: BLE001 - best effort; nft backstops
                logging.debug("engarde %s %s failed: %s", action, iface, e)
    if to_exclude or to_include:
        logging.info("engarde exclusions: +%s -%s", to_exclude, to_include)


def load_config(path: str) -> Config:
    with open(path) as f:
        raw = json.load(f)

    try:
        wans = {
            name: WanCfg(iface=w["iface"], session_id=int(w["session_id"]),
                         label=w.get("label", name))
            for name, w in raw["wans"].items()
        }

        policy = PolicyCfg(
            default_mode=raw["policy"].get("default_mode", "full"),
            default_master_policy=raw["policy"].get("default_master_policy", "static_primary"),
            default_master_wan=raw["policy"].get("default_master_wan", "wan2"),
            failback_hold_s=int(raw["policy"].get("failback_hold_s", 30)),
            dynamic_rtt_margin_ms=float(raw["policy"].get("dynamic_rtt_margin_ms", 25.0)),
            dynamic_loss_margin_pct=float(raw["policy"].get("dynamic_loss_margin_pct", 1.0)),
            dynamic_swap_dwell_s=float(raw["policy"].get("dynamic_swap_dwell_s", 10.0)),
            manage_default_route=bool(raw["policy"].get("manage_default_route", False)),
        )

        relay = RelayCfg(
            state_url=raw["relay"]["state_url"],
            fetch_interval_s=float(raw["relay"].get("fetch_interval_s", 1.0)),
            fetch_timeout_s=float(raw["relay"].get("fetch_timeout_s", 2.0)),
            fec_url=raw["relay"].get("fec_url"),
        )
        engarde = EngardeCfg(
            server_ip=raw["engarde"]["server_ip"],
            server_port=int(raw["engarde"]["server_port"]),
            admin_url=raw["engarde"].get("admin_url"),
        )
        nft = NftCfg(
            table=raw.get("nft", {}).get("table", "sbfd_ctl"),
            family=raw.get("nft", {}).get("family", "inet"),
        )

        eraw = raw.get("egress", {}) or {}
        egress = EgressCfg(
            engarde_table=str(eraw.get("engarde_table", "engarde")),
            wg_iface=str(eraw.get("wg_iface", "wg0")),
            default_mode=str(eraw.get("default_mode", "relay_vpn")),
        )

        raw_fec = raw.get("fec")
        fec_cfg = None
        if raw_fec is not None:
            # Legacy fallback: if `mode` is absent and the deprecated `enabled`
            # boolean is present, map true→adaptive (preserve their explicit
            # choice), false→off. Otherwise default = min_adaptive.
            if "mode" in raw_fec:
                cfg_mode = fec_control.normalize_mode(raw_fec.get("mode"))
            elif "enabled" in raw_fec:
                cfg_mode = (fec_control.MODE_ADAPTIVE if bool(raw_fec["enabled"])
                            else fec_control.MODE_OFF)
            else:
                cfg_mode = fec_control.DEFAULT_MODE
            profiles = {}
            for wname, praw in (raw_fec.get("wan_profiles") or {}).items():
                profiles[wname] = WanProfileCfg(
                    name=wname,
                    loss_table=praw.get("loss_table",
                                        fec_control.DEFAULT_CELL_LOSS_TABLE),
                    ramp_up_ticks=int(praw.get("ramp_up_ticks", 1)),
                    ramp_down_hold_s=float(praw.get("ramp_down_hold_s", 60.0)),
                    floor_ratio=praw.get("floor_ratio", "8:0"),
                    signal_floor_fec=praw.get(
                        "signal_floor_fec", fec_control.DEFAULT_SIGNAL_FLOOR_FEC))
            fec_cfg = FecCfg(
                # Derive `enabled` from the resolved mode so an explicit `mode`
                # always wins over the deprecated `enabled` bool. Leaving a
                # stale `enabled:false` next to `mode:"adaptive"` must NOT keep
                # FEC silently off (and vice-versa).
                enabled=(cfg_mode != fec_control.MODE_OFF),
                fifo=raw_fec["fifo"],
                loss_table=raw_fec.get("loss_table", fec_control.DEFAULT_LOSS_TABLE),
                ramp_up_ticks=int(raw_fec.get("ramp_up_ticks", 2)),
                ramp_down_hold_s=float(raw_fec.get("ramp_down_hold_s", 20.0)),
                full_mode_backoff_fec=raw_fec.get("full_mode_backoff_fec", "1:0"),
                full_min_up_wans=int(raw_fec.get("full_min_up_wans", 2)),
                wire_unit=raw_fec.get("wire_unit", "udpspeeder-client"),
                wire_stale_after_s=float(raw_fec.get("wire_stale_after_s", 30.0)),
                mode=cfg_mode,
                fixed_ratio=raw_fec.get("fixed_ratio", fec_control.DEFAULT_FIXED_RATIO),
                floor_ratio=raw_fec.get("floor_ratio", fec_control.DEFAULT_FLOOR_RATIO),
                wan_profiles=profiles,
                driver_dwell_s=float(raw_fec.get("driver_dwell_s", 120.0)),
            )

        raw_env = raw.get("environmental")
        env_cfg = None
        if raw_env is not None:
            ao = raw_env.get("auto_override", {})
            env_cfg = EnvironmentalCfg(
                enabled=bool(raw_env.get("enabled", True)),
                auto_override_path=ao["path"],
                auto_override_ttl_s=float(ao.get("ttl_s", 180.0)),
            )

        raw_maint = raw.get("maintenance_reboot")
        maint_cfg = None
        if raw_maint is not None:
            win = raw_maint.get("window", {})
            raw_hour = raw_maint.get("hour", 0)
            # int(True) == 1: without this, "hour": true reads as 1am.
            if isinstance(raw_hour, bool):
                raise ValueError(
                    "maintenance_reboot.hour must be an integer 0..23, "
                    f"got {raw_hour!r}")
            maint_cfg = MaintenanceCfg(
                enabled=bool(raw_maint.get("enabled", False)),
                hour=int(raw_hour),
                window_path=win.get(
                    "path", "/run/sbfd-ctl/maintenance_window.json"),
            )

        raw_cell = raw.get("cell_telemetry")
        cell_cfg = None
        if raw_cell is not None:
            cell_cfg = CellTelemetryCfg(
                state_path=raw_cell["state_path"],
                wan=str(raw_cell.get("wan", "wan1")),
                stale_after_s=float(raw_cell.get("stale_after_s", 10.0)),
                rsrq_degrade_db=float(raw_cell.get("rsrq_degrade_db", -12.0)),
                rsrq_recover_db=float(raw_cell.get("rsrq_recover_db", -10.0)),
                rsrp_degrade_dbm=float(raw_cell.get("rsrp_degrade_dbm", -110.0)),
                rsrp_recover_dbm=float(raw_cell.get("rsrp_recover_dbm", -108.0)),
                handoff_path=raw_cell.get("handoff_path", "/run/sbfd-ctl/cell_handoff.json"),
                handoff_ttl_s=float(raw_cell.get("handoff_ttl_s", 30.0)))

        raw_loc = raw.get("location_fec")
        loc_cfg = None
        if raw_loc is not None:
            # bool("false") is True, so a string here would turn the operator's
            # "off" into on. Absent still means on; present must be a boolean.
            loc_enabled = raw_loc.get("enabled", True)
            if not isinstance(loc_enabled, bool):
                raise ValueError("location_fec.enabled must be true or false")
            loc_cfg = LocationFecCfg(
                state_path=raw_loc.get("state_path", "/run/sbfd-ctl/location_fec.json"),
                enabled=loc_enabled,
                stale_after_s=float(raw_loc.get("stale_after_s", 30.0)))

        raw_notif = raw.get("notifications")
        notif_cfg = None
        if raw_notif is not None:
            notif_cfg = notify.NotifyCfg(
                topic=raw_notif["topic"],
                min_interval_s=float(raw_notif.get("min_interval_s", 30.0)),
                command=str(raw_notif.get("command",
                                          notify.DEFAULT_COMMAND)),
                wan_down_hold_s=float(raw_notif.get("wan_down_hold_s", 10.0)),
                switch_hold_s=float(raw_notif.get("switch_hold_s", 60.0)),
                fec_alerts=bool(raw_notif.get("fec_alerts", False)),
            )

        cfg = Config(
            wans=wans,
            relay=relay,
            engarde=engarde,
            nft=nft,
            policy=policy,
            ui_listen=raw["ui"]["listen"],
            sbfd_local_state=raw["sbfd_local_state"],
            runtime_state=raw["runtime_state"],
            persist_state=raw["persist_state"],
            published_state=raw["published_state"],
            egress=egress,
            fec=fec_cfg,
            environmental=env_cfg,
            maintenance=maint_cfg,
            cell=cell_cfg,
            location=loc_cfg,
            notifications=notif_cfg,
            map=raw.get("map"),
        )
    except KeyError as e:
        raise ValueError(f"{path}: missing required key {e}") from e

    if policy.default_mode not in VALID_MODES:
        raise ValueError(f"policy.default_mode must be one of {sorted(VALID_MODES)}, got {policy.default_mode!r}")
    if policy.default_master_policy not in VALID_POLICIES:
        raise ValueError(f"policy.default_master_policy must be one of {sorted(VALID_POLICIES)}")
    if policy.default_master_wan not in wans:
        raise ValueError(f"policy.default_master_wan {policy.default_master_wan!r} not in wans={list(wans)}")
    if policy.failback_hold_s < 0:
        raise ValueError(f"policy.failback_hold_s must be >= 0, got {policy.failback_hold_s}")
    if relay.fetch_interval_s <= 0:
        raise ValueError(f"relay.fetch_interval_s must be > 0, got {relay.fetch_interval_s}")
    if relay.fetch_timeout_s <= 0:
        raise ValueError(f"relay.fetch_timeout_s must be > 0, got {relay.fetch_timeout_s}")
    if not 1 <= engarde.server_port <= 65535:
        raise ValueError(f"engarde.server_port must be in 1..65535, got {engarde.server_port}")
    if egress.default_mode not in VALID_EGRESS_MODES:
        raise ValueError(
            f"egress.default_mode must be one of {sorted(VALID_EGRESS_MODES)}, "
            f"got {egress.default_mode!r}")
    # A negative dwell promotes a challenger on the very next tick, and NaN or
    # inf leaves one pending forever — both silently defeat the hysteresis
    # rather than failing, so reject them at load.
    if fec_cfg is not None and not (0 <= fec_cfg.driver_dwell_s < float("inf")):
        raise ValueError(
            f"fec.driver_dwell_s must be finite and >= 0, "
            f"got {fec_cfg.driver_dwell_s}")
    if env_cfg is not None and env_cfg.auto_override_ttl_s <= 0:
        raise ValueError(
            f"environmental.auto_override.ttl_s must be > 0, got {env_cfg.auto_override_ttl_s}")
    if maint_cfg is not None and not 0 <= maint_cfg.hour <= 23:
        raise ValueError(
            f"maintenance_reboot.hour must be 0..23, got {maint_cfg.hour}")
    if cell_cfg is not None:
        # Finiteness FIRST: json.loads accepts the barewords NaN/Infinity, and
        # every comparison against NaN is False (see hotspot_watchdog.load_config's
        # own comment on this) -- so e.g. a NaN handoff_ttl_s sails straight
        # through the `<= 0` check below and makes the sanity TTL in
        # load_cell_handoff inert, letting a wedged handoff file hold forced
        # duplication open forever. Reject all cell numeric fields up front.
        for k in ("stale_after_s", "rsrq_degrade_db", "rsrq_recover_db",
                  "rsrp_degrade_dbm", "rsrp_recover_dbm", "handoff_ttl_s"):
            v = getattr(cell_cfg, k)
            if not math.isfinite(v):
                raise ValueError(f"cell_telemetry.{k} must be a finite number, got {v!r}")
        if cell_cfg.stale_after_s <= 0:
            raise ValueError("cell_telemetry.stale_after_s must be > 0")
        if cell_cfg.rsrq_recover_db <= cell_cfg.rsrq_degrade_db:
            raise ValueError("cell_telemetry.rsrq_recover_db must be > rsrq_degrade_db")
        if cell_cfg.rsrp_recover_dbm <= cell_cfg.rsrp_degrade_dbm:
            raise ValueError("cell_telemetry.rsrp_recover_dbm must be > rsrp_degrade_dbm")
        if cell_cfg.wan not in wans:
            raise ValueError(
                f"cell_telemetry.wan {cell_cfg.wan!r} not in wans={list(wans)}")
        if cell_cfg.handoff_ttl_s <= 0:
            raise ValueError("cell_telemetry.handoff_ttl_s must be > 0")
    if loc_cfg is not None:
        # NaN is not <= 0 (every comparison against NaN is False), so it would
        # sail through and make the staleness gate inert: a dead daemon's
        # floor would hold forever.
        if not math.isfinite(loc_cfg.stale_after_s) or loc_cfg.stale_after_s <= 0:
            raise ValueError("location_fec.stale_after_s must be a finite number > 0")
    if fec_cfg is not None:
        for wname in fec_cfg.wan_profiles:
            if wname not in wans:
                raise ValueError(
                    f"fec.wan_profiles key {wname!r} not in wans={list(wans)}")
    if cfg.notifications is not None:
        if not cfg.notifications.topic:
            raise ValueError("notifications.topic must be a non-empty string")
        if cfg.notifications.min_interval_s < 0:
            raise ValueError(
                f"notifications.min_interval_s must be >= 0, "
                f"got {cfg.notifications.min_interval_s}")
        if cfg.notifications.wan_down_hold_s < 0:
            raise ValueError(
                f"notifications.wan_down_hold_s must be >= 0, "
                f"got {cfg.notifications.wan_down_hold_s}")
        if cfg.notifications.switch_hold_s < 0:
            raise ValueError(
                f"notifications.switch_hold_s must be >= 0, "
                f"got {cfg.notifications.switch_hold_s}")

    return cfg


# -- Runtime overlay (UI-set mode/policy) -----------------------------------

@dataclass
class RuntimeOverlay:
    mode: Optional[str] = None
    master_policy: Optional[str] = None
    master_wan: Optional[str] = None
    persist: bool = False
    set_by: str = "boot-default"
    set_ts: float = 0.0
    egress_mode: Optional[str] = None
    # FEC tri/quad-state override. Legacy fec_enabled boolean is still accepted
    # on load and on the HTTP API, mapped into fec_mode.
    fec_enabled: Optional[bool] = None  # deprecated; kept for backward-compat reads
    fec_mode: Optional[str] = None
    fec_fixed_ratio: Optional[str] = None
    fec_floor_ratio: Optional[str] = None
    environmental_enabled: Optional[bool] = None
    location_fec_enabled: Optional[bool] = None
    # First integer overlay field. Note hour 0 (midnight) is falsy and real, so
    # every read/write path here must test for None, never for truthiness.
    maintenance_enabled: Optional[bool] = None
    maintenance_hour: Optional[int] = None


def load_runtime_overlay(cfg: Config) -> RuntimeOverlay:
    """Load runtime.json from /run; if absent, try persist file; else defaults."""
    for path, src in ((cfg.runtime_state, "runtime"), (cfg.persist_state, "persist")):
        try:
            with open(path) as f:
                raw = _json.load(f)
            # Legacy fec_enabled bool migrates into fec_mode at load time so
            # operators who set "disabled" pre-tri-state stay disabled.
            legacy_enabled = raw.get("fec_enabled")
            loaded_fec_mode = raw.get("fec_mode")
            if loaded_fec_mode is None and isinstance(legacy_enabled, bool):
                loaded_fec_mode = (fec_control.MODE_ADAPTIVE if legacy_enabled
                                   else fec_control.MODE_OFF)
            return RuntimeOverlay(
                mode=raw.get("mode"),
                master_policy=raw.get("master_policy"),
                master_wan=raw.get("master_wan"),
                persist=bool(raw.get("persist", False)),
                set_by=raw.get("set_by", src),
                set_ts=float(raw.get("set_ts", 0.0)),
                egress_mode=raw.get("egress_mode"),
                fec_enabled=legacy_enabled,
                fec_mode=loaded_fec_mode,
                fec_fixed_ratio=raw.get("fec_fixed_ratio"),
                fec_floor_ratio=raw.get("fec_floor_ratio"),
                # Only a real bool is an operator opinion. The API path
                # already refuses anything else, but this file can be hand
                # edited, and bool("false") is True — so "off" written by hand
                # would have switched the feature ON. None lets the config
                # default stand, which is what an unusable value means.
                environmental_enabled=(
                    raw.get("environmental_enabled")
                    if isinstance(raw.get("environmental_enabled"), bool)
                    else None),
                location_fec_enabled=(
                    raw.get("location_fec_enabled")
                    if isinstance(raw.get("location_fec_enabled"), bool)
                    else None),
                maintenance_enabled=raw.get("maintenance_enabled"),
                maintenance_hour=raw.get("maintenance_hour"),
            )
        except (FileNotFoundError, ValueError, OSError):
            continue
    return RuntimeOverlay()


def _atomic_write_text(path: str, body: str) -> None:
    """Write via a temp file + os.replace so a concurrent reader never sees a
    truncated/partial file. os.replace is atomic within a filesystem.

    The temp path is unique per writer. A FIXED `<name>.tmp` is one inode
    shared by every concurrent writer: thread B opens it while A is between
    write and replace, A's replace moves that inode to the live path, and B's
    remaining writes land directly in the LIVE file -- unsynchronised and not
    atomic. The UI server is threaded, so that is reachable from two POSTs."""
    p = Path(path)
    tmp = Path("%s.%d.%d.tmp" % (p, os.getpid(), threading.get_ident()))
    try:
        tmp.write_text(body)
        os.replace(tmp, p)
    except BaseException:
        # Otherwise a permanently failing write leaves one orphan per attempt,
        # and the callers here retry on a 1 Hz loop.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def save_runtime_overlay(cfg: Config, ov: RuntimeOverlay):
    payload = {
        "mode": ov.mode,
        "master_policy": ov.master_policy,
        "master_wan": ov.master_wan,
        "persist": ov.persist,
        "set_by": ov.set_by,
        "set_ts": ov.set_ts,
        "egress_mode": ov.egress_mode,
        "fec_enabled": ov.fec_enabled,
        "fec_mode": ov.fec_mode,
        "fec_fixed_ratio": ov.fec_fixed_ratio,
        "fec_floor_ratio": ov.fec_floor_ratio,
        "environmental_enabled": ov.environmental_enabled,
        "location_fec_enabled": ov.location_fec_enabled,
        "maintenance_enabled": ov.maintenance_enabled,
        "maintenance_hour": ov.maintenance_hour,
    }
    body = _json.dumps(payload, indent=2)
    Path(cfg.runtime_state).parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(cfg.runtime_state, body)
    if ov.persist:
        Path(cfg.persist_state).parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(cfg.persist_state, body)
    elif Path(cfg.persist_state).exists():
        try:
            Path(cfg.persist_state).unlink()
        except OSError:
            pass


@dataclass
class AutoOverride:
    force_full: bool
    source: str
    reason: str
    set_ts: float


def load_auto_override(cfg: Config, now: float) -> Optional[AutoOverride]:
    """Read the environmental auto-override file. Returns None when the feature
    is unconfigured, or the file is missing / malformed / stale. Best-effort and
    fail-open — a bad file is ignored, never raised (mirrors fetch_relay_fec)."""
    env = cfg.environmental
    if env is None:
        return None
    try:
        raw = _json.loads(Path(env.auto_override_path).read_text())
        set_ts = float(raw["set_ts"])
    except (FileNotFoundError, ValueError, OSError, KeyError, TypeError,
            OverflowError):
        return None
    # Same guard, same reason as load_cell_handoff: json.loads accepts the
    # bareword NaN/Infinity, and the staleness test below is a comparison that
    # either defeats — holding forced full mode open with no expiry. A far-
    # future set_ts does the same, so reject it past a clock-skew band.
    if not math.isfinite(set_ts) or set_ts > now + 5.0:
        return None
    if now - set_ts > env.auto_override_ttl_s:
        return None
    return AutoOverride(
        # Only a real bool may actuate: bool() of any non-empty string is True,
        # so a hand-edited or corrupt "false" would buy full redundancy — and
        # full redundancy puts the metered link into the bundle. The record
        # still loads, so source/reason still reach the readout.
        force_full=(raw.get("force_full") is True),
        source=str(raw.get("source", "")),
        reason=str(raw.get("reason", "")),
        set_ts=set_ts,
    )


def load_maintenance_window(cfg: Config, now: float) -> Optional[dict]:
    """The WAN currently being rebooted on purpose, or None.

    `now` must be a WALL-CLOCK epoch: `until` is written by maintenance_reboot,
    a different process, as time.time(). Comparing it to a monotonic clock would
    be a silent always-true/always-false test.

    Fail-open, like load_auto_override: unconfigured, missing, unparseable,
    not-a-dict, no/invalid `until`, or expired all return None. A bad window
    must suppress NOTHING — the failure mode of this feature is a spurious page,
    never a missed one."""
    if cfg.maintenance is None:
        return None
    try:
        raw = _json.loads(Path(cfg.maintenance.window_path).read_text())
    except (FileNotFoundError, ValueError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    until = raw.get("until")
    # bool is an int subclass; `until: true` is not a deadline.
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        return None
    if now >= until:
        return None
    return raw


def load_cell_sample(cfg: Config, now: float) -> Optional[dict]:
    """Last modem telemetry reading — validated/coerced but NOT staleness-
    filtered: the UI wants last-known + a stale flag, and the FEC signal
    floor does its own freshness gate. Fail-open like load_auto_override."""
    if cfg.cell is None:
        return None
    try:
        raw = _json.loads(Path(cfg.cell.state_path).read_text())
        set_ts = float(raw["set_ts"])
    except (FileNotFoundError, ValueError, OSError, KeyError, TypeError,
            OverflowError):
        return None
    out = {"set_ts": set_ts}
    for k in ("rsrp", "rsrq", "sinr"):
        v = raw.get(k)
        out[k] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    for k in ("cell_id", "band"):
        v = raw.get(k)
        out[k] = str(v) if v is not None else None
    return out


def load_location_floor(cfg: Config, now: float) -> Optional[dict]:
    """{wan: {level, reason}} from location_fec.py, or None when unconfigured,
    missing, malformed, or older than stale_after_s. Fail-open like
    load_auto_override: a floor that cannot be read is no floor. Only `level`
    is trusted, and only as a non-bool int — it crosses a process boundary.

    Finiteness and future-timestamp guards match load_cell_handoff's
    precedent below: json.loads accepts the bareword NaN, and
    `now - set_ts > stale_after_s` is False forever against it, making the
    staleness gate inert -- a dead daemon's floor would hold forever."""
    if cfg.location is None:
        return None
    try:
        raw = _json.loads(Path(cfg.location.state_path).read_text())
        set_ts = float(raw["set_ts"])
    except (FileNotFoundError, ValueError, OSError, KeyError, TypeError,
            OverflowError):
        return None
    if not math.isfinite(set_ts):
        return None
    if set_ts > now + 5.0:
        return None
    if now - set_ts > cfg.location.stale_after_s:
        return None
    wans_raw = raw.get("wans")
    if wans_raw is None:
        wans_raw = {}
    if not isinstance(wans_raw, dict):
        return None
    out = {}
    for wan, obj in wans_raw.items():
        if not isinstance(obj, dict):
            continue
        level = obj.get("level")
        if isinstance(level, bool) or not isinstance(level, int):
            continue
        out[wan] = {"level": level, "reason": str(obj.get("reason", ""))}
    return out


@dataclass
class HandoffWindow:
    reason: str
    set_ts: float
    until_ts: float


def load_cell_handoff(cfg: Config, now: float) -> Optional[HandoffWindow]:
    """The open duplication window, or None. Fail-open like load_auto_override.
    Two clocks gate it: until_ts (the window itself) and a sanity TTL on
    set_ts — a wedged/ancient file must never hold full mode open.

    Finiteness FIRST (CodeRabbit PR#5 CR1): json.loads accepts the barewords
    NaN/Infinity (see hotspot_watchdog.load_config's own comment on this), and
    both gates below are comparisons that a non-finite value defeats — an
    until_ts of Infinity satisfies `now < until_ts` forever, holding forced
    full-mode duplication on the metered link open with no expiry. Reject
    non-finite set_ts/until_ts, and a set_ts from the future beyond a small
    clock-skew guard band (a forged/corrupt far-future pair must not ride
    under the sanity-TTL age check), before trusting either clock."""
    if cfg.cell is None:
        return None
    try:
        raw = _json.loads(Path(cfg.cell.handoff_path).read_text())
        set_ts = float(raw["set_ts"])
        until_ts = float(raw["until_ts"])
    except (FileNotFoundError, ValueError, OSError, KeyError, TypeError,
            OverflowError):
        return None
    if not (math.isfinite(set_ts) and math.isfinite(until_ts)):
        return None
    if set_ts > now + 5.0:
        return None
    if now >= until_ts:
        return None
    if now - set_ts > cfg.cell.handoff_ttl_s:
        return None
    return HandoffWindow(reason=str(raw.get("reason", "")),
                         set_ts=set_ts, until_ts=until_ts)


def cell_snapshot(cfg: Config, sample: Optional[dict], now: float) -> dict:
    """Published `cell` block. Absent sample still publishes configured=true
    so the UI can show 'no telemetry' instead of hiding the panel."""
    if cfg.cell is None:
        return {"configured": False}
    if sample is None:
        return {"configured": True, "wan": cfg.cell.wan, "rsrp": None,
                "rsrq": None, "sinr": None, "cell_id": None, "band": None,
                "age_s": None, "stale": True}
    age = max(0.0, now - sample["set_ts"])
    return {"configured": True, "wan": cfg.cell.wan, "rsrp": sample["rsrp"],
            "rsrq": sample["rsrq"], "sinr": sample["sinr"],
            "cell_id": sample["cell_id"], "band": sample["band"],
            "age_s": round(age, 1), "stale": age > cfg.cell.stale_after_s}


def effective_policy(cfg: Config, ov: RuntimeOverlay) -> tuple:
    mode = ov.mode if ov.mode in VALID_MODES else cfg.policy.default_mode
    policy = ov.master_policy if ov.master_policy in VALID_POLICIES else cfg.policy.default_master_policy
    master_wan = ov.master_wan if ov.master_wan in cfg.wans else cfg.policy.default_master_wan
    egress_mode = ov.egress_mode if ov.egress_mode in VALID_EGRESS_MODES else cfg.egress.default_mode
    return mode, policy, master_wan, egress_mode


def effective_fec_enabled(cfg: Config, ov: RuntimeOverlay) -> bool:
    """True when FEC is configured AND the effective mode is not 'off'.
    Retained for backward compatibility with callers and the published state."""
    if not (cfg.fec and cfg.fec.enabled):
        return False
    return effective_fec_mode(cfg, ov) != fec_control.MODE_OFF


def effective_fec_mode(cfg: Config, ov: RuntimeOverlay) -> str:
    """Operator override if set, else the config default. Returns MODE_OFF when
    FEC is unconfigured. Honors legacy ov.fec_enabled when ov.fec_mode is unset."""
    if not (cfg.fec and cfg.fec.enabled):
        return fec_control.MODE_OFF
    if ov.fec_mode in fec_control.ALL_MODES:
        return ov.fec_mode
    if ov.fec_enabled is not None:
        return (fec_control.MODE_ADAPTIVE if ov.fec_enabled
                else fec_control.MODE_OFF)
    return cfg.fec.mode


def effective_fec_fixed_ratio(cfg: Config, ov: RuntimeOverlay) -> str:
    """The ratio used by MODE_FIXED — operator override wins, else cfg default."""
    cfg_fixed = fec_control.safe_ratio(cfg.fec.fixed_ratio if cfg.fec else None,
                                       fec_control.DEFAULT_FIXED_RATIO, logging)
    return fec_control.safe_ratio(ov.fec_fixed_ratio, cfg_fixed, logging)


def effective_fec_floor_ratio(cfg: Config, ov: RuntimeOverlay,
                              profile_floor: Optional[str] = None) -> str:
    """Floor precedence: operator runtime override > active WAN profile >
    config default. The profile slot lets the cellular profile drop the
    min_adaptive floor to 8:0 (true zero) without a mode change."""
    cfg_floor = fec_control.safe_ratio(cfg.fec.floor_ratio if cfg.fec else None,
                                       fec_control.DEFAULT_FLOOR_RATIO, logging)
    base = fec_control.safe_ratio(profile_floor, cfg_floor, logging) \
        if profile_floor is not None else cfg_floor
    return fec_control.safe_ratio(ov.fec_floor_ratio, base, logging)


def effective_environmental_enabled(cfg: Config, ov: RuntimeOverlay) -> bool:
    """Resolve the environmental master toggle.

    Unlike effective_fec_enabled (where cfg.fec.enabled is a hard kill-switch),
    cfg.environmental.enabled is only the boot-time DEFAULT: the operator's
    runtime override (ov.environmental_enabled) wins in either direction. The
    feature is hard-off only when unconfigured (cfg.environmental is None)."""
    if cfg.environmental is None:
        return False
    if ov.environmental_enabled is not None:
        return ov.environmental_enabled
    return bool(cfg.environmental.enabled)


def effective_location_fec_enabled(cfg: Config, ov: RuntimeOverlay) -> bool:
    """Same shape as effective_environmental_enabled: config is the default,
    the operator's runtime toggle wins, hard-off only when unconfigured."""
    if cfg.location is None:
        return False
    if ov.location_fec_enabled is not None:
        return ov.location_fec_enabled
    return bool(cfg.location.enabled)


def location_floor_for_driver(floors, enabled, driver, table):
    """(level, reason) the location floor asks of the FEC DRIVER WAN only —
    a place that kills cellular must not lift the floor while satellite is
    driving. Clamped to the active profile's table. 0 means no opinion."""
    if not enabled or not floors or not driver:
        return 0, ""
    entry = floors.get(driver)
    if not entry:
        return 0, ""
    level = fec_control.apply_location_floor(0, entry.get("level"), table)
    return level, (entry.get("reason", "") if level > 0 else "")


def location_floor_active(ratio_on_wire, ratio_with_location,
                          ratio_without_location):
    """Is the location floor the reason the wire carries the parity it does?

    THE SEMANTIC, stated once so it is not re-litigated: `active` is about the
    ratio standing on the wire THIS TICK, not about which tick caused the last
    transition. It answers "if the location floor said nothing right now, would
    the controller write something lower?" — so a wire already holding 8:6 from
    an earlier loss-driven decision, with loss since fallen to a 8:0 baseline
    and the floor asking 8:6, IS active even though no write happened and
    location did not cause the actuator's current state. The floor is the only
    thing holding that parity up; the moment it released, 8:0 would go out.
    Attributing by who caused the last write would show the operator "not
    active" for a floor that is doing all the work, which is the reading that
    matters when a vehicle is standing in a known bad place.

    Two things must both hold, and each catches cases the other misses.

    The floor must have changed this tick's DECISION — `off` and `fixed`
    discard the adaptive ratio outright, a signal floor may already stand
    higher, and a min_adaptive config floor of 8:8 lifts every level to the
    same top rung, so in all of those the floor asked and changed nothing.

    And that decision must be what the actuator actually HOLDS. A refused FIFO
    write leaves an older ratio flowing, and an older ratio earns the location
    floor no credit even when it happens to differ from today's baseline: a
    tick that accepted 8:2 off real loss, followed by the loss clearing, leaves
    8:2 on the wire against a baseline of 8:0 for reasons that are entirely
    historical.

    A None ratio_on_wire means nothing has ever been written, so there is no
    parity to credit the floor with."""
    if ratio_on_wire is None:
        return False
    return (ratio_with_location != ratio_without_location
            and ratio_on_wire == ratio_with_location)


def pinned_ladder_level(backoff_ratio, table, location_level):
    """The single rung the ladder's span collapses to under full-redundancy
    backoff. Backoff holds the ADAPTIVE ENGINE at one rung, but the location
    floor is deliberately not suppressed by backoff, so it can lift the applied
    ratio above that rung — and a span pinned to the backoff rung alone would
    draw the applied dot outside the span it is meant to sit in."""
    return max(fec_control.ratio_to_level(backoff_ratio, table), location_level)


def effective_maintenance_enabled(cfg: Config, ov: RuntimeOverlay) -> bool:
    """Resolve the maintenance-reboot toggle. Same shape as environmental (NOT
    FEC): cfg.maintenance.enabled is only the boot default and the operator's
    overlay wins in either direction. Hard-off only when unconfigured."""
    if cfg.maintenance is None:
        return False
    if ov.maintenance_enabled is not None:
        return ov.maintenance_enabled
    return bool(cfg.maintenance.enabled)


def effective_maintenance_hour(cfg: Config, ov: RuntimeOverlay) -> int:
    """Resolve the reboot hour. The overlay wins only when it is a sane hour:
    bool is an int subclass, so True must not be read as hour 1, and 0 is
    midnight — falsy but perfectly valid, so this tests the value, not truth."""
    if cfg.maintenance is None:
        return 0
    h = ov.maintenance_hour
    if not isinstance(h, bool) and isinstance(h, int) and 0 <= h <= 23:
        return h
    return cfg.maintenance.hour


def apply_auto_override(mode: str, env_enabled: bool,
                        auto: Optional[AutoOverride]) -> tuple:
    """Fold the environmental auto-override into the effective mode.

    Precedence: when the environmental toggle is on AND a fresh override requests
    force_full, raise to 'full'. Otherwise pass the operator/default mode through.
    Auto can only raise to full; it never forces master_backup. Returns
    (effective_mode, active_override_or_None)."""
    active = bool(env_enabled and auto is not None and auto.force_full)
    if active:
        return "full", auto
    return mode, None


def apply_handoff_window(mode: str, window: Optional[HandoffWindow],
                         cell_wan: Optional[str],
                         active_wans: set) -> tuple:
    """Fold the duplication window into the effective mode. Only raises to
    'full', and only while the telemetry WAN is actually carrying traffic —
    a handoff on an idle backup WAN needs no duplication. Returns
    (effective_mode, active_window_or_None); an already-full mode reports the
    window as inactive so the cap accounting counts only windows that DID
    something."""
    if window is None or cell_wan is None:
        return mode, None
    if mode == "full" or cell_wan not in active_wans:
        return mode, None
    return "full", window


def environmental_snapshot(configured: bool, env_enabled: bool,
                           active: Optional[AutoOverride],
                           auto: Optional[AutoOverride], now: float) -> dict:
    """Build the /api/state 'environmental' block. `active` is the override that
    actually forced full (or None); `auto` is the loaded override regardless of
    whether it was honored, used for the status readout."""
    return {
        "configured": configured,
        "enabled": env_enabled,
        "active": active is not None,
        "force_full": bool(auto.force_full) if auto else False,
        "source": auto.source if auto else None,
        "reason": auto.reason if auto else None,
        "age_s": (now - auto.set_ts) if auto else None,
    }


def fetch_relay_fec(url, timeout_s) -> dict:
    """Best-effort GET of the relay /fec endpoint. Returns {ok, data, error};
    never raises (mirrors fetch_remote_sbfd_state's defensive style)."""
    if not url:
        return {"ok": False, "data": None, "error": "no fec_url configured"}
    try:
        status, reason, body = _relay_request(url, timeout_s=timeout_s)
    except (http.client.HTTPException, OSError, ValueError) as e:
        return {"ok": False, "data": None, "error": f"transport: {e}"}
    if status != 200:
        return {"ok": False, "data": None, "error": f"HTTP {status}: {reason}"}
    try:
        return {"ok": True, "data": _json.loads(body.decode()), "error": None}
    except (ValueError, UnicodeDecodeError) as e:
        return {"ok": False, "data": None, "error": f"parse: {e}"}


_post_relay_fec_last_warned = None


def post_relay_fec(url, mode, fixed_ratio, floor_ratio, timeout_s,
                   client_loss_pct=None, wan_profile=None,
                   signal_floor=None, location_level=None) -> bool:
    """Best-effort POST of desired (mode, fixed_ratio, floor_ratio) to relay /fec.

    client_loss_pct carries our locally measured relay->client loss — the
    direction the relay's TX leg repairs but cannot see (sbfd loss is
    RX-side). wan_profile/signal_floor carry the per-WAN policy selection to
    the relay leg, and location_level the floor this PLACE is known to need,
    so both legs lift together; older relays ignore unknown keys. Also sends
    the legacy `enabled` boolean so an older relay binary still honors the
    off/on intent during a rolling upgrade. Returns True iff 200.

    A non-200 HTTP response (e.g. 400 from an unknown wan_profile, which
    would otherwise 400 forever and invisibly on every reconcile tick) is
    distinguished from a transport failure and logged at WARNING with the
    status code and a truncated body. Debounced on (code, body) so a
    persistent failure warns once, not every tick — mirrors
    fec_control.safe_ratio's debounce spirit."""
    global _post_relay_fec_last_warned
    if not url:
        return False
    payload = {
        "mode": mode,
        "fixed_ratio": fixed_ratio,
        "floor_ratio": floor_ratio,
        "enabled": mode != fec_control.MODE_OFF,
    }
    if client_loss_pct is not None:
        payload["client_loss_pct"] = round(client_loss_pct, 2)
    if wan_profile is not None:
        payload["wan_profile"] = wan_profile
    if signal_floor is not None:
        payload["signal_floor"] = bool(signal_floor)
    # Coerce nothing: int() on a bad value would raise from OUTSIDE the try
    # below and take the controller tick with it. A level that is not a plain
    # int (bool included — the relay 400s it) costs us the key, not the push.
    if isinstance(location_level, int) and not isinstance(location_level, bool):
        payload["location_level"] = location_level
    body = _json.dumps(payload).encode()
    try:
        status, _reason, resp_body = _relay_request(
            url, method="POST", body=body,
            headers={"Content-Type": "application/json"}, timeout_s=timeout_s)
    except (http.client.HTTPException, OSError, ValueError):
        return False
    if status == 200:
        return True
    text = resp_body.decode("utf-8", "replace")
    signature = (status, text[:200])
    if signature != _post_relay_fec_last_warned:
        _post_relay_fec_last_warned = signature
        logging.warning("post relay fec rejected: HTTP %s: %s", status, text[:200])
    return False


# -- Helpers -----------------------------------------------------------------

def is_master_wan_down(published_state_path: str, master_wan: str) -> bool:
    """Read the latest published state and return True iff the master WAN is DOWN.

    Returns False on any read error / missing field — fail-open (allow the apply).
    """
    try:
        snap = _json.loads(Path(published_state_path).read_text())
    except (FileNotFoundError, ValueError, OSError):
        return False
    client_local = snap.get("client_local", {})
    wan = client_local.get(master_wan, {})
    return wan.get("state") == "DOWN"


# -- State publisher ---------------------------------------------------------

def publish_state(cfg: Config, snapshot: dict):
    Path(cfg.published_state).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(cfg.published_state).with_suffix(".tmp")
    tmp.write_text(_json.dumps(snapshot, indent=2))
    os.replace(tmp, cfg.published_state)


def validate_runtime_payload(payload: dict, wan_names: set):
    """Returns (ok: bool, error_msg: Optional[str])."""
    if not isinstance(payload, dict):
        return False, "payload must be a JSON object"
    if "mode" in payload and payload["mode"] not in VALID_MODES:
        return False, f"mode must be one of {sorted(VALID_MODES)}"
    if "master_policy" in payload and payload["master_policy"] not in VALID_POLICIES:
        return False, f"master_policy must be one of {sorted(VALID_POLICIES)}"
    if "master_wan" in payload and payload["master_wan"] not in wan_names:
        return False, f"master_wan must be one of {sorted(wan_names)}"
    if "persist" in payload and not isinstance(payload["persist"], bool):
        return False, "persist must be true or false"
    if "egress_mode" in payload and payload["egress_mode"] not in VALID_EGRESS_MODES:
        return False, f"egress_mode must be one of {sorted(VALID_EGRESS_MODES)}"
    if "fec_enabled" in payload and not isinstance(payload["fec_enabled"], bool):
        return False, "fec_enabled must be true or false"
    if "fec_mode" in payload and payload["fec_mode"] not in fec_control.ALL_MODES:
        return False, f"fec_mode must be one of {sorted(fec_control.ALL_MODES)}"
    # Normalize ratio entries in place so the apply block stores the canonical
    # 'x:y'. Resolving here and only here keeps the percent rule in one place —
    # the runtime and persist files never hold a percent string.
    for _key in ("fec_fixed_ratio", "fec_floor_ratio"):
        if _key in payload:
            if payload[_key] is None:
                # Explicit null = clear the operator override; the effective_*
                # helpers fall through to profile/config. Distinct from an
                # absent key, which leaves the stored override untouched.
                continue
            try:
                payload[_key] = fec_control.resolve_ratio(payload[_key])
            except ValueError as e:
                return False, f"{_key}: {e}"
    if "environmental_enabled" in payload and not isinstance(payload["environmental_enabled"], bool):
        return False, "environmental_enabled must be true or false"
    if ("location_fec_enabled" in payload
            and not isinstance(payload["location_fec_enabled"], bool)):
        return False, "location_fec_enabled must be true or false"
    if ("maintenance_enabled" in payload
            and not isinstance(payload["maintenance_enabled"], bool)):
        return False, "maintenance_enabled must be true or false"
    if "maintenance_hour" in payload:
        h = payload["maintenance_hour"]
        # bool is an int subclass: reject True/False BEFORE the range check, or
        # `true` passes as hour 1 and authorizes a reboot nobody asked for.
        if isinstance(h, bool) or not isinstance(h, int):
            return False, "maintenance_hour must be an integer 0..23"
        if not 0 <= h <= 23:
            return False, "maintenance_hour must be 0..23"
    return True, None


# -- map UI helpers ------------------------------------------------------------

_MAP_DEFAULTS = {
    "stations_path": "/var/lib/sbfd-ctl/stations.json",
    "labels_path": "/var/lib/sbfd-ctl/station_labels.json",
    "environ_points_path": "/run/sbfd-ctl/environ_points.json",
    "gpsd": {"host": "127.0.0.1", "port": 2947},
    "tile_cache": {"path": "/var/lib/sbfd-ctl/tilecache",
                   "max_mb": 512, "max_zoom": 17},
    "location_store_path": "/var/lib/sbfd-ctl/location_fec_store.json",
    "location_config_path": "/etc/sbfd-ctl/location-fec.json",
    # Zones the operator drew on the map. We write it; location_fec reads it,
    # keyed off its mtime, so a drawn zone is live within a poll. Distinct
    # from location_config_path, which ships with the box.
    "location_zones_path": "/var/lib/sbfd-ctl/location_zones.json",
    "max_location_tiles": 2000,
}

_SID_RE = re.compile(r"^s[0-9]+$")
_ZID_RE = re.compile(r"^z[0-9]+$")


def resolve_map_cfg(raw) -> dict:
    """Merge the optional config `map` section over deployment defaults
    (one level deep — the nested gpsd/tile_cache dicts merge key-wise)."""
    out = {k: (dict(v) if isinstance(v, dict) else v)
           for k, v in _MAP_DEFAULTS.items()}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k].update(v)
            else:
                out[k] = v
    return out


def validate_label(payload) -> tuple:
    """(ok, sid, label, err). Empty label means delete. Labels are stored
    verbatim (minus control chars) and must only ever be rendered as text."""
    if not isinstance(payload, dict):
        return (False, None, None, "payload must be an object")
    sid = payload.get("id")
    if not isinstance(sid, str) or not _SID_RE.match(sid):
        return (False, None, None, "invalid station id")
    label = payload.get("label", "")
    if not isinstance(label, str):
        return (False, None, None, "label must be a string")
    label = "".join(ch for ch in label if ch.isprintable())[:48]
    return (True, sid, label, None)


# A zone is a FEC floor, not a region: 50 km of raised parity is far more
# likely to be a slipped decimal point than an intention.
_ZONE_MAX_RADIUS_M = 50000.0


def _zone_number(value):
    """A real, finite number as a float, or None.

    bool is a subclass of int, and json.loads accepts the barewords NaN and
    Infinity. NaN in particular fails EVERY comparison, so a range check alone
    passes it straight through — which is how this file has twice let a
    non-finite number reach something that only bounds-checked it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def validate_zone_payload(payload, wan_names, table_len,
                          keep_level=None) -> tuple:
    """(ok, zone_or_id, err) for one operator-drawn location zone. Pure.

    location_fec.validate_zone stays the authority on what a zone IS, and
    everything accepted here satisfies it. This adds only what an API needs
    and a hand-edited config file does not: an id, an explicit delete, a
    radius ceiling, and a named error instead of a silent skip — the operator
    is standing at the map waiting to be told why the save was refused.

    `keep_level` is the level this zone is ALREADY stored at, when it is an
    update. A level equal to it is accepted whatever the driving table says;
    the bound below says why."""
    if not isinstance(payload, dict):
        return (False, None, "payload must be an object")
    zid = payload.get("id")
    has_id = zid is not None
    if has_id and not (isinstance(zid, str) and _ZID_RE.match(zid)):
        return (False, None, "invalid zone id")
    # `is True`, not truthiness: this removes a live FEC floor, so a stray
    # "false" or 1 from a hand-rolled client must not actuate it.
    if payload.get("delete") is True:
        if not has_id:
            return (False, None, "invalid zone id")
        return (True, {"id": zid, "delete": True}, None)
    lat = _zone_number(payload.get("lat"))
    if lat is None or not (-90.0 <= lat <= 90.0):
        return (False, None, "lat must be a number in -90..90")
    lon = _zone_number(payload.get("lon"))
    if lon is None or not (-180.0 <= lon <= 180.0):
        return (False, None, "lon must be a number in -180..180")
    radius = _zone_number(payload.get("radius_m"))
    if radius is None or not (0 < radius <= _ZONE_MAX_RADIUS_M):
        return (False, None, "radius_m must be a number in 0..%d"
                % int(_ZONE_MAX_RADIUS_M))
    level = payload.get("level")
    # bool first: int(True) is 1, so an unguarded check would turn
    # `"level": true` into a real floor of level 1 on a live vehicle.
    if isinstance(level, bool) or not isinstance(level, int) or level < 0:
        return (False, None, "level must be 0..%d" % (table_len - 1))
    if level > table_len - 1 and level != keep_level:
        # Above the top rung of the table driving right now. Refused for a
        # new level, but a level the zone ALREADY has is kept: profiles swap
        # under us, and a zone set to 4 on the base table would otherwise be
        # either uneditable while a 4-rung cellular profile drives, or
        # silently rewritten to 3 the next time its label is changed. This
        # can only preserve a level, never introduce one, so raise-only is
        # untouched.
        return (False, None, "level must be 0..%d" % (table_len - 1))
    label = payload.get("label", "")
    if not isinstance(label, str):
        return (False, None, "label must be a string")
    # Same treatment as validate_label, plus a default: this label is
    # published as the REASON a floor was raised, so it has to name something.
    label = "".join(ch for ch in label if ch.isprintable()).strip()[:48] or "zone"
    wans = payload.get("wans")
    if wans is None or wans == []:
        # Absent, null and empty all mean the same thing, and the daemon
        # spells it None: this zone applies to every WAN.
        wans = None
    elif not isinstance(wans, list):
        return (False, None, "wans must be a list of WAN names")
    else:
        for w in wans:
            if not isinstance(w, str):
                return (False, None, "wans must be a list of WAN names")
            if w not in wan_names:
                return (False, None, "unknown wan %s" % w)
        wans = list(wans)
    suppress = payload.get("suppress_learned", False)
    if not isinstance(suppress, bool):
        return (False, None, "suppress_learned must be true or false")
    zone = {"label": label, "lat": lat, "lon": lon, "radius_m": radius,
            "level": level, "wans": wans, "suppress_learned": suppress}
    if has_id:
        zone["id"] = zid
    return (True, zone, None)


def predict_from_stations(data: dict, n: int = 2) -> list:
    """Top-n predicted next station ids from a stations.json dict — the same
    ordering rule as StationTracker.predict_points (count desc, then the
    destination's last_visit desc), reimplemented read-only so the failover
    daemon does not import the tracker."""
    origin = data.get("last_station")
    if not origin:
        return []
    stations = data.get("stations", {})
    row = data.get("transitions", {}).get(origin, {})
    ranked = sorted(
        row.items(),
        key=lambda kv: (-kv[1], -stations.get(kv[0], {}).get("last_visit", 0.0)))
    return [sid for sid, _c in ranked[:n] if sid in stations]


def _read_json_file(path):
    try:
        return _json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def apply_station_label(labels_path: str, sid: str, label: str) -> dict:
    """Set (or delete, when label is empty) one label; atomic write; returns
    the resulting mapping. Labels live apart from stations.json on purpose —
    the tracker rewrites that file periodically and would race us."""
    labels = _read_json_file(labels_path)
    if not isinstance(labels, dict):
        labels = {}
    if label:
        labels[sid] = label
    else:
        labels.pop(sid, None)
    p = Path(labels_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(_json.dumps(labels))
    tmp.replace(p)
    return labels


# Every drawn zone is walked once per look-ahead point in location_fec's 1 Hz
# loop and rides in every 3 s map payload, so the collection needs a bound of
# its own -- the 4096-byte cap bounds one request, not the file. 200 is far
# past any real deployment and still cheap on both counts.
_MAX_OPERATOR_ZONES = 200


class ZoneLimitError(Exception):
    """More drawn zones than the loop and the map payload should carry."""


# The UI server is threaded, and apply_location_zone is a read-modify-write of
# one file. Without this two POSTs interleave and the later read wins: the
# other operator's zone is simply gone, with a 200 telling them it was saved.
_ZONES_LOCK = threading.Lock()


def stored_zone_level(zones_path, zid):
    """The level a drawn zone is already stored at, or None.

    Read separately from apply_location_zone so the validator can stay pure,
    and deliberately WITHOUT taking _ZONES_LOCK: the caller whose write
    depends on the answer -- validate_and_apply_location_zone -- already holds
    it, and the lock is not reentrant. Called on its own this is a snapshot,
    which is all a read-only caller can ask for."""
    raw = _read_json_file(zones_path)
    zones = raw.get("zones") if isinstance(raw, dict) else None
    if not isinstance(zones, list):
        return None
    for z in zones:
        if not isinstance(z, dict) or z.get("id") != zid:
            continue
        level = z.get("level")
        if isinstance(level, bool) or not isinstance(level, int) or level < 0:
            return None
        return level
    return None


def _zone_watermark(zones, stored_next):
    """The lowest zone number that has never been handed out.

    Stored in the file as `next_id`, because deriving it from the zones alone
    reuses an id the moment the highest zone is deleted — and a stale editor
    panel still holding that id would then save over a DIFFERENT zone. The
    stored value is only ever a floor: an id already in the file always wins,
    so a hand-edited or missing counter cannot cause a collision either."""
    highest = 0
    for z in zones:
        zid = z.get("id") if isinstance(z, dict) else None
        if isinstance(zid, str) and _ZID_RE.match(zid):
            highest = max(highest, int(zid[1:]))
    if isinstance(stored_next, bool) or not isinstance(stored_next, int):
        stored_next = 0
    return max(stored_next, highest + 1)


def apply_location_zone(zones_path: str, zone: dict):
    """Apply one create / update / delete to the operator zone file; atomic
    write; returns the resulting list, or None when the id it was told to
    change is not in the file.

    Fail-open on the READ, exactly as location_fec is: an unreadable or
    corrupt file is treated as no zones, so a botched hand-edit costs the
    zones drawn so far rather than the operator's ability to draw new ones.
    NOT fail-open on the write — OSError propagates so the handler answers
    500 instead of telling the operator a floor was saved that was not.
    Mirrors apply_station_label, which takes the same treatment under its own
    lock. A caller that also needs the zone VALIDATED against the file this is
    about to replace goes through validate_and_apply_location_zone, which
    holds the lock across both; this entry point locks the write alone."""
    with _ZONES_LOCK:
        return _apply_location_zone_locked(zones_path, zone)


def _apply_location_zone_locked(zones_path: str, zone: dict):
    raw = _read_json_file(zones_path)
    existing = raw.get("zones") if isinstance(raw, dict) else None
    zones = ([z for z in existing if isinstance(z, dict)]
             if isinstance(existing, list) else [])
    # Taken BEFORE the change, so a delete can never lower it.
    watermark = _zone_watermark(zones, (raw or {}).get("next_id")
                                if isinstance(raw, dict) else None)
    zid = zone.get("id")
    if zone.get("delete"):
        kept = [z for z in zones if z.get("id") != zid]
        if len(kept) == len(zones):
            return None
        zones = kept
    elif zid:
        for i, z in enumerate(zones):
            if z.get("id") == zid:
                zones[i] = dict(zone)
                break
        else:
            return None
    else:
        if len(zones) >= _MAX_OPERATOR_ZONES:
            # Only a CREATE is refused: an update or a delete is the way back
            # under the cap, and refusing those would trap the operator above
            # it with no way down from the map.
            raise ZoneLimitError("too many zones (max %d)"
                                 % _MAX_OPERATOR_ZONES)
        zones = zones + [dict(zone, id="z%d" % watermark)]
        watermark += 1
    p = Path(zones_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # next_id rides out on every write, delete included: that is the whole
    # point of persisting it.
    _atomic_write_text(zones_path, _json.dumps(
        {"zones": zones, "next_id": watermark}, indent=2))
    return zones


def validate_and_apply_location_zone(zones_path, payload, wan_names,
                                     table_len):
    """Validate one POSTed zone against the file it is about to change, then
    apply it. Returns (ok, zone, zones, err), where `zones` is the resulting
    list -- or None when the id it was told to change is not in the file.

    The look-up, the validation and the write are ONE critical section on
    purpose. `keep_level` is read from this same file, so deciding outside the
    lock decides against a copy the write will not see, and that race runs
    both ways: a save refused for a level that changed underneath the operator
    (an honest refusal), but also a save ACCEPTED for a level the zone no
    longer has, because a racing lower landed after the look-up -- putting an
    off-table floor back on a live vehicle with both requests answered 200.
    Under one acquisition the decision and the write describe one file.

    Only local file I/O runs under the lock: no network, no nested
    acquisition, nothing that can park another operator's POST behind it."""
    with _ZONES_LOCK:
        zid = payload.get("id") if isinstance(payload, dict) else None
        # Only an update can carry a kept level; a create has no id to look up.
        keep = (stored_zone_level(zones_path, zid)
                if isinstance(zid, str) and _ZID_RE.match(zid) else None)
        ok, zone, err = validate_zone_payload(payload, wan_names, table_len,
                                              keep_level=keep)
        if not ok:
            return (False, None, None, err)
        return (True, zone, _apply_location_zone_locked(zones_path, zone),
                None)


def active_fec_table(fec_cfg, published_state_path, snap=None):
    """The loss table of the profile currently driving FEC.

    A zone's `level` is an index into THIS table, so the endpoint that accepts
    a level and the payload that tells the page what each level means have to
    resolve it the same way — otherwise the map offers a rung the daemon would
    clamp. Degrades to the base table: with no FEC section, or no driver named
    in the snapshot, that is the table sbfd-ctl would use anyway.

    `snap` lets a caller that has already read the published state pass it in,
    so the table and anything else derived from that file describe the SAME
    tick rather than two reads either side of a publish."""
    if fec_cfg is None:
        return list(fec_control.DEFAULT_LOSS_TABLE)
    if snap is None:
        snap = _read_json_file(published_state_path)
    fec = snap.get("fec") if isinstance(snap, dict) else None
    profile = fec.get("profile") if isinstance(fec, dict) else None
    driver = profile.get("driver_wan") if isinstance(profile, dict) else None
    return resolve_fec_profile(fec_cfg,
                               driver if isinstance(driver, str) else None)[1]


# Parsed tile store, keyed by (path, mtime_ns, size, inode). The map polls every 3s
# and the store changes at walking pace, so re-reading and re-validating it per
# request is pure waste — and a malformed store logged its "dropped N malformed
# entries" warning on every one of those polls.
_STORE_MEMO = {"path": None, "stat": None, "store": None}


def _map_zone_rows(zones_path, source):
    """The drawable rows of one zone file, tagged with where they came from.

    Both files hold the same shape, and both are read the same defensive way:
    the config one is hand-edited and the operator one is written by an
    endpoint whose atomic write can still be interrupted. `source` says where
    a row came from and `editable` whether this page may change it: a config
    zone ships with the box, and an operator row with no usable id cannot be
    addressed by the endpoint. Both are still DRAWN — a zone the daemon is
    steering parity with must never be missing from the map."""
    out = []
    raw = _read_json_file(zones_path)
    zones = raw.get("zones") if isinstance(raw, dict) else None
    if not isinstance(zones, list):
        return out
    for z in zones:
        if not isinstance(z, dict):
            continue
        # `wans` reaches the page as `z.wans.join(", ")`; anything but a
        # list of names is emitted as null, which the page already handles.
        names = z.get("wans")
        if not (isinstance(names, list)
                and all(isinstance(w, str) for w in names)):
            names = None
        try:
            # Geometry is the one thing worth dropping a row over: without a
            # position and a radius there is no circle to draw at all.
            lat, lon = float(z["lat"]), float(z["lon"])
            radius, level = float(z["radius_m"]), int(z["level"])
        except (KeyError, TypeError, ValueError):
            continue
        # NaN and inf ARE floats, so they clear the conversion above, and
        # json.dumps writes them as the bare tokens NaN/Infinity -- which
        # JSON.parse rejects. One poisoned zone would cost the page the whole
        # map payload, not just its own circle. Same class as the tile
        # residual guard below.
        if not (math.isfinite(lat) and math.isfinite(lon)
                and math.isfinite(radius)):
            continue
        row = {"label": str(z.get("label") or "zone"),
               "lat": lat, "lon": lon, "radius_m": radius, "level": level,
               "wans": names, "source": source, "editable": False}
        if source == "operator":
            row["suppress_learned"] = bool(z.get("suppress_learned", False))
            zid = z.get("id")
            # No id, no EDIT — but still a draw. location_fec.validate_zone
            # never asked for an id, so a hand-written row without one is live
            # in the daemon; leaving it off the map would hide a zone that is
            # actually steering parity. The page says so instead.
            if isinstance(zid, str) and _ZID_RE.match(zid):
                row["id"] = zid
                row["editable"] = True
        out.append(row)
    return out


def map_location_layer(store_path, zones_path, fix, max_tiles,
                       operator_zones_path=None):
    """Learned tiles (nearest the fix first, capped) and zones for the map,
    from both zone sources. Degrades to empty lists: a broken source must
    never 500 the endpoint. Lazy import keeps the failover daemon free of a
    hard dependency on the location module."""
    import tile_store
    import station_tracker
    out = {"tiles": [], "zones": []}
    try:
        st = os.stat(store_path)
        # The inode is in the key because mtime_ns + size alone cannot tell a
        # rewrite that reproduces both from no change at all; the writer's
        # tmp + os.replace always lands a new one.
        stat_key = (st.st_mtime_ns, st.st_size, st.st_ino)
    except (OSError, ValueError):
        # No store, unreadable, or an unusable path (os.stat raises ValueError,
        # not OSError, on an embedded NUL — the read below used to absorb that
        # one): an empty layer, and nothing to memoize.
        _STORE_MEMO.update({"path": None, "stat": None, "store": None})
        stat_key = None
    if stat_key is not None and (_STORE_MEMO["path"] == store_path
                                 and _STORE_MEMO["stat"] == stat_key):
        store = _STORE_MEMO["store"]
    elif stat_key is None:
        store = tile_store.TileStore()
    else:
        raw = _read_json_file(store_path)
        # Parse through the store's OWN validator rather than passing per-WAN
        # entries through raw: it drops malformed entries and coerces the
        # numbers, and it never raises. The map page does arithmetic on these
        # values (`(v.ewma_loss || 0).toFixed(1)`), so a string here is a
        # client-side exception, not a cosmetic wart. from_dict on a non-dict
        # logs and starts empty, so only call it when there is something to
        # parse.
        store = (tile_store.TileStore.from_dict(raw) if isinstance(raw, dict)
                 else tile_store.TileStore())
        _STORE_MEMO.update({"path": store_path, "stat": stat_key,
                            "store": store})
    residual = store.residual
    rows = []
    for tid, per_wan in store.tiles.items():
        try:
            lat, lon = tile_store.center(tid)
            box = list(tile_store.bbox(tid))
        except ValueError:
            continue
        dist = (station_tracker.haversine_m(fix[0], fix[1], lat, lon)
                if fix else 0.0)
        r = residual.get(tid)
        res = r.get("ewma") if isinstance(r, dict) else None
        # NaN and inf ARE floats, and json.dumps writes them as bare
        # NaN/Infinity tokens — JSON.parse throws on those, so one poisoned
        # tile would cost the page the entire payload.
        if (isinstance(res, bool) or not isinstance(res, (int, float))
                or not math.isfinite(res)):
            res = None
        rows.append((dist, {
            "id": tid, "bbox": box, "wans": per_wan, "residual": res}))
    rows.sort(key=lambda r: r[0])
    out["tiles"] = [r[1] for r in rows[:max_tiles]]
    out["zones"] = _map_zone_rows(zones_path, "config")
    if operator_zones_path:
        out["zones"] += _map_zone_rows(operator_zones_path, "operator")
    return out


def map_fec_levels(table):
    """What each level on this table costs, for the editor's level list.

    A level is meaningless to an operator on its own — it is an index into a
    table they cannot see. Degrades per row rather than raising: a display
    helper on a hand-written table must not be the thing that 500s /api/map."""
    out = []
    for i, row in enumerate(table or ()):
        ratio = row.get("fec") if isinstance(row, dict) else None
        pct = None
        try:
            a, b = fec_control.parse_ratio(ratio)
            if fec_control.validate_ratio(a, b):
                pct = round(fec_control.ratio_overhead_pct(a, b), 1)
        except (AttributeError, TypeError, ValueError, ZeroDivisionError):
            pass
        out.append({"level": i,
                    "ratio": ratio if isinstance(ratio, str) else None,
                    "overhead_pct": pct})
    return out


def assemble_map_payload(map_cfg, published_state_path, fix, now,
                         fec_cfg=None) -> dict:
    """Aggregate every map data source; each degrades independently to
    null/empty — a broken source must never 500 the endpoint.

    `fec_cfg` is what lets the page explain a level to the operator drawing a
    zone. It is optional so a caller that only wants positions need not have
    one; without it the level keys are present but empty, never absent."""
    st = _read_json_file(map_cfg["stations_path"]) or {}
    labels = _read_json_file(map_cfg["labels_path"])
    labels = labels if isinstance(labels, dict) else {}
    stations = []
    for sid, s in (st.get("stations") or {}).items():
        stations.append({"id": sid, "lat": s.get("lat"), "lon": s.get("lon"),
                         "visits": s.get("visits", 0),
                         "last_visit": s.get("last_visit"),
                         "label": labels.get(sid)})
    snap = _read_json_file(published_state_path) or {}
    out_fix = None
    if fix is not None:
        lat, lon, speed, track = fix[0], fix[1], fix[2], fix[3]
        fix_ts = fix[4] if len(fix) > 4 else None
        age = round(now - fix_ts, 1) if fix_ts else None
        out_fix = {"lat": lat, "lon": lon, "speed": speed,
                   "track": track, "age_s": age}
    try:
        # Clamped at 0: rows[:-5] is a negative slice, which drops the five
        # NEAREST tiles and keeps everything else — the opposite of a cap.
        max_tiles = max(0, int(map_cfg.get("max_location_tiles", 2000)))
    except (TypeError, ValueError):
        max_tiles = 2000
    location = map_location_layer(
        map_cfg["location_store_path"], map_cfg["location_config_path"],
        (fix[0], fix[1]) if fix is not None else None, max_tiles,
        operator_zones_path=map_cfg.get("location_zones_path"))
    # What a level MEANS on the link currently driving FEC. The editor greys
    # out the levels at or below the floor, so a floor the page cannot see
    # would have it offering rungs that change nothing.
    table = (active_fec_table(fec_cfg, published_state_path, snap=snap)
             if fec_cfg else None)
    fec_snap = snap.get("fec") if isinstance(snap.get("fec"), dict) else {}
    wan_labels = snap.get("wan_labels")
    location["levels"] = map_fec_levels(table) if table else []
    location["floor_level"] = (
        fec_control.ratio_rung(fec_snap.get("floor_ratio"), table)
        if table else None)
    location["wans"] = (wan_labels
                        if table and isinstance(wan_labels, dict) else {})
    return {"ts": now,
            "fix": out_fix,
            "stations": stations,
            "predictions": predict_from_stations(st),
            "environ": _read_json_file(map_cfg["environ_points_path"]),
            "mode": snap.get("mode"),
            "active": snap.get("active_wans"),
            "location_fec": location}


_GPS_MEMO = {"ts": 0.0, "fix": None}


def get_map_fix(host, port):
    """Fresh gpsd fix for the map, memoized 2s. Lazy import: the failover
    daemon has no hard dependency on the environ module."""
    now = time.time()
    if now - _GPS_MEMO["ts"] < 2.0:
        return _GPS_MEMO["fix"]
    fix = None
    try:
        import environ_ctl
        # quiet=True: the map polls every 3s, so a down gpsd would otherwise
        # write a WARNING per poll for as long as a page stays open.
        fix = environ_ctl.get_fix(host, port, timeout=1.5, quiet=True)
    except Exception as e:  # noqa: BLE001 - map shows "no gps" instead
        logging.debug("map gpsd read failed: %s", e)
    _GPS_MEMO["ts"] = now
    _GPS_MEMO["fix"] = fix
    return fix


_VENDOR_ASSETS = {
    "vendor/leaflet.js": "application/javascript",
    "vendor/leaflet.css": "text/css; charset=utf-8",
    "vendor/images/marker-icon.png": "image/png",
    "vendor/images/marker-icon-2x.png": "image/png",
    "vendor/images/marker-shadow.png": "image/png",
    "vendor/images/layers.png": "image/png",
    "vendor/images/layers-2x.png": "image/png",
}

_TILE_RE = re.compile(r"^/tiles/([0-9]{1,2})/([0-9]+)/([0-9]+)\.png$")
_TILE_UA = "PathFuse-map/1.0 (+https://github.com/QuickOK/PathFuse)"
_tile_store_count = 0


def tile_valid(z: int, x: int, y: int, max_zoom: int) -> bool:
    return 0 <= z <= max_zoom and 0 <= x < 2 ** z and 0 <= y < 2 ** z


def tile_cache_file(cache_dir: str, z: int, x: int, y: int) -> Path:
    return Path(cache_dir) / str(z) / str(x) / f"{y}.png"


def fetch_tile(z: int, x: int, y: int, timeout: float = 4.0):
    """One OSM tile, or None on any failure (offline -> cache-only mode)."""
    url = f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    req = urllib.request.Request(url, headers={"User-Agent": _TILE_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:  # noqa: BLE001 - offline is a supported state
        logging.debug("tile fetch %s/%s/%s failed: %s", z, x, y, e)
        return None


def evict_tiles(cache_dir: str, max_mb: int) -> int:
    """Delete oldest-mtime tiles until the cache fits max_mb. Returns count."""
    # st_mtime_ns: float st_mtime loses sub-microsecond ordering, which can
    # evict the newest tile on rapid writes (ties break by directory order)
    files = sorted(Path(cache_dir).rglob("*.png"),
                   key=lambda f: f.stat().st_mtime_ns)
    total = sum(f.stat().st_size for f in files)
    budget = max_mb * 1024 * 1024
    removed = 0
    for f in files:
        if total <= budget:
            break
        size = f.stat().st_size
        try:
            f.unlink()
            total -= size
            removed += 1
        except OSError:
            continue
    return removed


def store_tile(cache_dir, z, x, y, data, max_mb) -> None:
    """Atomic write; every ~200 stores, amortized LRU eviction."""
    global _tile_store_count
    p = tile_cache_file(cache_dir, z, x, y)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(p)
    except OSError as e:
        logging.debug("tile store failed: %s", e)
        return
    _tile_store_count += 1
    if _tile_store_count % 200 == 0:
        evict_tiles(cache_dir, max_mb)


def start_ui_server(cfg: Config, stop_event: threading.Event, fec_hist=None):
    """Bind the UI HTTP server (returns the bound httpd; caller doesn't need it)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    ui_dir = Path(__file__).resolve().parent / "ui"
    deployed_ui_dir = Path("/opt/sbfd-ctl/ui")
    if deployed_ui_dir.exists():
        ui_dir = deployed_ui_dir

    wan_names = set(cfg.wans.keys())
    map_cfg = resolve_map_cfg(cfg.map)

    class Handler(BaseHTTPRequestHandler):
        # The status page polls /api/state at 1 Hz and /api/engarde at 0.5 Hz
        # for as long as it is open, and the map page adds /api/map at 3s. On
        # HTTP/1.0 each of those was a fresh TCP connection. Keep-alive is safe
        # here because every response path sets an accurate Content-Length
        # (_send_json, _send_static, _serve_tile, the inline /api/state write),
        # and send_error sets its own plus `Connection: close` -- which is what
        # stops the 404/413 paths that answer a POST WITHOUT reading its body
        # from leaving that body to be parsed as the next request.
        protocol_version = "HTTP/1.1"
        # A browser holds several connections open; each costs a thread for its
        # whole life under ThreadingHTTPServer. The default timeout is None (an
        # unbounded blocking read), so a client that vanishes without a FIN --
        # laptop off Wi-Fi -- would pin one forever. handle_one_request turns
        # this timeout into a close.
        timeout = 30
        # Without this, every keep-alive response pays ~40ms: the handler's
        # header and body writes are separate, Nagle holds the second, and the
        # browser sits on its delayed-ACK timer -- so a 1 Hz poller would be
        # slower than it was on HTTP/1.0. See sbfd.py's state listener.
        disable_nagle_algorithm = True

        def log_message(self, fmt, *args):
            logging.debug("ui %s - %s", self.address_string(), fmt % args)

        def _send_json(self, code: int, obj):
            body = _json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, name: str, ctype: str):
            path = ui_dir / name
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                self.send_error(404, f"missing UI asset {name}")
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def _serve_tile(self, z: int, x: int, y: int):
            tc = map_cfg["tile_cache"]
            if not tile_valid(z, x, y, tc["max_zoom"]):
                self.send_error(404)
                return
            p = tile_cache_file(tc["path"], z, x, y)
            if p.exists():
                data = p.read_bytes()
            else:
                data = fetch_tile(z, x, y)
                if data is not None:
                    store_tile(tc["path"], z, x, y, data, tc["max_mb"])
            if data is None:
                self.send_error(502, "tile unavailable (offline, not cached)")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send_static("index.html", "text/html; charset=utf-8")
            elif self.path == "/app.js":
                self._send_static("app.js", "application/javascript")
            elif self.path == "/wall.css":
                self._send_static("wall.css", "text/css; charset=utf-8")
            elif self.path in ("/map", "/map.html"):
                self._send_static("map.html", "text/html; charset=utf-8")
            elif self.path == "/map.js":
                self._send_static("map.js", "application/javascript")
            elif self.path.lstrip("/") in _VENDOR_ASSETS:
                name = self.path.lstrip("/")
                self._send_static(name, _VENDOR_ASSETS[name])
            elif self.path == "/api/state":
                try:
                    data = Path(cfg.published_state).read_text()
                except FileNotFoundError:
                    self._send_json(503, {"error": "state not yet published"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data.encode())))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data.encode())
            elif self.path == "/api/fec_history":
                self._send_json(200, {"samples": (fec_hist.snapshot()
                                                  if fec_hist else [])})
            elif self.path == "/api/engarde":
                url = cfg.engarde.admin_url or f"http://{cfg.engarde.server_ip}:8080/api/v1/get-list"
                try:
                    with urllib.request.urlopen(url, timeout=1.5) as resp:
                        body = resp.read()
                    payload = _json.loads(body.decode())
                    # wan_ifaces lets the UI tell a runtime-excluded WAN
                    # (standby) from a config-excluded non-WAN (hidden):
                    # engarde reports both identically in get-list.
                    self._send_json(200, {
                        "ok": True, "data": payload,
                        "wan_ifaces": sorted(w.iface for w in cfg.wans.values()),
                    })
                except urllib.error.HTTPError as e:
                    self._send_json(200, {"ok": False, "error": f"HTTP {e.code}: {e.reason}"})
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    self._send_json(200, {"ok": False, "error": f"transport: {e}"})
                except ValueError as e:
                    self._send_json(200, {"ok": False, "error": f"parse: {e}"})
            elif self.path == "/api/desired_egress":
                try:
                    snap = _json.loads(Path(cfg.published_state).read_text())
                except (FileNotFoundError, ValueError, OSError):
                    self._send_json(503, {"error": "state not yet published"})
                    return
                self._send_json(200, {
                    "mode": snap.get("egress_mode") or cfg.egress.default_mode,
                    "master_wan": snap.get("master_wan"),
                    "ts": snap.get("ts"),
                })
            elif self.path == "/api/map":
                g = map_cfg["gpsd"]
                fix = get_map_fix(g["host"], g["port"])
                self._send_json(200, assemble_map_payload(
                    map_cfg, cfg.published_state, fix, time.time(),
                    fec_cfg=cfg.fec))
            elif (m := _TILE_RE.match(self.path)):
                self._serve_tile(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            else:
                self.send_error(404)

        def _body_length(self):
            """The request body's length, or None if the header is unusable.

            int() on a non-numeric header raised straight out of the handler,
            and a NEGATIVE length passed the size cap and then blocked in
            rfile.read(-1) until the peer closed -- one thread pinned per
            request on a keep-alive connection."""
            raw = self.headers.get("Content-Length", "0") or "0"
            try:
                n = int(raw)
            except (TypeError, ValueError):
                return None
            return n if n >= 0 else None

        def do_POST(self):
            if self.path == "/api/station-label":
                length = self._body_length()
                if length is None:
                    # send_error, not _send_json: with no usable length we
                    # cannot consume the body, so the connection must close or
                    # that body is parsed as the next request.
                    self.send_error(400, "invalid Content-Length"); return
                if length > 4096:
                    self.send_error(413, "payload too large"); return
                try:
                    payload = _json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._send_json(400, {"error": "invalid JSON"}); return
                ok, sid, label, err = validate_label(payload)
                if not ok:
                    self._send_json(400, {"error": err}); return
                try:
                    labels = apply_station_label(map_cfg["labels_path"], sid, label)
                except OSError as e:
                    self._send_json(500, {"error": f"persist failed: {e}"}); return
                self._send_json(200, {"ok": True, "labels": labels})
                return
            if self.path == "/api/location-zone":
                length = self._body_length()
                if length is None:
                    self.send_error(400, "invalid Content-Length"); return
                if length > 4096:
                    self.send_error(413, "payload too large"); return
                try:
                    payload = _json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._send_json(400, {"error": "invalid JSON"}); return
                # The level is an index into the table of whichever profile is
                # driving right now, so the bound is resolved per request.
                table_len = len(active_fec_table(cfg.fec, cfg.published_state))
                # ...except for a level the zone already has, which is kept
                # rather than snapped down onto a table that has since got
                # shorter. That look-up reads the zone file, so it happens
                # under the same lock as the write it justifies.
                try:
                    ok, zone, zones, err = validate_and_apply_location_zone(
                        map_cfg["location_zones_path"], payload, wan_names,
                        table_len)
                except ZoneLimitError as e:
                    self._send_json(400, {"error": str(e)}); return
                except OSError as e:
                    self._send_json(500, {"error": f"persist failed: {e}"}); return
                if not ok:
                    self._send_json(400, {"error": err}); return
                if zones is None:
                    self._send_json(400,
                                    {"error": f"unknown zone {zone['id']}"})
                    return
                self._send_json(200, {"ok": True, "zones": zones})
                return
            if self.path != "/api/runtime":
                self.send_error(404); return
            length = self._body_length()
            if length is None:
                self.send_error(400, "invalid Content-Length"); return
            if length > 4096:
                self.send_error(413, "payload too large"); return
            try:
                body = self.rfile.read(length)
                payload = _json.loads(body) if body else {}
            except ValueError:
                self._send_json(400, {"error": "invalid JSON"}); return

            ok, err = validate_runtime_payload(payload, wan_names)
            if not ok:
                self._send_json(400, {"error": err}); return

            ov = load_runtime_overlay(cfg)

            # Master-DOWN gate for local_direct. Best-effort (TOCTOU between
            # gate and write is acceptable; reconciler re-evaluates each tick).
            # Resolve desired master with the same precedence as effective_policy()
            # so the gate uses the same wan that will actually be applied.
            if payload.get("egress_mode") == "local_direct":
                payload_master = payload.get("master_wan")
                if payload_master in cfg.wans:
                    desired_master = payload_master
                else:
                    desired_master = effective_policy(cfg, ov)[2]
                if is_master_wan_down(cfg.published_state, desired_master):
                    self._send_json(409, {"error": "master_wan_down", "wan": desired_master})
                    return

            ov.mode = payload.get("mode", ov.mode) or cfg.policy.default_mode
            ov.master_policy = payload.get("master_policy", ov.master_policy) or cfg.policy.default_master_policy
            ov.master_wan = payload.get("master_wan", ov.master_wan) or cfg.policy.default_master_wan
            ov.persist = bool(payload.get("persist", ov.persist))
            ov.egress_mode = payload.get("egress_mode", ov.egress_mode) or cfg.egress.default_mode
            if "fec_enabled" in payload:
                ov.fec_enabled = payload["fec_enabled"]
                # Legacy clients use this; map into the new tri-state so the
                # rest of the controller sees a single source of truth.
                ov.fec_mode = (fec_control.MODE_ADAPTIVE if payload["fec_enabled"]
                               else fec_control.MODE_OFF)
            if "fec_mode" in payload:
                ov.fec_mode = payload["fec_mode"]
                # Keep the legacy flag in sync for downgrade safety.
                ov.fec_enabled = (payload["fec_mode"] != fec_control.MODE_OFF)
            if "fec_fixed_ratio" in payload:
                ov.fec_fixed_ratio = payload["fec_fixed_ratio"]
            if "fec_floor_ratio" in payload:
                ov.fec_floor_ratio = payload["fec_floor_ratio"]
            if "environmental_enabled" in payload:
                ov.environmental_enabled = payload["environmental_enabled"]
            if "location_fec_enabled" in payload:
                ov.location_fec_enabled = payload["location_fec_enabled"]
            # `in payload`, never `payload.get(k) or default`: hour 0 is midnight
            # and falsy, and the `or` idiom would rewrite it to the config default.
            if "maintenance_enabled" in payload:
                ov.maintenance_enabled = payload["maintenance_enabled"]
            if "maintenance_hour" in payload:
                ov.maintenance_hour = payload["maintenance_hour"]
            ov.set_by = "ui"
            ov.set_ts = time.time()
            save_runtime_overlay(cfg, ov)

            self._send_json(200, {"ok": True, "applied": payload})

    host_str, port_str = cfg.ui_listen.rsplit(":", 1)
    httpd = ThreadingHTTPServer((host_str, int(port_str)), Handler)
    t = threading.Thread(target=httpd.serve_forever, name="ui-http", daemon=True)
    t.start()
    logging.info("UI listening on %s", cfg.ui_listen)
    return httpd


# -- Main controller loop ----------------------------------------------------

def run_controller(cfg: Config, stop_event=None, wire_tracker=None, fec_hist=None):
    sid_to_wan = {w.session_id: name for name, w in cfg.wans.items()}

    apply_nft_init(cfg)
    currently_active = set(cfg.wans.keys())
    master_up_since: Optional[float] = None
    dynamic_master_current: Optional[str] = None
    dynamic_candidate: Optional[str] = None
    dynamic_candidate_since: Optional[float] = None
    last_remote = StateSnapshot(ok=False, per_wan={}, error="not yet fetched")
    last_remote_at = 0.0
    last_decision_reason = "init"
    last_switch = {"ts": time.time(), "from": list(sorted(currently_active)),
                   "to": list(sorted(currently_active)), "reason": "boot"}
    recent_switches: deque = deque([last_switch], maxlen=20)

    tick = 0.5
    remote_interval = max(0.5, cfg.relay.fetch_interval_s)

    fec_rt = fec_control.FecRuntime(current_level=0, up_streak=0, last_change_ts=time.time())
    fec_profile_active = "default"
    # The table `fec_rt`'s level is indexed against. Tracked alongside the
    # profile name because a swap has to re-base the level from the OLD table.
    fec_profile_table = cfg.fec.loss_table if cfg.fec else None
    # Sticky-driver state (see fec_driver_pick).
    fec_driver_current = None
    fec_driver_candidate = None
    fec_driver_candidate_since = None
    # Last tick's active set, so a WAN that has just joined can be told apart
    # from one that has been racing all along. None on the first tick: no
    # membership is known yet, so nobody counts as a newcomer.
    fec_driver_active_prev = None
    fec_signal_floor = fec_control.SignalFloor(fec_control.SignalThresholds(
        rsrq_degrade_db=cfg.cell.rsrq_degrade_db,
        rsrq_recover_db=cfg.cell.rsrq_recover_db,
        rsrp_degrade_dbm=cfg.cell.rsrp_degrade_dbm,
        rsrp_recover_dbm=cfg.cell.rsrp_recover_dbm)
        if cfg.cell else None)
    fec_signal_engaged = False
    fec_signal_floor_applied = False
    fec_location_level_prev = 0
    fec_current_ratio = None
    fec_ratio_since: Optional[float] = None
    fec_relay_last = {"ok": False, "data": None, "error": "not yet fetched"}
    fec_relay_last_at = 0.0
    fec_relay_last_acked = None
    fec_relay_last_post_ts = None

    handoff_was_active = False
    duplication_count = 0
    duplication_last_ts = None
    duplication_last_reason = None

    notifier = None
    detector = None
    if cfg.notifications is not None:
        notifier = notify.Notifier(cfg.notifications.topic,
                                   min_interval_s=cfg.notifications.min_interval_s,
                                   command=cfg.notifications.command)
        notifier.start()
        notifier.notify(notify.Event(
            "started", "▶️ sbfd-ctl started",
            f"wans: {', '.join(cfg.wans)}", "low"))
        # ~10s of failed relay polls before alerting, at the actual poll cadence.
        detector = notify.EventDetector(
            relay_fail_threshold=max(1, round(10.0 / remote_interval)),
            wan_down_hold_s=cfg.notifications.wan_down_hold_s,
            switch_hold_s=cfg.notifications.switch_hold_s,
            fec_alerts=cfg.notifications.fec_alerts)

    if stop_event is None:
        stop_event = threading.Event()

    while not stop_event.is_set():
        loop_start = time.time()
        relay_polled = False
        switch_event = None
        ov = load_runtime_overlay(cfg)
        mode, policy, master_wan, egress_mode = effective_policy(cfg, ov)
        env_auto = load_auto_override(cfg, loop_start)
        cell_sample = load_cell_sample(cfg, loop_start)
        location_floors = load_location_floor(cfg, loop_start)
        location_enabled = effective_location_fec_enabled(cfg, ov)
        env_enabled = effective_environmental_enabled(cfg, ov)
        mode, env_active = apply_auto_override(mode, env_enabled, env_auto)
        handoff_win = load_cell_handoff(cfg, loop_start)
        mode, handoff_active = apply_handoff_window(
            mode, handoff_win, cfg.cell.wan if cfg.cell else None,
            currently_active)
        if handoff_active and not handoff_was_active:
            duplication_count += 1
            duplication_last_ts = loop_start
            duplication_last_reason = handoff_active.reason
            logging.info("duplication window OPEN (%s, until %.1fs)",
                         handoff_active.reason,
                         handoff_active.until_ts - loop_start)
        elif handoff_was_active and not handoff_active:
            logging.info("duplication window closed")
        handoff_was_active = bool(handoff_active)
        maint_enabled = effective_maintenance_enabled(cfg, ov)
        maint_hour = effective_maintenance_hour(cfg, ov)
        # loop_start is time.time() — a wall clock, which is what the window's
        # `until` epoch must be judged against.
        maint_window = load_maintenance_window(cfg, loop_start)

        local = read_local_sbfd_state(cfg.sbfd_local_state, sid_to_wan)
        fec_reconcile_due = False
        if loop_start - last_remote_at >= remote_interval:
            last_remote = fetch_remote_sbfd_state(
                cfg.relay.state_url, cfg.relay.fetch_timeout_s, sid_to_wan)
            last_remote_at = loop_start
            if cfg.fec:
                fec_relay_last = fetch_relay_fec(cfg.relay.fec_url, cfg.relay.fetch_timeout_s)
                fec_relay_last_at = loop_start
                fec_reconcile_due = True
            relay_polled = True

        eff = merge_effective(local, last_remote)

        rtt = {}
        loss = {}
        for w in cfg.wans:
            sample = local.per_wan.get(w) if local.ok else None
            rtt[w] = sample.rtt_ms if (sample and sample.rtt_ms is not None) else float("inf")
            loss[w] = sample.loss_pct if (sample and sample.loss_pct is not None) else 0.0

        out = decide(cfg, DecideInput(
            mode=mode, policy=policy, master_wan_cfg=master_wan,
            eff_state=eff, rtt_ms=rtt, loss_pct=loss,
            master_up_since=master_up_since,
            currently_active=currently_active, now=loop_start,
            dynamic_master_current=dynamic_master_current,
            dynamic_candidate=dynamic_candidate,
            dynamic_candidate_since=dynamic_candidate_since,
        ))
        master_up_since = out.master_up_since
        if policy == "dynamic":
            dynamic_master_current = out.dynamic_master_current
            dynamic_candidate = out.dynamic_candidate
            dynamic_candidate_since = out.dynamic_candidate_since
        else:
            dynamic_master_current = None
            dynamic_candidate = None
            dynamic_candidate_since = None

        # Single source of truth for "which WAN does box-originated traffic
        # egress" — shared by the kernel default route and the local_direct
        # engarde anchor. Follows the policy-resolved master and the actually
        # active WAN after failover, not static config (CR #11 / CR #14).
        # None when all WANs are DOWN (callers no-op rather than black-hole).
        egress_master = pick_default_wan(eff, out.desired_active,
                                         out.effective_master, dynamic_master_current)

        if out.desired_active != currently_active:
            current_drops = list_current_drops(cfg)
            actions = compute_nft_diff(cfg, out.desired_active, current_drops)
            try:
                apply_nft_diff(cfg, actions)
                last_switch = {
                    "ts": loop_start,
                    "from": sorted(currently_active),
                    "to": sorted(out.desired_active),
                    "reason": out.reason,
                }
                recent_switches.append(last_switch)
                switch_event = (last_switch["from"], last_switch["to"],
                                last_switch["reason"])
                logging.info("switch: %s -> %s (%s)",
                             sorted(currently_active), sorted(out.desired_active), out.reason)
                currently_active = set(out.desired_active)
            except RuntimeError as e:
                logging.error("nft apply failed: %s", e)

        # Keep engarde's runtime exclusions matched to the blocked WAN set
        # every tick (engarde restarts forget them; see sync_engarde_exclusions).
        sync_engarde_exclusions(cfg, out.desired_active)

        managed_default = None
        if cfg.policy.manage_default_route:
            chosen = egress_master
            chosen_iface = cfg.wans[chosen].iface if chosen else None
            chosen_gw = read_wan_gateway(chosen_iface) if chosen_iface else None
            current_managed = read_managed_default()
            if chosen is None and current_managed is not None:
                # No WAN pickable (all DOWN/UNKNOWN): preserve the last-good
                # default route rather than delete it (deleting our sole default
                # during a transient both-down blip caused episodic outages).
                logging.warning("no WAN pickable; preserving managed default %s",
                                current_managed)
            action = compute_route_action(chosen_iface, chosen_gw, current_managed)
            if action is not None:
                try:
                    apply_route_action(action)
                    logging.info("default route: %s %s via %s",
                                 action[0], action[1], action[2])
                except RuntimeError as e:
                    logging.error("default route apply failed: %s", e)
            # Re-read post-apply for honest publishing.
            managed_default = read_managed_default()

        # Engarde-table reconciliation (egress_mode actuator). local_direct
        # anchors on the active egress WAN, not the static configured master,
        # so it never pins traffic to a failed-over (DOWN) WAN (CR #14).
        engarde_master_iface = cfg.wans[egress_master].iface if (egress_mode == "local_direct" and egress_master in cfg.wans) else None
        engarde_master_gw = read_wan_gateway(engarde_master_iface) if engarde_master_iface else None
        engarde_current = read_engarde_table_default(cfg.egress.engarde_table)
        engarde_action = compute_engarde_table_action(
            egress_mode=egress_mode,
            master_iface=engarde_master_iface,
            master_gw=engarde_master_gw,
            current=engarde_current,
            cfg=cfg.egress,
        )
        if engarde_action is not None:
            try:
                apply_engarde_table_action(engarde_action)
                logging.info("engarde-table: replace via=%s dev=%s table=%s",
                             engarde_action.get("via"), engarde_action["dev"],
                             engarde_action["table"])
            except RuntimeError as e:
                logging.error("engarde-table apply failed: %s", e)
        engarde_after = read_engarde_table_default(cfg.egress.engarde_table)

        last_decision_reason = out.reason

        fec_mode_eff = effective_fec_mode(cfg, ov)
        fec_fixed_ratio_eff = effective_fec_fixed_ratio(cfg, ov)
        fec_floor_ratio_eff = effective_fec_floor_ratio(cfg, ov)
        fec_desired = fec_mode_eff != fec_control.MODE_OFF
        fec_driver = None
        fec_driver_display = None
        fec_actuator_ok = True
        fec_loss = loss
        fec_loss_source = "local"
        # None until the profile resolves below: with cfg.fec absent there is no
        # active loss table, and the UI must fall back rather than be handed a
        # ladder derived from the wrong one.
        fec_ladder = None
        fec_ladder_r2c = None
        fec_location_level, fec_location_reason = 0, ""
        # Whether the floor changed the ratio this tick actually sends. With no
        # FEC config there is no ratio to change.
        fec_location_applied = False
        relay_desired = (fec_mode_eff, fec_fixed_ratio_eff, fec_floor_ratio_eff)
        if cfg.fec:
            # Our TX leg repairs client->relay loss, which only the relay can
            # measure — drive it from the fetched relay snapshot (local loss,
            # the opposite direction, is the stale-relay fallback).
            remote_fresh = bool(last_remote_at) and (
                loop_start - last_remote_at) <= max(3 * remote_interval, 10.0)
            fec_loss, fec_loss_source = fec_loss_map(
                local, last_remote, remote_fresh, cfg.wans)
            # Profile first: the driver WAN picks table/hysteresis/floor, so it
            # must be known before the adaptive engine steps.
            (fec_driver, fec_driver_candidate,
             fec_driver_candidate_since) = fec_driver_pick(
                fec_loss, currently_active, fec_driver_current,
                fec_driver_candidate, fec_driver_candidate_since,
                cfg.fec.driver_dwell_s, loop_start,
                prev_active=fec_driver_active_prev)
            fec_driver_active_prev = set(currently_active or ())
            if fec_driver != fec_driver_current:
                logging.info("fec driver -> %s (was %s)",
                             fec_driver, fec_driver_current)
            fec_driver_current = fec_driver
            # The client_to_relay direction's driver_wan/driving_loss_pct
            # predate profiles and only mean something in a mode where the
            # adaptive engine actually picks the ratio — off/fixed ignore
            # loss entirely, so keep publishing None there (unchanged
            # contract), even though fec_driver itself must now resolve
            # every tick to feed the profile machinery below.
            fec_driver_display = (fec_driver if fec_mode_eff in
                                  (fec_control.MODE_ADAPTIVE,
                                   fec_control.MODE_MIN_ADAPTIVE) else None)
            (prof_name, prof_table, prof_hyst,
             prof_floor, prof_sf_fec) = resolve_fec_profile(cfg.fec, fec_driver)
            if prof_name != fec_profile_active:
                fec_rt = fec_reseed_runtime(fec_rt, fec_profile_table,
                                            prof_table, loop_start)
                logging.info("fec profile -> %s (driver=%s)", prof_name, fec_driver)
                fec_profile_active = prof_name
                fec_profile_table = prof_table
            fec_floor_ratio_eff = effective_fec_floor_ratio(
                cfg, ov, profile_floor=prof_floor)
            # Signal floor: only for a profiled driver that IS the telemetry
            # WAN, and only on a fresh sample — anything else disengages
            # (fail-open; measured loss remains the ground truth).
            rsrq = rsrp = None
            if (prof_name != "default" and cfg.cell is not None
                    and fec_driver == cfg.cell.wan and cell_sample is not None
                    and (loop_start - cell_sample["set_ts"]) <= cfg.cell.stale_after_s):
                rsrq, rsrp = cell_sample.get("rsrq"), cell_sample.get("rsrp")
            prev_engaged = fec_signal_engaged
            fec_signal_engaged = fec_signal_floor.update(rsrq, rsrp)
            if fec_signal_engaged != prev_engaged:
                logging.info("fec signal floor %s (rsrq=%s rsrp=%s)",
                             "engaged" if fec_signal_engaged else "released",
                             rsrq, rsrp)
            # Full-redundancy backoff suppresses only the SIGNAL floor (the
            # expensive pre-emptive tier): engarde is already duplicating over
            # both WANs, so RSRQ-triggered parity would be waste. The
            # min_adaptive profile/config floor is deliberately KEPT — full
            # mode engages at reliability-critical moments (hazard override,
            # handoff windows, both-down fallback), and floor parity is what
            # recovers packets dropped on both links at once, which
            # duplication alone cannot. So apply_mode's floor re-raises the
            # backed-off adaptive level; full_mode_backoff_fec cannot
            # undercut a configured floor. Mirrors mode_aware_level's
            # up-count semantics exactly (sum over `eff`, not the active set).
            fec_full_backoff = (
                mode == "full"
                and sum(1 for st in eff.values() if st == "UP")
                >= cfg.fec.full_min_up_wans)
            fec_signal_floor_applied = fec_signal_engaged and not fec_full_backoff
            # The adaptive engine always runs so the loss-tracked level stays
            # fresh; apply_mode then maps it to the actual ratio per mode.
            _fec_target = compute_fec_target(cfg.fec, mode, eff, fec_loss,
                                             currently_active,
                                             loss_table=prof_table)
            fec_rt, _fec_changed = fec_control.step_level(
                _fec_target, fec_rt, prof_hyst, loop_start)
            _fec_level = fec_control.apply_signal_floor(
                fec_rt.current_level, fec_signal_floor_applied, prof_table, prof_sf_fec)
            # Location floor: raise-only, driver WAN only, resolved against
            # the ACTIVE profile's table. Unlike the signal floor it is NOT
            # suppressed by full-redundancy backoff — a place known to drop
            # both links at once is exactly where parity beats duplication.
            fec_location_level, fec_location_reason = location_floor_for_driver(
                location_floors, location_enabled, fec_driver, prof_table)
            _level_before_location = _fec_level
            _fec_level = fec_control.apply_location_floor(
                _fec_level, fec_location_level, prof_table)
            if fec_location_level != fec_location_level_prev:
                logging.info("fec location floor -> level %d (%s)",
                             fec_location_level, fec_location_reason or "released")
                fec_location_level_prev = fec_location_level
            _adaptive_ratio = fec_control.level_to_ratio(_fec_level, prof_table)
            _fec_ratio = fec_control.apply_mode(
                fec_mode_eff, _adaptive_ratio,
                fixed_ratio=fec_fixed_ratio_eff,
                floor_ratio=fec_floor_ratio_eff)
            # The same ratio computed as if the location floor had said
            # nothing. Comparing the two is the only honest way to report
            # whether the floor changed the parity actually sent: a lifted
            # LEVEL can still land on the ratio the leg was already sending
            # (off/fixed ignore the level; a min_adaptive floor of 8:8 lifts
            # every level to the same rung).
            _ratio_without_location = fec_control.apply_mode(
                fec_mode_eff,
                fec_control.level_to_ratio(_level_before_location, prof_table),
                fixed_ratio=fec_fixed_ratio_eff,
                floor_ratio=fec_floor_ratio_eff)
            # Push our locally measured (relay->client) loss so the relay can
            # drive ITS leg on the direction it actually repairs. Quantized to
            # a table level in relay_desired so posts fire on level changes,
            # not every EWMA wiggle.
            client_push_loss = worst_active_loss(loss, currently_active)
            # The location level rides along as the last element: a change in
            # the place we are standing in must re-post at once, not wait out
            # the heartbeat, and shows as reconcile_pending until acked.
            relay_desired = (fec_mode_eff, fec_fixed_ratio_eff,
                             fec_floor_ratio_eff,
                             fec_control.loss_to_level(client_push_loss,
                                                       prof_table),
                             prof_name, fec_signal_floor_applied,
                             fec_location_level)
            if _fec_ratio != fec_current_ratio:
                fec_actuator_ok = fec_control.write_fifo(
                    cfg.fec.fifo, _fec_ratio, logging)
                if fec_actuator_ok:
                    fec_current_ratio = _fec_ratio
                    fec_ratio_since = loop_start
                    logging.info("fec ratio -> %s (fec_mode=%s mode=%s active=%s)",
                                 _fec_ratio, fec_mode_eff, mode,
                                 sorted(currently_active))
            # AFTER the write, and against what the actuator actually holds:
            # `active` claims parity is on the wire, so a refused FIFO write
            # must retract the claim — and a refusal that leaves an OLDER ratio
            # flowing must not earn the floor credit for it either. Same rule
            # the ladder below states for its dots: keyed on fec_current_ratio,
            # never on what we wanted.
            fec_location_applied = location_floor_active(
                fec_current_ratio, _fec_ratio, _ratio_without_location)

            # Ladder for the UI's pip row. Keyed on fec_current_ratio (what the
            # actuator accepted), not _fec_ratio (what we wanted): a failed FIFO
            # write must not light dots for parity that isn't on the wire.
            #
            # The scale spans every profile that could drive either leg, so a
            # position means one ratio no matter which profile is active — that
            # is what lets the shaded span identify the driver. Only the client
            # can build it: it knows every profile's table AND floor, while the
            # relay is pushed a single resolved floor and never sees our link
            # states. So we compute BOTH legs' views here.
            _relay_now = (fec_relay_last or {}).get("data") or {}
            fec_scale = fec_control.ladder_scale(
                fec_profile_candidates(cfg, ov), fec_mode_eff,
                fixed_ratio=fec_fixed_ratio_eff,
                # What the relay is applying RIGHT NOW, which across an
                # unacknowledged change our own settings would not produce.
                # Omitting it rounds that ratio down onto a lower rung, so the
                # card would understate parity the relay really is sending.
                extra=[r for r in (_relay_now.get("ratio"),
                                   _relay_now.get("fixed_ratio"))
                       if isinstance(r, str)])
            # Full-redundancy backoff pins the adaptive engine to one rung, so
            # the client leg's reachable span collapses to a single ratio. The
            # relay never applies this backoff (its run_once calls loss_to_level
            # directly), so its span stays the full floor-to-top range — the two
            # legs genuinely differ here, and the row now shows that.
            # fec_full_backoff tracks the ROUTING mode and up-count alone, but
            # backoff only holds anything when the adaptive engine is what
            # picks the ratio: 'off' and 'fixed' ignore the engine entirely, so
            # reporting them as pinned would put "· pinned" on a row nothing is
            # pinning.
            fec_pinned = fec_full_backoff and fec_mode_eff in (
                fec_control.MODE_ADAPTIVE, fec_control.MODE_MIN_ADAPTIVE)
            fec_pinned_level = (
                pinned_ladder_level(cfg.fec.full_mode_backoff_fec, prof_table,
                                    fec_location_level)
                if fec_pinned else None)
            fec_ladder = fec_control.ladder_view(
                fec_scale,
                fec_control.reachable_ratios(
                    fec_mode_eff, prof_table, fec_floor_ratio_eff,
                    fixed_ratio=fec_fixed_ratio_eff,
                    pinned_level=fec_pinned_level),
                fec_current_ratio, fec_floor_ratio_eff, fec_mode_eff,
                pinned=fec_pinned)
            # The relay leg's span is built entirely from the relay's OWN
            # reported settings (see relay_fec_direction). These are only the
            # shared scale and the tables it may name — deliberately NOT our
            # mode/floor/fixed, so there is nothing here to fall back to and
            # the card cannot assert a setting the relay never reported.
            fec_ladder_r2c = {
                "scale": fec_scale,
                "tables": {name: p.loss_table
                           for name, p in cfg.fec.wan_profiles.items()},
                "default_table": cfg.fec.loss_table,
            }

            # Reconcile desired mode/ratio to relay on the remote-fetch cadence
            # only (best-effort) — rate-limiting to the relay tick keeps a slow
            # or unreachable relay from blocking the 0.5s failover loop on
            # every tick.
            if fec_reconcile_due and should_post_fec(
                    relay_desired, fec_relay_last_acked,
                    fec_relay_last_post_ts, loop_start):
                if post_relay_fec(cfg.relay.fec_url, fec_mode_eff,
                                  fec_fixed_ratio_eff,
                                  fec_floor_ratio_eff,
                                  cfg.relay.fetch_timeout_s,
                                  client_loss_pct=client_push_loss,
                                  wan_profile=prof_name,
                                  signal_floor=fec_signal_floor_applied,
                                  location_level=fec_location_level):
                    fec_relay_last_acked = relay_desired
                fec_relay_last_post_ts = loop_start

        def _wan_obj(snap: StateSnapshot, w: str) -> dict:
            s = snap.per_wan.get(w) if snap.ok else None
            if not s:
                return {"state": "UNKNOWN", "rtt_ms": None, "loss_pct": None, "state_since": None}
            return {"state": s.state, "rtt_ms": s.rtt_ms, "loss_pct": s.loss_pct, "state_since": s.state_since}

        snapshot = {
            "ts": loop_start,
            "mode": mode,
            "master_policy": policy,
            "master_wan": master_wan,
            "egress_mode": egress_mode,
            # The UI renders its persist checkbox from this. Omit it and a
            # freshly loaded page shows the box unchecked, so the next Apply
            # posts persist=false and deletes the persisted overlay.
            "persist": bool(ov.persist),
            "effective": eff,
            "wan_labels": {w: cfg.wans[w].label for w in cfg.wans},
            "engarde_server": f"{cfg.engarde.server_ip}:{cfg.engarde.server_port}",
            "client_local": {w: _wan_obj(local, w) for w in cfg.wans},
            "relay_remote": {
                "states": {w: _wan_obj(last_remote, w) for w in cfg.wans},
                "fetched_at": last_remote.fetched_at,
                "stale_s": loop_start - last_remote_at if last_remote_at else None,
                "ok": last_remote.ok,
                "error": last_remote.error,
            },
            "active_wans": sorted(currently_active),
            "last_switch": last_switch,
            "recent_switches": list(recent_switches),
            "decision_reason": last_decision_reason,
            "failback_remaining_s": (
                max(0.0, cfg.policy.failback_hold_s - (loop_start - master_up_since))
                if (mode == "master_backup" and master_up_since
                    and currently_active != {master_wan if policy != "dynamic"
                                             else (dynamic_master_current
                                                   or _best_link(eff, rtt, loss, master_wan))})
                else 0.0
            ),
            "dynamic": {
                "master": dynamic_master_current,
                "candidate": dynamic_candidate,
                "candidate_since": dynamic_candidate_since,
                "swap_dwell_remaining_s": (
                    max(0.0, cfg.policy.dynamic_swap_dwell_s - (loop_start - dynamic_candidate_since))
                    if (policy == "dynamic" and dynamic_candidate and dynamic_candidate_since)
                    else 0.0
                ),
                "rtt_margin_ms": cfg.policy.dynamic_rtt_margin_ms,
                "loss_margin_pct": cfg.policy.dynamic_loss_margin_pct,
                "swap_dwell_s": cfg.policy.dynamic_swap_dwell_s,
            },
            "managed_default_route": {
                "enabled": cfg.policy.manage_default_route,
                "metric": MANAGED_DEFAULT_METRIC,
                "iface": managed_default[0] if managed_default else None,
                "gateway": managed_default[1] if managed_default else None,
            },
            # via=None and dev=None together signal "engarde PBR not provisioned"
            # (the actuator never deletes — apply_engarde_table_action only does
            # 'replace'). UI surfaces a red badge when both are null.
            "engarde_table": {
                "name": cfg.egress.engarde_table,
                "via": engarde_after.get("via") if engarde_after else None,
                "dev": engarde_after.get("dev") if engarde_after else None,
            },
            "fec": {
                "configured": bool(cfg.fec and cfg.fec.enabled),
                "desired_enabled": fec_desired,
                "desired_mode": fec_mode_eff,
                "desired_fixed_ratio": fec_fixed_ratio_eff,
                "floor_ratio": fec_floor_ratio_eff,
                "floor_override": ov.fec_floor_ratio,
                # fixed_ratio_presets is retained so a cached older page keeps
                # working; ratio_presets is what the current UI reads for both
                # the fixed and the floor dropdown.
                "fixed_ratio_presets": list(fec_control.FIXED_RATIO_PRESETS),
                "ratio_presets": list(fec_control.FIXED_RATIO_PRESETS),
                "profile": {
                    "name": fec_profile_active, "driver_wan": fec_driver,
                    # Mirrors the `dynamic` block: what is challenging the
                    # driver and how long it still has to hold, so a profile
                    # that looks stuck can be told apart from one being held.
                    "driver_candidate": fec_driver_candidate,
                    "driver_dwell_remaining_s": (
                        max(0.0, cfg.fec.driver_dwell_s
                            - (loop_start - fec_driver_candidate_since))
                        if (cfg.fec and fec_driver_candidate_since is not None)
                        else 0.0),
                },
                "signal_floor_active": fec_signal_floor_applied,
                "location_floor": {
                    "configured": cfg.location is not None,
                    "enabled": location_enabled,
                    # Binding = it actually changed the ratio sent this tick.
                    # level/reason/wans stay published in every mode (the
                    # daemon's request is informative either way); only
                    # `active` claims the parity reached the wire.
                    "active": fec_location_applied,
                    "level": fec_location_level,
                    "reason": fec_location_reason,
                    "wans": location_floors or {},
                },
                "directions": {
                    "client_to_relay": {
                        "enabled": fec_desired,
                        "mode": fec_mode_eff,
                        "fixed_ratio": fec_fixed_ratio_eff,
                        "ratio": fec_current_ratio,
                        "level": fec_rt.current_level,
                        # Where that ratio sits on the ACTIVE profile's ladder,
                        # so the UI can size its pip row to the rungs that
                        # actually exist (5 on the base table, 4 on cellular).
                        "ladder": fec_ladder,
                        "driving_loss_pct": (fec_loss.get(fec_driver_display) if fec_driver_display else None),
                        "driver_wan": fec_driver_display,
                        "loss_source": fec_loss_source,
                        "since": fec_ratio_since,
                        "actuator_ok": fec_actuator_ok,
                        "wire": (wire_tracker.snapshot(loop_start) if wire_tracker else None),
                        # client->relay decode outcomes are measured AT THE
                        # RELAY; they ride back on the /fec fetch.
                        "rx": ((fec_relay_last or {}).get("data") or {}).get("rx"),
                    },
                    "relay_to_client": relay_fec_direction(
                        fec_relay_last, fec_relay_last_at, loop_start,
                        relay_desired, fec_relay_last_acked,
                        local_rx=(wire_tracker.rx_snapshot(loop_start)
                                  if wire_tracker else None),
                        ladder_inputs=fec_ladder_r2c),
                },
            },
            "environmental": environmental_snapshot(
                configured=cfg.environmental is not None,
                env_enabled=env_enabled, active=env_active,
                auto=env_auto, now=loop_start),
            "cell": cell_snapshot(cfg, cell_sample, loop_start),
            "duplication": {
                "configured": cfg.cell is not None,
                "active": handoff_was_active,
                "reason": (handoff_active.reason if handoff_active else None),
                "remaining_s": (round(handoff_active.until_ts - loop_start, 1)
                                if handoff_active else 0.0),
                "count": duplication_count,
                "last_ts": duplication_last_ts,
                "last_reason": duplication_last_reason,
            },
            # The resolved schedule, published so maintenance_reboot.py reads it
            # rather than re-deriving the config-vs-overlay precedence. (Its
            # read_published() also demands the snapshot's fresh "ts" above.)
            "maintenance": {
                "configured": cfg.maintenance is not None,
                "enabled": maint_enabled,
                "hour": maint_hour,
            },
        }
        if detector is not None:
            fec_engaged = False
            fec_at_max = False
            if cfg.fec and fec_current_ratio:
                try:
                    _fa, _fb = fec_control.parse_ratio(fec_current_ratio)
                    fec_engaged = _fb > 0
                except ValueError:
                    pass
                if fec_mode_eff in (fec_control.MODE_ADAPTIVE,
                                    fec_control.MODE_MIN_ADAPTIVE):
                    # Compare against the ACTIVE profile's table, not the
                    # base cfg.fec.loss_table — a shorter cellular-profile
                    # table (e.g. 4 rows, max index 3) means current_level
                    # never reaches len(cfg.fec.loss_table) - 1, permanently
                    # suppressing the fec-at-max notification.
                    fec_at_max = fec_control.is_level_at_max(
                        fec_rt.current_level, prof_table)
            for _ev in detector.observe(notify.Observation(
                    wan_states={w: eff.get(w, "UNKNOWN") for w in cfg.wans},
                    wan_labels={w: cfg.wans[w].label for w in cfg.wans},
                    mode=mode,
                    env_active=env_active is not None,
                    env_reason=(env_active.reason if env_active else ""),
                    fec_engaged=fec_engaged,
                    fec_at_max=fec_at_max,
                    relay_polled=relay_polled,
                    relay_ok=last_remote.ok,
                    switch=switch_event,
                    maintenance=maint_window,
                    handoff_active=handoff_was_active)):
                notifier.notify(_ev)
        if fec_hist is not None and cfg.fec:
            fec_hist.append_from_directions(
                loop_start, snapshot["fec"]["directions"])
        publish_state(cfg, snapshot)

        elapsed = time.time() - loop_start
        stop_event.wait(max(0.0, tick - elapsed))

    if notifier is not None:
        notifier.stop()
    # The keep-alive pool is process-global; don't leave a relay socket behind
    # for whatever runs after this loop.
    close_relay_conns()


def withdraw_managed_default(cfg: Config):
    """Best-effort cleanup of our metric-50 default on shutdown."""
    if not cfg.policy.manage_default_route:
        return
    cur = read_managed_default()
    if cur is None:
        return
    try:
        apply_route_action(("delete", cur[0], cur[1]))
        logging.info("withdrew managed default route on shutdown")
    except RuntimeError as e:
        logging.warning("could not withdraw managed default: %s", e)


def main():
    import signal

    ap = argparse.ArgumentParser(description="sbfd-ctl - failover controller")
    ap.add_argument("-c", "--config", required=True, help="path to JSON config")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--no-ui", action="store_true", help="disable web UI (for testing)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config(args.config)
    stop = threading.Event()

    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    fec_hist = fec_history.FecHistory() if cfg.fec else None
    if not args.no_ui:
        start_ui_server(cfg, stop, fec_hist=fec_hist)

    try:
        wire_tracker = None
        if cfg.fec:
            wire_tracker = fec_report.FecWireTracker(
                "client_to_server", cfg.fec.wire_stale_after_s)
            fec_report.start_wire_tailer(cfg.fec.wire_unit, wire_tracker, stop)
        run_controller(cfg, stop, wire_tracker=wire_tracker, fec_hist=fec_hist)
    except KeyboardInterrupt:
        logging.info("shutting down")
        stop.set()
    finally:
        withdraw_managed_default(cfg)


if __name__ == "__main__":
    main()
