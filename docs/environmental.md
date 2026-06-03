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

All signals use keyless Open-Meteo HTTP APIs and only stdlib Python.

| Signal | Source | Field | Units / values |
|---|---|---|---|
| `precip` | Open-Meteo Forecast | `current.precipitation` | mm |
| `weather` | Open-Meteo Forecast | `current.weather_code` | WMO code (categorical) |
| `smoke` | Open-Meteo Air Quality | `current.pm2_5` | µg/m³ |

Each poll queries both the client's current GPS position and a course-projected
look-ahead point. The look-ahead direction comes from the client's gpsd
course-over-ground value; every signal uses the travel heading (not wind direction).

Each signal has its own **hysteresis band** (`on_thresh` > `off_thresh`) and a
**debounce count** (`wet_confirm` to activate, `dry_confirm` to deactivate). The daemon
sets `force_full: true` if **any** signal is in hazard state (OR logic); `reason` names
which signals triggered.

The thresholds in `config/environmental.example.json` are starting guesses — tune
`precip` against the precipitation rate at which your rain-sensitive link (e.g. a
satellite uplink) degrades, and `smoke` against the PM2.5 level you want to react to.

### Categorical signals (`hazard_codes`)

`precip` and `smoke` are magnitude signals: the controller compares a numeric value
against `on_thresh`/`off_thresh`. `weather` is **categorical** — `weather_code` is a WMO
enum, not a magnitude (fog `45` is not "more hazardous" than overcast `3`). A signal that
sets `hazard_codes` is evaluated by **set membership** instead: each polled code maps to
`1.0` if it is in `hazard_codes`, else `0.0`, before the controller sees it. Run such a
signal with `on_thresh: 1.0` / `off_thresh: 0.0` so the binary result never lands in the
hysteresis band — debounce (`wet_confirm`/`dry_confirm`) still governs flap protection.

The default `weather` codes are `[80, 81, 82, 95, 96, 99]`: rain showers (80–82) and
thunderstorms with/without hail (95/96/99). This targets convective cells, which carry
high cloud liquid water and can degrade a Ku/Ka satellite uplink **even with no rain at
the dish** — see [`cloud-coverage.md`](../cloud-coverage.md). `dry_confirm: 3` holds the
override as a cell drifts across the slant path rather than dropping it the moment the
code clears. To also react to non-convective thunderstorm-adjacent codes, add them to the
list; to react only to thunderstorms, drop the shower codes (80–82).

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
