# PathFuse architecture

PathFuse turns several unreliable WAN uplinks into one dependable connection. It does this with
**link liveness**, **automatic failover**, **cross-link packet redundancy**, and **adaptive forward
error correction (FEC)** — layered over an existing multipath relay tunnel (engarde), WireGuard, and
UDPspeeder. It trades bandwidth for reliability and low loss; it does **not** sum link bandwidth
(this is redundancy + failover + FEC, not bandwidth aggregation).

There are two roles:

- **client** — the edge node with multiple WAN uplinks (cellular, satellite, fiber, …).
- **relay** — a host with a stable public address that the client's links converge on.

```
        client (edge)                                   relay (public)
   ┌───────────────────────┐                       ┌───────────────────────┐
   │ apps → wg0            │   WAN A ───────────┐   │  engarde-server       │
   │   ↓ udpspeeder-client │   WAN B ───────────┼──▶│   ↓ udpspeeder-server │
   │   ↓ engarde-client    │   WAN C ───────────┘   │   ↓ wg0 → egress      │
   └───────────────────────┘   (any number)         └───────────────────────┘
            ▲   sbfd liveness + sbfd-ctl failover/FEC          ▲ sbfd + udpspeeder-fec
            └──────────── management overlay (control) ────────┘
```

## Components

| Component | Role |
|---|---|
| `sbfd.py` | Software BFD-equivalent UDP liveness daemon — one session per WAN; reports up/down, RTT, loss. |
| `sbfd_ctl.py` | Failover controller (client). Chooses full-redundancy vs primary/backup, steers egress, drives the client→relay FEC, and serves the `:8081` operator UI. |
| `fec_control.py` | Pure adaptive-FEC decision logic (loss → ratio table + hysteresis). Shared by both ends. |
| `udpspeeder_fec.py` | Relay-side adaptive-FEC controller for the relay→client direction; serves a `/fec` HTTP control/telemetry endpoint. |
| `fec_report.py` | Parses UDPspeeder `--report` output into live throughput / overhead wire stats. |
| `ui/` | The `:8081` status page (links, failover state, FEC per direction). |

## Data plane

Application traffic rides an inner WireGuard tunnel (`wg0`). Each direction is FEC-encoded by its
sender and carried across the WAN links by the engarde multipath relay:

```
client wg0 ─▶ :59411 udpspeeder-client (FEC encode) ─▶ :59401 engarde-client
            ─▶ [WAN A | WAN B | …] ─▶ engarde-server :59402
            ─▶ :59412 udpspeeder-server (FEC decode) ─▶ :51820 relay wg0 ─▶ egress
```

The reverse direction is symmetric (relay → client). FEC is applied by the **sender** and recovered
at the **decoder**, so each direction's redundancy is controlled independently.

In **full-redundancy** mode engarde sends every packet on all UP links (best-of-N latency, loss
masked). In **master/backup** mode it uses one link and fails over when it dies.

## Control plane

The client's `sbfd-ctl` is the brain:

- reads its **local** sbfd state (`/run/sbfd/state.json`) and the relay's sbfd `/state` over the
  management overlay;
- decides which links are active (failover policy) and applies it (nftables + default route);
- drives the **client→relay** FEC ratio, and **reconciles** the desired FEC on/off to the relay by
  POSTing the relay's `/fec` endpoint;
- reads the relay's `/fec` for the **relay→client** direction's status;
- publishes a single `/api/state` JSON that the `:8081` UI renders.

The management overlay is any stable network (e.g. a small WireGuard mesh) you provide so the client
can reach the relay's control endpoints; it is separate from the data plane.

### Environmental auto-override (optional)

An optional policy source (`environ_ctl`) can ask `sbfd-ctl` to raise the link to
full redundancy ahead of an on-route hazard (precipitation, wildfire smoke). It
writes a TTL'd `auto_override.json`; `sbfd-ctl` reads it each loop and, when the
operator's environmental toggle is on, raises the mode to `full` (raise-only —
never forces master/backup). See [`environmental.md`](environmental.md).

## Failover

- **Modes:** `full` (redundancy — all UP links) · `master_backup` (one master, fail over).
- **Master policies:** `static_primary` (pin the configured primary link) · `dynamic` (pick the best
  link by EWMA RTT/loss with hysteresis to avoid flapping) · `static_configured` (operator picks).
- **Egress modes:** `relay_vpn` (out via an upstream VPN/overlay at the relay) · `relay_direct` (out
  the relay's own WAN) · `local_direct` (out the local link, bypassing the relay).

## Adaptive FEC

Redundancy ramps with measured loss and backs off when links are clean:

| Active-link loss | FEC ratio |
|---|---|
| < 0.5 % | `8:0` (off — no parity) |
| 0.5–2 % | `8:2` |
| 2–5 % | `8:4` |
| 5–10 % | `8:6` |
| > 10 % | `8:8` |

- **Hysteresis:** raise only after N consecutive higher targets; lower only after a hold time —
  avoids flapping the ratio.
- **Mode-aware backoff:** in full-redundancy with enough UP links, engarde already duplicates, so
  FEC drops to the off tier (`8:0`).
- **Two directions, independent:** client→relay is driven by `sbfd-ctl`; relay→client by
  `udpspeeder-fec`. A single operator switch can force both to `8:0` (disabled) without stopping the
  tunnel.
- **Receiver-measured loss:** sbfd loss is RX-side, so each leg is driven by the loss the *far end*
  measures — the client leg by the relay-fetched `/state` snapshot, the relay leg by
  `client_loss_pct` pushed in the client's `POST /fec` (fallback: own-side loss when the far-end
  view is stale, e.g. during a rolling upgrade).
- **Wire stats:** parsed from UDPspeeder `--report` — throughput and parity overhead per direction
  (recovery counts are not emitted by the report and are not shown).

## Port map (defaults)

| Port | Purpose |
|---|---|
| `8081/tcp` | operator status UI (client) |
| `9275/tcp` | sbfd `/state` (over the management overlay) |
| `9276/tcp` | relay FEC `/fec` control + telemetry (over the management overlay) |
| `3784 + session_id /udp` | sbfd liveness, one port per WAN session |
| `59401 / 59411 / 59412 /udp` | udpspeeder engarde-in / client / server (loopback) |
| `59402/udp` | engarde server listen |
| `51820/udp` | inner WireGuard (`wg0`, loopback to udpspeeder) |

Addresses in this repo's examples use RFC-reserved ranges (`198.51.100.x`, `192.0.2.x`,
`203.0.113.x`, `100.64.x`); replace them with your own in `deploy/values.json`.

## See also
- Run / read the code: top-level `README.md`.
- Deploy: `deploy/README.md` (manual) or `/pathfuse-setup` in Claude Code.
- Design rationale: `docs/design-notes.md`.
