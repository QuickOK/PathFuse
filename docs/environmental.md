# Environmental redundancy (optional)

`environ_ctl.py` is an optional, opt-in policy source for mobile / moving-vehicle deployments.
It samples on-route weather and air-quality signals and, when a hazard is detected,
asks `sbfd-ctl` to raise the link to full redundancy. PathFuse's core is deliberately
generic — it has no GPS or weather coupling. This daemon is a separate, independently
toggled layer; the core data plane and failover controller work identically without it.

## How it drives failover

`environ_ctl` writes a heartbeat file (default `/run/sbfd-ctl/auto_override.json`):

```json
{ "force_full": true, "source": "environ_ctl", "reason": "precip ahead; smoke", "set_ts": 1717300000.0 }
```

- **`force_full`** — the only actuated field. `true` asks `sbfd-ctl` to raise to full
  redundancy; `false` withdraws the request.
- **`reason`** — a human-readable string shown on the `:8081` UI when the override is
  active.
- **`set_ts`** — Unix timestamp of the last write. `sbfd-ctl` ignores the record once
  it is older than `environmental.auto_override.ttl_s` (default **180 s**). A dead or
  stopped `environ_ctl` process therefore withdraws the override automatically.

## Precedence

When the operator's environmental toggle is **on** and `sbfd-ctl` reads a fresh
`force_full: true`, it raises the effective mode to `full`. Otherwise the
operator-configured mode or system default applies. This is **raise-only**: the
daemon can never force the mode below `full`, and it never forces `master` or `backup`
directly. The toggle is the operator's lever — turn it off to stay in `master/backup`
mode during a hazard (e.g. to preserve bandwidth when the links are healthy enough).
The toggle is on the `:8081` UI next to the Egress selector.

## Signals

Both signals use keyless Open-Meteo HTTP APIs and only stdlib Python.

| Signal | Source | Field | Units |
|---|---|---|---|
| `precip` | Open-Meteo Forecast | `current.precipitation` | mm |
| `smoke` | Open-Meteo Air Quality | `current.pm2_5` | µg/m³ |

Each poll queries both the client's current GPS position and a course-projected
look-ahead point. The look-ahead direction comes from the client's gpsd
course-over-ground value; smoke uses the travel heading (not wind direction).

Each signal has its own **hysteresis band** (`on_thresh` > `off_thresh`) and a
**debounce count** (`wet_confirm` to activate, `dry_confirm` to deactivate). The daemon
sets `force_full: true` if **any** signal is in hazard state (OR logic); `reason` names
which signals triggered.

The thresholds in `config/environmental.example.json` are starting guesses — tune
`precip` against the precipitation rate at which your rain-sensitive link (e.g. a
satellite uplink) degrades, and `smoke` against the PM2.5 level you want to react to.

## Fail-safe

Two independent layers protect against a malfunctioning `environ_ctl`:

1. **Per-signal fetch errors** — a failed HTTP fetch holds that signal's last known
   state rather than defaulting to hazard. If **no** signal can be evaluated for
   `max_stale_s` seconds (default 600 s), `environ_ctl` explicitly writes
   `force_full: false`, withdrawing any active override.
2. **TTL** — if the process dies without writing a final record, `sbfd-ctl` ignores
   the stale file once it is older than `ttl_s` (default 180 s). No manual cleanup
   is needed.

## Run

```bash
environ_ctl.py -c config/environmental.json
```

Requires `gpsd` running and pointed at the GPS device. Deploy via the systemd
unit template at `deploy/templates/systemd/environ-ctl.service.tmpl`.
