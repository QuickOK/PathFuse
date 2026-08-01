#!/usr/bin/env python3
"""
Read-only preview of the sbfd-ctl wall display.

Spins up start_ui_server() backed by a synthesized published_state derived from
the live /run/sbfd/state.json that sbfd already writes. No nft writes, no
runtime overlay changes - hitting /api/runtime returns errors (no controller
loop is running). Pure visual preview.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sbfd_ctl import (
    Config, EngardeCfg, NftCfg, RelayCfg, PolicyCfg, WanCfg,
    load_config, read_local_sbfd_state, fetch_remote_sbfd_state,
    start_ui_server,
)


def build_snapshot(cfg: Config, sid_to_wan: dict) -> dict:
    local = read_local_sbfd_state(cfg.sbfd_local_state, sid_to_wan)
    try:
        remote = fetch_remote_sbfd_state(cfg.relay.state_url, cfg.relay.fetch_timeout_s, sid_to_wan)
    except Exception as e:  # noqa: BLE001
        from sbfd_ctl import StateSnapshot
        remote = StateSnapshot(ok=False, per_wan={}, error=f"preview: {e}")

    def wo(snap, w):
        s = snap.per_wan.get(w) if snap.ok else None
        if not s:
            return {"state": "UNKNOWN", "rtt_ms": None, "loss_pct": None, "state_since": None}
        return {"state": s.state, "rtt_ms": s.rtt_ms, "loss_pct": s.loss_pct, "state_since": s.state_since}

    eff = {}
    for w in cfg.wans:
        a = (local.per_wan.get(w).state if local.ok and local.per_wan.get(w) else "UNKNOWN")
        b = (remote.per_wan.get(w).state if remote.ok and remote.per_wan.get(w) else "UNKNOWN")
        eff[w] = "DOWN" if "DOWN" in (a, b) else ("UP" if a == "UP" or b == "UP" else "UNKNOWN")

    now = time.time()
    return {
        "ts": now,
        "mode": cfg.policy.default_mode,
        "master_policy": cfg.policy.default_master_policy,
        "master_wan": cfg.policy.default_master_wan,
        "effective": eff,
        "wan_labels": {w: cfg.wans[w].label for w in cfg.wans},
        "engarde_server": f"{cfg.engarde.server_ip}:{cfg.engarde.server_port}",
        "client_local": {w: wo(local, w) for w in cfg.wans},
        "relay_remote": {
            "states": {w: wo(remote, w) for w in cfg.wans},
            "fetched_at": remote.fetched_at,
            "stale_s": 0.0,
            "ok": remote.ok,
            "error": remote.error,
        },
        "active_wans": sorted(cfg.wans.keys()),
        "last_switch": {"ts": now, "from": sorted(cfg.wans.keys()),
                        "to": sorted(cfg.wans.keys()), "reason": "preview - no controller running"},
        "recent_switches": [],
        "decision_reason": "preview mode (read-only)",
        "failback_remaining_s": 0.0,
        "dynamic": {
            "master": None, "candidate": None, "candidate_since": None,
            "swap_dwell_remaining_s": 0.0,
            "rtt_margin_ms": cfg.policy.dynamic_rtt_margin_ms,
            "loss_margin_pct": cfg.policy.dynamic_loss_margin_pct,
            "swap_dwell_s": cfg.policy.dynamic_swap_dwell_s,
        },
        "fec": {
            "configured": True,
            "desired_enabled": True,
            "desired_mode": "min_adaptive",
            "floor_ratio": "20:1",
            "directions": {
                # c2r sits two rungs over its floor (pips lit and flashing);
                # r2c is on the cellular ladder, one rung over a floor that IS
                # a rung there — the two shapes the pip row has to handle.
                "client_to_relay": {
                    "enabled": True, "ratio": "8:4", "level": 2,
                    "mode": "min_adaptive",
                    "ladder": {"levels": 5, "floor_level": 0, "applied_level": 2},
                    "driving_loss_pct": 3.1, "driver_wan": cfg.policy.default_master_wan,
                    "since": now - 42, "actuator_ok": True,
                    "wire": {"tx_mbps": 4.2, "overhead_pct": 16.7, "sample_age_s": 6.0, "stale": False},
                },
                "relay_to_client": {
                    "enabled": True, "ratio": "12:1", "level": 2,
                    "mode": "min_adaptive",
                    "ladder": {"levels": 4, "floor_level": 1, "applied_level": 2},
                    "driving_loss_pct": 1.2, "since": now - 133,
                    "ok": True, "stale_s": 0.4, "error": None,
                    "reconcile_pending": False,
                    "wire": {"tx_mbps": 3.8, "overhead_pct": 9.0, "sample_age_s": 4.0, "stale": False},
                },
            },
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config/sbfd-ctl.example.json")
    ap.add_argument("-l", "--listen", default=None,
                    help="override ui_listen, e.g. 0.0.0.0:8181")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.listen:
        cfg.ui_listen = args.listen
    cfg.published_state = str(Path(tempfile.gettempdir()) / f"sbfd-ui-preview-state-{os.getuid()}.json")

    sid_to_wan = {w.session_id: name for name, w in cfg.wans.items()}

    Path(cfg.published_state).write_text(json.dumps(build_snapshot(cfg, sid_to_wan)))

    stop = threading.Event()
    httpd = start_ui_server(cfg, stop)
    print(f"preview UI on http://{cfg.ui_listen}/  (read-only, no nft writes)", flush=True)

    try:
        while True:
            try:
                snap = build_snapshot(cfg, sid_to_wan)
                tmp = Path(cfg.published_state).with_suffix(".tmp")
                tmp.write_text(json.dumps(snap))
                tmp.replace(cfg.published_state)
            except Exception as e:  # noqa: BLE001
                print(f"snapshot refresh error: {e}", flush=True)
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
