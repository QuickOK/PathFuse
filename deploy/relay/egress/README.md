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
| `default … metric 100` (at most one) | this actuator | the *preferred* exit: the upstream the mode selects, only while its probe passes |
| exemption prefixes `via <wan gw>` | this actuator | destinations that always use the relay's own WAN |
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
2. polls the client for the desired mode. Within `grace_s` of the last good
   poll that mode holds; after that `default_mode` applies. Unknown names are
   rejected and counted in `desired_mode_fetch_fail`;
3. installs the mode's upstream as the single preferred default with an atomic
   `ip route replace`, or deletes every preferred default when the mode uses no
   upstream or its upstream is unhealthy (fail open);
4. re-adds any missing exemption route.

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

The dead-man switch runs when a tick crashes, times out or rejects its config.
It deletes every preferred default (fail open). Give it the table name through a
drop-in too (`Environment=EGRESS_TABLE=<table>`), so it still works when the
config cannot be read. Order the service after whatever creates the table and
the upstream interfaces (a drop-in with `After=`).

> **Regression watch:** `journalctl -u relay-egress-watchdog` shows one summary line per tick,
> plus `ROUTE:` and `HEALTH:` lines on every change. Covered by
> `tests/test_relay_egress_watchdog.py`.
