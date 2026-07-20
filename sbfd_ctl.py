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


@dataclass
class EnvironmentalCfg:
    enabled: bool
    auto_override_path: str
    auto_override_ttl_s: float = 180.0


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


def compute_fec_target(fec_cfg, mode, eff, loss, active_wans):
    """Pure: map mode/effective-state/loss to a FEC table level (the
    pre-fec-mode-override adaptive choice). Loss = worst among the WANs
    actually carrying traffic."""
    up_count = sum(1 for w, st in eff.items() if st == "UP")
    active_loss = worst_active_loss(loss, active_wans)
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
    """Shape the relay->client published direction dict from a fetch_relay_fec result.

    `desired` / `last_acked` are either the legacy enabled-boolean (during the
    pre-tri-state transition) or a (mode, fixed_ratio) tuple. We just compare
    them for equality to drive reconcile_pending — the relay echoes back what
    it actually applied."""
    fetch = fetch or {}
    data = fetch.get("data") or {}
    return {
        "enabled": data.get("enabled"),
        "mode": data.get("mode"),
        "fixed_ratio": data.get("fixed_ratio"),
        "ratio": data.get("ratio"),
        "level": data.get("level"),
        "driving_loss_pct": data.get("driving_loss_pct"),
        "loss_source": data.get("loss_source"),
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
    if env_cfg is not None and env_cfg.auto_override_ttl_s <= 0:
        raise ValueError(
            f"environmental.auto_override.ttl_s must be > 0, got {env_cfg.auto_override_ttl_s}")
    if maint_cfg is not None and not 0 <= maint_cfg.hour <= 23:
        raise ValueError(
            f"maintenance_reboot.hour must be 0..23, got {maint_cfg.hour}")
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
                environmental_enabled=raw.get("environmental_enabled"),
                maintenance_enabled=raw.get("maintenance_enabled"),
                maintenance_hour=raw.get("maintenance_hour"),
            )
        except (FileNotFoundError, ValueError, OSError):
            continue
    return RuntimeOverlay()


def _atomic_write_text(path: str, body: str) -> None:
    """Write via a temp file + os.replace so a concurrent reader never sees a
    truncated/partial file. os.replace is atomic within a filesystem."""
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(body)
    os.replace(tmp, p)


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
    except (FileNotFoundError, ValueError, OSError, KeyError, TypeError):
        return None
    if now - set_ts > env.auto_override_ttl_s:
        return None
    return AutoOverride(
        force_full=bool(raw.get("force_full", False)),
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
    if ov.fec_fixed_ratio:
        return ov.fec_fixed_ratio
    if cfg.fec:
        return cfg.fec.fixed_ratio
    return fec_control.DEFAULT_FIXED_RATIO


def effective_fec_floor_ratio(cfg: Config, ov: RuntimeOverlay) -> str:
    """The floor used by MODE_MIN_ADAPTIVE — operator override wins, else cfg."""
    if ov.fec_floor_ratio:
        return ov.fec_floor_ratio
    if cfg.fec:
        return cfg.fec.floor_ratio
    return fec_control.DEFAULT_FLOOR_RATIO


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
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            data = _json.loads(resp.read().decode())
        return {"ok": True, "data": data, "error": None}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "data": None, "error": f"transport: {e}"}
    except ValueError as e:
        return {"ok": False, "data": None, "error": f"parse: {e}"}


def post_relay_fec(url, mode, fixed_ratio, floor_ratio, timeout_s,
                   client_loss_pct=None) -> bool:
    """Best-effort POST of desired (mode, fixed_ratio, floor_ratio) to relay /fec.

    client_loss_pct carries our locally measured relay->client loss — the
    direction the relay's TX leg repairs but cannot see (sbfd loss is
    RX-side). Older relays ignore the extra fields. Also sends the legacy
    `enabled` boolean so an older relay binary still honors the off/on intent
    during a rolling upgrade. Returns True iff 200."""
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
    body = _json.dumps(payload).encode()
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
            try:
                payload[_key] = fec_control.resolve_ratio(payload[_key])
            except ValueError as e:
                return False, f"{_key}: {e}"
    if "environmental_enabled" in payload and not isinstance(payload["environmental_enabled"], bool):
        return False, "environmental_enabled must be true or false"
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
}

_SID_RE = re.compile(r"^s[0-9]+$")


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


def assemble_map_payload(map_cfg, published_state_path, fix, now) -> dict:
    """Aggregate every map data source; each degrades independently to
    null/empty — a broken source must never 500 the endpoint."""
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
    return {"ts": now,
            "fix": out_fix,
            "stations": stations,
            "predictions": predict_from_stations(st),
            "environ": _read_json_file(map_cfg["environ_points_path"]),
            "mode": snap.get("mode"),
            "active": snap.get("active_wans")}


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
        fix = environ_ctl.get_fix(host, port, timeout=1.5)
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


def start_ui_server(cfg: Config, stop_event: threading.Event):
    """Bind the UI HTTP server (returns the bound httpd; caller doesn't need it)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    ui_dir = Path(__file__).resolve().parent / "ui"
    deployed_ui_dir = Path("/opt/sbfd-ctl/ui")
    if deployed_ui_dir.exists():
        ui_dir = deployed_ui_dir

    wan_names = set(cfg.wans.keys())
    map_cfg = resolve_map_cfg(cfg.map)

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
            elif self.path == "/api/map":
                g = map_cfg["gpsd"]
                fix = get_map_fix(g["host"], g["port"])
                self._send_json(200, assemble_map_payload(
                    map_cfg, cfg.published_state, fix, time.time()))
            elif (m := _TILE_RE.match(self.path)):
                self._serve_tile(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path == "/api/station-label":
                length = int(self.headers.get("Content-Length", "0") or "0")
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
    fec_relay_last = {"ok": False, "data": None, "error": "not yet fetched"}
    fec_relay_last_at = 0.0
    fec_relay_last_acked = None
    fec_relay_last_post_ts = None

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
        env_enabled = effective_environmental_enabled(cfg, ov)
        mode, env_active = apply_auto_override(mode, env_enabled, env_auto)
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
        fec_actuator_ok = True
        fec_loss = loss
        fec_loss_source = "local"
        relay_desired = (fec_mode_eff, fec_fixed_ratio_eff, fec_floor_ratio_eff)
        if cfg.fec:
            # Our TX leg repairs client->relay loss, which only the relay can
            # measure — drive it from the fetched relay snapshot (local loss,
            # the opposite direction, is the stale-relay fallback).
            remote_fresh = bool(last_remote_at) and (
                loop_start - last_remote_at) <= max(3 * remote_interval, 10.0)
            fec_loss, fec_loss_source = fec_loss_map(
                local, last_remote, remote_fresh, cfg.wans)
            # The adaptive engine always runs so the loss-tracked level stays
            # fresh; apply_mode then maps it to the actual ratio per mode.
            _fec_target = compute_fec_target(cfg.fec, mode, eff, fec_loss, currently_active)
            fec_rt, _fec_changed = fec_control.step_level(
                _fec_target, fec_rt, fec_hyst, loop_start)
            _adaptive_ratio = fec_control.level_to_ratio(
                fec_rt.current_level, cfg.fec.loss_table)
            _fec_ratio = fec_control.apply_mode(
                fec_mode_eff, _adaptive_ratio,
                fixed_ratio=fec_fixed_ratio_eff,
                floor_ratio=fec_floor_ratio_eff)
            # Push our locally measured (relay->client) loss so the relay can
            # drive ITS leg on the direction it actually repairs. Quantized to
            # a table level in relay_desired so posts fire on level changes,
            # not every EWMA wiggle.
            client_push_loss = worst_active_loss(loss, currently_active)
            relay_desired = (fec_mode_eff, fec_fixed_ratio_eff,
                             fec_floor_ratio_eff,
                             fec_control.loss_to_level(client_push_loss,
                                                       cfg.fec.loss_table))
            if fec_mode_eff in (fec_control.MODE_ADAPTIVE, fec_control.MODE_MIN_ADAPTIVE):
                fec_driver = fec_driver_wan(fec_loss, currently_active)
            if _fec_ratio != fec_current_ratio:
                fec_actuator_ok = fec_control.write_fifo(
                    cfg.fec.fifo, _fec_ratio, logging)
                if fec_actuator_ok:
                    fec_current_ratio = _fec_ratio
                    fec_ratio_since = loop_start
                    logging.info("fec ratio -> %s (fec_mode=%s mode=%s active=%s)",
                                 _fec_ratio, fec_mode_eff, mode,
                                 sorted(currently_active))

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
                                  client_loss_pct=client_push_loss):
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
                # fixed_ratio_presets is retained so a cached older page keeps
                # working; ratio_presets is what the current UI reads for both
                # the fixed and the floor dropdown.
                "fixed_ratio_presets": list(fec_control.FIXED_RATIO_PRESETS),
                "ratio_presets": list(fec_control.FIXED_RATIO_PRESETS),
                "directions": {
                    "client_to_relay": {
                        "enabled": fec_desired,
                        "mode": fec_mode_eff,
                        "fixed_ratio": fec_fixed_ratio_eff,
                        "ratio": fec_current_ratio,
                        "level": fec_rt.current_level,
                        "driving_loss_pct": (fec_loss.get(fec_driver) if fec_driver else None),
                        "driver_wan": fec_driver,
                        "loss_source": fec_loss_source,
                        "since": fec_ratio_since,
                        "actuator_ok": fec_actuator_ok,
                        "wire": (wire_tracker.snapshot(loop_start) if wire_tracker else None),
                    },
                    "relay_to_client": relay_fec_direction(
                        fec_relay_last, fec_relay_last_at, loop_start,
                        relay_desired, fec_relay_last_acked),
                },
            },
            "environmental": environmental_snapshot(
                configured=cfg.environmental is not None,
                env_enabled=env_enabled, active=env_active,
                auto=env_auto, now=loop_start),
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
                    fec_at_max = fec_rt.current_level >= len(cfg.fec.loss_table) - 1
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
                    maintenance=maint_window)):
                notifier.notify(_ev)
        publish_state(cfg, snapshot)

        elapsed = time.time() - loop_start
        stop_event.wait(max(0.0, tick - elapsed))

    if notifier is not None:
        notifier.stop()


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
