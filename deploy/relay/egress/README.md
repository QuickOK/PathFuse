# relay-egress-watchdog — relay-side egress actuator (reference)

This is a vendor-neutral **reference** for the relay component that *enacts* a
client's egress mode. The client (`sbfd-ctl`) only **publishes** the desired mode
at `:8081/api/desired_egress`; the relay decides where the client's decrypted
traffic actually exits. A real deployment adapts this to its specific upstream
VPN/overlay and WAN.

`relay-egress-watchdog` is a `Type=oneshot` fired by a 10s timer. Each tick it:

1. Health-probes the upstream-VPN egress.
2. Polls the client's `EGRESS_CLIENT_CONTROL_URL` for the desired egress mode.
3. Adds/withdraws the metric-100 *upstream-VPN* default in the egress PBR table,
   so the client subnet exits via either the upstream VPN or the relay's own WAN
   (a lower-priority WAN default stays in the table to fall through to).

Config comes from an EnvironmentFile (`EGRESS_*` vars); the script hardcodes no
real infrastructure addresses (defaults are RFC-5737/6598 examples).

## Egress-mode vocabulary (the contract that matters)

The actuator validates against the **canonical** vocabulary that `sbfd-ctl`
publishes, and only `relay_vpn` keeps the upstream route in place:

| Client mode (`/api/desired_egress`) | Effect on the egress PBR table |
|---|---|
| `relay_vpn`    | keep the metric-100 upstream-VPN default (exit via the upstream VPN) |
| `relay_direct` | withdraw it → fall through to the relay's own WAN default |
| `local_direct` | withdraw it (egress is steered on the client side) |

If an actuator's internal names drift from what the client publishes, every poll
is rejected as `invalid mode`, the watchdog falls back to
`EGRESS_DESIRED_MODE_DEFAULT`, and the requested mode **silently never takes
effect**. `MODE_ALIASES` exists to absorb alternate/legacy names onto the
canonical set; keep it in sync with the client.

> **Regression watch:** symptom of vocabulary drift is `state.json` showing
> `desired_mode_fetch_fail` climbing while `desired_mode` stays pinned to the
> default. Covered by `tests/test_relay_egress_watchdog.py`.

## Deploy

Install to `/usr/local/sbin/relay-egress-watchdog` (mode 0755, root:root) and
drive it from a 10s `.timer`. It is intentionally **not** auto-wired by the
PathFuse deploy kit — the upstream-VPN integration is deployment-specific.
