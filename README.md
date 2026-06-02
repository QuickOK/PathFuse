# PathFuse

Make one dependable connection out of several unreliable WAN uplinks — per-link
liveness (software BFD), automatic failover, cross-link packet redundancy, and
adaptive forward error correction. Built for **uptime and low loss** on
cellular / satellite / fiber / wired links. It trades bandwidth for reliability
and does **not** sum link bandwidth (this is redundancy + failover + FEC, not
bandwidth aggregation).

## What's here

PathFuse is a set of small, stdlib-only Python daemons layered on top of an
existing multipath relay tunnel (engarde), WireGuard, and UDPspeeder:

| Component | Role |
|---|---|
| `sbfd.py` | Software BFD-equivalent UDP liveness daemon — per-link up/down, RTT, loss |
| `sbfd_ctl.py` | Failover controller — full-redundancy vs primary/backup, egress steering, drives FEC, serves the `:8081` status UI |
| `fec_control.py` | Pure adaptive-FEC decision logic (loss → ratio, hysteresis) |
| `udpspeeder_fec.py` | Relay-side adaptive-FEC controller + `/fec` HTTP control |
| `fec_report.py` | Parses UDPspeeder `--report` output into live throughput/overhead stats |
| `ui/` | The `:8081` operator status page |

**Optional: environmental redundancy.** `environ_ctl.py` is an opt-in policy
source that forces full redundancy when on-route precipitation or wildfire smoke
is detected (GPS + Open-Meteo), with a master on/off toggle on the `:8081` UI.
See [`docs/environmental.md`](docs/environmental.md). It is not part of the core
data plane.

## Three ways to use this repo

1. **Run / read the code (this repo).** Quickstart below.
2. **Deploy it** — run `deploy/wizard.sh` (manual, no Claude needed), or open this repo in Claude
   Code and run `/pathfuse-setup` for guided / SSH-assisted setup. Details in `deploy/README.md`.
3. **Understand the architecture** — see [`docs/architecture.md`](docs/architecture.md) and
   [`docs/design-notes.md`](docs/design-notes.md).

## Quickstart

```bash
git clone git@github.com:QuickOK/PathFuse.git && cd PathFuse
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest -q            # full suite should pass

# preview the status UI against synthesized state (no privileges, no network writes):
.venv/bin/python ui-preview.py -l 127.0.0.1:8181   # open http://127.0.0.1:8181/
```

## Connections — any type, any number

Links are pure config; nothing is hard-coded to two WANs or to any technology.
Each link is one entry in the `wans` block of `config/sbfd-ctl.example.json`:

```json
"wans": {
  "fiber":     { "iface": "enp3s0", "session_id": 1, "label": "Fiber 1G" },
  "fiveg":     { "iface": "wwan0",  "session_id": 2, "label": "5G Router" },
  "satellite": { "iface": "eth1",   "session_id": 3, "label": "Satellite" }
},
"policy": { "default_master_wan": "fiber" }
```

- **key** (`fiber`) — your logical name for the link (used in policy + the UI).
- **iface** — the real OS interface (`enp3s0`, `wwan0`, `ppp0`, …).
- **session_id** — a unique small integer; it sets the link's liveness UDP ports
  (`bind_port + session_id`), so the relay must listen on the matching ports.
- **label** — free display text on the `:8081` page.

Add as many as you like; failover, the FEC-driver indicator, the status cards, and
the signal diagram all render whatever links you define.

**Failover policies:** `static_primary` (pin the configured default link),
`dynamic` (pick the best link by RTT/loss with hysteresis), `static_configured`
(operator picks). **Egress modes:** `relay_vpn` (egress via an upstream VPN/overlay at
the relay), `relay_direct` (out the relay's own WAN), `local_direct` (out the local link,
bypassing the relay).

## Requirements

- Python 3 (stdlib only — no third-party Python deps for the daemons).
- The data-plane components it integrates with — **engarde**, **WireGuard**, and
  **UDPspeeder/speederv2** — are separate projects you install yourself. PathFuse
  invokes them as external programs (subprocess / sockets / FIFO); it does not bundle
  them. (The deploy kit includes a helper to fetch/build them.)

## Security / secrets

This repo ships **no secrets and no real infrastructure addresses** — only RFC-reserved
example IPs (`198.51.100.x`, `100.64.0.x`, `192.0.2.x`) and placeholders. Real keys
(WireGuard, UDPspeeder) are generated at setup time and never committed.

## License

MIT — see `LICENSE`. Third-party components (engarde, UDPspeeder, WireGuard) keep their
own licenses and are fetched, not redistributed here.
