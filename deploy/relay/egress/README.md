# relay-egress-watchdog: the relay-side egress actuator

The client (`sbfd-ctl`) only **publishes** its desired egress mode at
`:8081/api/desired_egress`. The relay decides where the client's decrypted
traffic actually exits. This directory holds the relay component that enacts the
mode. It is vendor-neutral: a deployment names its upstreams and addresses in its
own config file.

## Model

One routing table carries the client subnet's egress (an `ip rule from <client
subnet> lookup <table>` sends it there). In that table:

| Route | Owner | Purpose |
|---|---|---|
| exemption prefixes `via <wan gw>` | this actuator | destinations that always use the relay's own WAN |
| `default … metric 100` (at most one) | this actuator | the *preferred* exit: the upstream the mode selects, only while its probe passes |
| `default via <wan gw> metric 200` | your setup | fail-open path when no preferred route exists |

An **upstream** is a named egress path: an upstream VPN behind a veth, a
WireGuard tunnel to a cloud exit, and so on. Each has a route, an optional link to
check, and an HTTP probe whose body must match a regex.
- a `netns` probe runs inside a namespace;
- a `source` probe binds a source address that your own `ip rule` routes into the upstream.

Each 10 s tick:
1. probes every upstream in parallel; each has its own hysteresis (unhealthy
   after `fail_threshold` straight failures, healthy after `pass_threshold`
   passes, the first pass ever counts at once);
2. re-adds any missing exemption route (applied first, so exempt prefixes never
   briefly exit via an upstream);
3. polls the client for the desired mode. Within `grace_s` of the last good
   poll that mode holds; after that `default_mode` applies. Unknown names are
   rejected and counted in `desired_mode_fetch_fail`;
4. installs the mode's upstream as the single preferred default with an atomic
   `ip route replace`, or deletes every preferred default when the mode uses no
   upstream or its upstream is unhealthy (fail open).

`dry_run: true` logs `DRY-RUN would run: …` and changes nothing. Use it to
shadow-run beside an existing actuator before taking over.

## Egress-mode vocabulary (the contract that matters)

| Client mode | Relay behaviour |
|---|---|
| `relay_vpn` | preferred route = the upstream mapped in `mode_upstreams` (an upstream VPN) |
| `relay_backbone` | preferred route = the upstream mapped in `mode_upstreams` (a cloud-backbone exit) |
| `relay_direct` | no preferred route: exits the relay's own WAN |
| `local_direct` | no preferred route (the client steers this traffic locally) |

If the relay's vocabulary drifts from what the client publishes, every poll is
rejected as `invalid mode`, the relay falls back to `default_mode`, and the
requested mode **silently never takes effect**. `MODE_ALIASES` absorbs legacy
names. Watch for `fetch_fail=` climbing in the journal.

## Config

`/etc/relay-egress-watchdog/config.json`; see `config/relay-egress.example.json`.
The actuator re-reads it every tick, so an edit takes effect within 10 s.

**Key fields:**
- `table`: the PBR table name (no default; required).
- `preferred_metric`: the metric for the preferred route (default 100). Must be a positive integer.
- `dry_run`: if `true`, logs `DRY-RUN would run: …` without changing routes (default `false`).
- `client.control_url`: the client's desired-mode endpoint (e.g., `http://100.64.0.2:8081/api/desired_egress`). If set to `""` (empty string), polling is disabled and `default_mode` always applies (no HTTP call).
- `exempt.prefixes`: list of CIDR prefixes that always exit via the relay's own WAN. Include your site's private address ranges (e.g., the upstream VPN's subnet, the backbone exit's source space).

Removing a prefix from `exempt.prefixes` does not delete its route: run
`ip route del <prefix> table <table>` yourself.

## Install

```bash
sudo install -m0755 deploy/relay/egress/relay-egress-watchdog /usr/local/sbin/
sudo install -m0755 deploy/relay/egress/relay-egress-deadman  /usr/local/sbin/
sudo install -m0644 deploy/relay/egress/systemd/relay-egress-* /etc/systemd/system/
sudo install -D -m0644 config/relay-egress.example.json /etc/relay-egress-watchdog/config.json  # then edit
sudo systemctl daemon-reload && sudo systemctl enable --now relay-egress-watchdog.timer
```

**The dead-man switch** runs when a tick crashes, times out, or rejects its config (exit 1 on config failure or table read failure; exit 2 on config error). It deletes every preferred default (fail open). Suppress the dead-man during a shadow run by setting `dry_run: true` in the config—the dead-man then skips execution and exits 0, leaving the live actuator's routes untouched. If the config cannot be read during shadow run, the dead-man has no way to see the dry_run flag; keep the config readable, or do not wire the OnFailure hook until the shadow run is complete.

**Relay drop-in examples** (create at `/etc/systemd/system/relay-egress-watchdog.{service,timer}.d/relay-egress-local.conf`):

Tell the dead-man the table name when the config is unreadable:
```ini
[Service]
Environment=EGRESS_TABLE=egress
```

Order the service after whatever creates the table and upstream interfaces:
```ini
[Unit]
After=network-online.target some-veth-setup.service
```

> **Regression watch:** `journalctl -u relay-egress-watchdog` shows one summary line per tick,
> plus `ROUTE:` and `HEALTH:` lines on every change. Covered by
> `tests/test_relay_egress_watchdog.py`.
