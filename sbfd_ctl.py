#!/usr/bin/env python3
"""
sbfd-ctl - failover controller for sbfd + engarde.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import fec_control
import fec_report

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
                            dynamic_candidate_since=new_dyn_cand_since)
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
        )

    if not master_up and backup_up:
        return DecideOutput(
            desired_active={backup},
            reason=f"master ({master}) DOWN, fail-over to {backup}",
            master_up_since=new_master_up_since,
            dynamic_master_current=new_dyn_master,
            dynamic_candidate=new_dyn_cand,
            dynamic_candidate_since=new_dyn_cand_since,
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
            )

    return DecideOutput(
        desired_active={master},
        reason=f"master ({master}) UP, steady state",
        master_up_since=new_master_up_since,
        dynamic_master_current=new_dyn_master,
        dynamic_candidate=new_dyn_cand,
        dynamic_candidate_since=new_dyn_cand_since,
    )


# -- State sensing -----------------------------------------------------------

import os
import time
import json as _json
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


def fetch_remote_sbfd_state(url: str,
                            timeout_s: float,
                            session_id_to_wan: dict) -> StateSnapshot:
    """HTTP GET the relay /state endpoint. Fail-open on any error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        return StateSnapshot(ok=False, per_wan={}, error=f"HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        return StateSnapshot(ok=False, per_wan={}, error=f"URL error: {e.reason}")
    except (TimeoutError, OSError) as e:
        return StateSnapshot(ok=False, per_wan={}, error=f"transport: {e}")

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
    """DOWN dominates: a WAN is DOWN if either side reports DOWN."""
    eff = {}
    wans = set((local.per_wan if local.ok else {}).keys()) | \
           set((remote.per_wan if remote.ok else {}).keys())
    for w in wans:
        l = local.per_wan.get(w) if local.ok else None
        r = remote.per_wan.get(w) if remote.ok else None
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


def apply_nft_diff(cfg: Config, actions: list) -> None:
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
        if line.startswith("delete rule"):
            wan = _wan_from_rule_line(line)
            handle = _find_drop_handle(cfg, wan) if wan else None
            if handle is not None:
                fallback = ["nft", "delete", "rule", cfg.nft.family, cfg.nft.table,
                            "egress_filter", "handle", str(handle)]
                r3 = subprocess.run(fallback, capture_output=True, text=True)
                if r3.returncode == 0:
                    continue
        raise RuntimeError(f"nft action {line!r} failed: {r2.stderr.strip()}")


# -- Kernel default-route actuator -------------------------------------------
# Beats DHCP-installed defaults (typically metric 100/200) by installing our
# own at metric 50, so system traffic (tailscale, NTP, package mgrs) follows
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


def compute_fec_target(fec_cfg, mode, eff, loss, active_wans):
    """Pure: map mode/effective-state/loss to a FEC table level.
    Loss = worst among the WANs actually carrying traffic."""
    up_count = sum(1 for w, st in eff.items() if st == "UP")
    active = active_wans or set(loss.keys())
    active_loss = max((loss.get(w, 0.0) for w in active), default=0.0)
    return fec_control.mode_aware_level(
        mode, up_count, active_loss, fec_cfg.loss_table,
        fec_cfg.full_min_up_wans, fec_cfg.full_mode_backoff_fec)


def fec_driver_wan(loss, active_wans):
    """The active WAN whose loss feeds compute_fec_target (the max-loss one).
    Uses the same active fallback as compute_fec_target. None if no candidates."""
    active = active_wans or set(loss.keys())
    if not active:
        return None
    return max(active, key=lambda w: loss.get(w, 0.0))


def should_post_fec(desired, last_acked, last_post_ts, now, heartbeat_s=30.0):
    """Reconcile decision: POST when desired differs from last-acked (assert until
    acked), on the first tick, or once the heartbeat elapses (defends against an
    relay restart that reverted to its default)."""
    if desired != last_acked:
        return True
    if last_post_ts is None:
        return True
    return (now - last_post_ts) >= heartbeat_s


def relay_fec_direction(fetch, fetched_at, now, desired, last_acked):
    """Shape the relay->client published direction dict from a fetch_relay_fec result."""
    fetch = fetch or {}
    data = fetch.get("data") or {}
    return {
        "enabled": data.get("enabled"),
        "ratio": data.get("ratio"),
        "level": data.get("level"),
        "driving_loss_pct": data.get("driving_loss_pct"),
        "since": data.get("since"),
        "ok": bool(fetch.get("ok")),
        "stale_s": (now - fetched_at) if fetched_at else None,
        "error": fetch.get("error"),
        "reconcile_pending": (last_acked != desired),
        "wire": data.get("wire"),
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

    Returns None | ('replace', iface, gw) | ('delete', iface, gw).
    Safety: if desired_iface is set but desired_gateway is None (couldn't
    read it), returns None — refuse to act on incomplete info.
    """
    if desired_iface is None:
        if current is not None:
            return ("delete", current[0], current[1])
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
            fec_cfg = FecCfg(
                enabled=bool(raw_fec.get("enabled", True)),
                fifo=raw_fec["fifo"],
                loss_table=raw_fec.get("loss_table", fec_control.DEFAULT_LOSS_TABLE),
                ramp_up_ticks=int(raw_fec.get("ramp_up_ticks", 2)),
                ramp_down_hold_s=float(raw_fec.get("ramp_down_hold_s", 20.0)),
                full_mode_backoff_fec=raw_fec.get("full_mode_backoff_fec", "1:0"),
                full_min_up_wans=int(raw_fec.get("full_min_up_wans", 2)),
                wire_unit=raw_fec.get("wire_unit", "udpspeeder-client"),
                wire_stale_after_s=float(raw_fec.get("wire_stale_after_s", 30.0)),
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
    fec_enabled: Optional[bool] = None


def load_runtime_overlay(cfg: Config) -> RuntimeOverlay:
    """Load runtime.json from /run; if absent, try persist file; else defaults."""
    for path, src in ((cfg.runtime_state, "runtime"), (cfg.persist_state, "persist")):
        try:
            with open(path) as f:
                raw = _json.load(f)
            return RuntimeOverlay(
                mode=raw.get("mode"),
                master_policy=raw.get("master_policy"),
                master_wan=raw.get("master_wan"),
                persist=bool(raw.get("persist", False)),
                set_by=raw.get("set_by", src),
                set_ts=float(raw.get("set_ts", 0.0)),
                egress_mode=raw.get("egress_mode"),
                fec_enabled=raw.get("fec_enabled"),
            )
        except (FileNotFoundError, ValueError, OSError):
            continue
    return RuntimeOverlay()


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
    }
    body = _json.dumps(payload, indent=2)
    Path(cfg.runtime_state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.runtime_state).write_text(body)
    if ov.persist:
        Path(cfg.persist_state).parent.mkdir(parents=True, exist_ok=True)
        Path(cfg.persist_state).write_text(body)
    elif Path(cfg.persist_state).exists():
        try:
            Path(cfg.persist_state).unlink()
        except OSError:
            pass


def effective_policy(cfg: Config, ov: RuntimeOverlay) -> tuple:
    mode = ov.mode if ov.mode in VALID_MODES else cfg.policy.default_mode
    policy = ov.master_policy if ov.master_policy in VALID_POLICIES else cfg.policy.default_master_policy
    master_wan = ov.master_wan if ov.master_wan in cfg.wans else cfg.policy.default_master_wan
    egress_mode = ov.egress_mode if ov.egress_mode in VALID_EGRESS_MODES else cfg.egress.default_mode
    return mode, policy, master_wan, egress_mode


def effective_fec_enabled(cfg: Config, ov: RuntimeOverlay) -> bool:
    """Operator override if set, else the config default. Always False when FEC
    is unconfigured (cfg.fec absent or cfg.fec.enabled false)."""
    if not (cfg.fec and cfg.fec.enabled):
        return False
    if ov.fec_enabled is not None:
        return ov.fec_enabled
    return True


def fetch_relay_fec(url, timeout_s) -> dict:
    """Best-effort GET of the relay /fec endpoint. Returns {ok, data, error};
    never raises (mirrors fetch_remote_sbfd_state's defensive style)."""
    if not url:
        return {"ok": False, "data": None, "error": "no fec_url configured"}
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            data = _json.loads(resp.read().decode())
        return {"ok": True, "data": data, "error": None}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "data": None, "error": f"transport: {e}"}
    except ValueError as e:
        return {"ok": False, "data": None, "error": f"parse: {e}"}


def post_relay_fec(url, enabled, timeout_s) -> bool:
    """Best-effort POST of desired enabled to relay /fec. Returns True iff acked 200."""
    if not url:
        return False
    body = _json.dumps({"enabled": bool(enabled)}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
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
    pi_local = snap.get("pi_local", {})
    wan = pi_local.get(master_wan, {})
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
    return True, None


def start_ui_server(cfg: Config, stop_event: threading.Event):
    """Bind the UI HTTP server (returns the bound httpd; caller doesn't need it)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    ui_dir = Path(__file__).resolve().parent / "ui"
    deployed_ui_dir = Path("/opt/sbfd-ctl/ui")
    if deployed_ui_dir.exists():
        ui_dir = deployed_ui_dir

    wan_names = set(cfg.wans.keys())

    class Handler(BaseHTTPRequestHandler):
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

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send_static("index.html", "text/html; charset=utf-8")
            elif self.path == "/app.js":
                self._send_static("app.js", "application/javascript")
            elif self.path == "/wall.css":
                self._send_static("wall.css", "text/css; charset=utf-8")
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
            elif self.path == "/api/engarde":
                url = cfg.engarde.admin_url or f"http://{cfg.engarde.server_ip}:8080/api/v1/get-list"
                try:
                    with urllib.request.urlopen(url, timeout=1.5) as resp:
                        body = resp.read()
                    payload = _json.loads(body.decode())
                    self._send_json(200, {"ok": True, "data": payload})
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
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path != "/api/runtime":
                self.send_error(404); return
            length = int(self.headers.get("Content-Length", "0") or "0")
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

def run_controller(cfg: Config, stop_event=None, wire_tracker=None):
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
    fec_hyst = (fec_control.FecHysteresis(cfg.fec.ramp_up_ticks, cfg.fec.ramp_down_hold_s)
                if cfg.fec else None)
    fec_current_ratio = None
    fec_ratio_since: Optional[float] = None
    fec_ovh_last = {"ok": False, "data": None, "error": "not yet fetched"}
    fec_ovh_last_at = 0.0
    fec_ovh_last_acked = None
    fec_ovh_last_post_ts = None

    if stop_event is None:
        stop_event = threading.Event()

    while not stop_event.is_set():
        loop_start = time.time()
        ov = load_runtime_overlay(cfg)
        mode, policy, master_wan, egress_mode = effective_policy(cfg, ov)

        local = read_local_sbfd_state(cfg.sbfd_local_state, sid_to_wan)
        fec_reconcile_due = False
        if loop_start - last_remote_at >= remote_interval:
            last_remote = fetch_remote_sbfd_state(
                cfg.relay.state_url, cfg.relay.fetch_timeout_s, sid_to_wan)
            last_remote_at = loop_start
            if cfg.fec:
                fec_ovh_last = fetch_relay_fec(cfg.relay.fec_url, cfg.relay.fetch_timeout_s)
                fec_ovh_last_at = loop_start
                fec_reconcile_due = True

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
                logging.info("switch: %s -> %s (%s)",
                             sorted(currently_active), sorted(out.desired_active), out.reason)
                currently_active = set(out.desired_active)
            except RuntimeError as e:
                logging.error("nft apply failed: %s", e)

        managed_default = None
        if cfg.policy.manage_default_route:
            chosen = pick_default_wan(eff, out.desired_active,
                                      cfg.policy.default_master_wan,
                                      dynamic_master_current)
            chosen_iface = cfg.wans[chosen].iface if chosen else None
            chosen_gw = read_wan_gateway(chosen_iface) if chosen_iface else None
            current_managed = read_managed_default()
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

        # Engarde-table reconciliation (egress_mode actuator).
        engarde_master_iface = cfg.wans[master_wan].iface if (egress_mode == "local_direct" and master_wan in cfg.wans) else None
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

        fec_desired = effective_fec_enabled(cfg, ov)
        fec_driver = None
        fec_actuator_ok = True
        if cfg.fec:
            if fec_desired:
                _fec_target = compute_fec_target(cfg.fec, mode, eff, loss, currently_active)
                fec_rt, _fec_changed = fec_control.step_level(_fec_target, fec_rt, fec_hyst, loop_start)
                _fec_ratio = fec_control.level_to_ratio(fec_rt.current_level, cfg.fec.loss_table)
                fec_driver = fec_driver_wan(loss, currently_active)
                if _fec_changed or fec_current_ratio is None:
                    fec_actuator_ok = fec_control.write_fifo(cfg.fec.fifo, _fec_ratio, logging)
                    if fec_actuator_ok:
                        fec_current_ratio = _fec_ratio
                        fec_ratio_since = loop_start
                        logging.info("fec ratio -> %s (mode=%s active=%s)",
                                     _fec_ratio, mode, sorted(currently_active))
            else:
                _off_ratio = fec_control.level_to_ratio(0, cfg.fec.loss_table)
                if fec_current_ratio != _off_ratio:
                    fec_actuator_ok = fec_control.write_fifo(cfg.fec.fifo, _off_ratio, logging)
                    if fec_actuator_ok:
                        fec_rt = fec_control.FecRuntime(0, 0, loop_start)
                        fec_current_ratio = _off_ratio
                        fec_ratio_since = loop_start
                        logging.info("fec disabled -> %s (forced off)", _off_ratio)

            # Reconcile desired enable-state to relay on the remote-fetch cadence
            # only (best-effort) — rate-limiting to the relay tick keeps a slow or
            # unreachable relay from blocking the 0.5s failover loop on every tick.
            if fec_reconcile_due and should_post_fec(
                    fec_desired, fec_ovh_last_acked, fec_ovh_last_post_ts, loop_start):
                if post_relay_fec(cfg.relay.fec_url, fec_desired, cfg.relay.fetch_timeout_s):
                    fec_ovh_last_acked = fec_desired
                fec_ovh_last_post_ts = loop_start

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
            "effective": eff,
            "wan_labels": {w: cfg.wans[w].label for w in cfg.wans},
            "engarde_server": f"{cfg.engarde.server_ip}:{cfg.engarde.server_port}",
            "pi_local": {w: _wan_obj(local, w) for w in cfg.wans},
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
                "directions": {
                    "client_to_relay": {
                        "enabled": fec_desired,
                        "ratio": fec_current_ratio,
                        "level": fec_rt.current_level,
                        "driving_loss_pct": (loss.get(fec_driver) if fec_driver else None),
                        "driver_wan": fec_driver,
                        "since": fec_ratio_since,
                        "actuator_ok": fec_actuator_ok,
                        "wire": (wire_tracker.snapshot(loop_start) if wire_tracker else None),
                    },
                    "relay_to_client": relay_fec_direction(
                        fec_ovh_last, fec_ovh_last_at, loop_start,
                        fec_desired, fec_ovh_last_acked),
                },
            },
        }
        publish_state(cfg, snapshot)

        elapsed = time.time() - loop_start
        stop_event.wait(max(0.0, tick - elapsed))


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

    if not args.no_ui:
        start_ui_server(cfg, stop)

    try:
        wire_tracker = None
        if cfg.fec:
            wire_tracker = fec_report.FecWireTracker(
                "client_to_server", cfg.fec.wire_stale_after_s)
            fec_report.start_wire_tailer(cfg.fec.wire_unit, wire_tracker, stop)
        run_controller(cfg, stop, wire_tracker=wire_tracker)
    except KeyboardInterrupt:
        logging.info("shutting down")
        stop.set()
    finally:
        withdraw_managed_default(cfg)


if __name__ == "__main__":
    main()
