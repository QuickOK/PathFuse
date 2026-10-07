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

Each 10 s tick first writes the dead-man record (see
[The dead-man switch](#the-dead-man-switch)), then:
1. probes every upstream in parallel; each has its own hysteresis (unhealthy
   after `fail_threshold` straight failures, healthy after `pass_threshold`
   passes, the first pass ever counts at once). A `link` that is missing (`ip`
   says it does not exist) or not admin-UP is a hard failure: that upstream is
   unhealthy at once, without waiting for `fail_threshold`. An `ip` that cannot
   tell (it fails for another reason, hangs, or prints something unreadable) is
   an ordinary failure;
2. polls the client for the desired mode. Within `grace_s` of the last good
   poll that mode holds; after that `default_mode` applies. Unknown names are
   rejected and counted in `desired_mode_fetch_fail`;
3. re-adds any missing exemption route. The exemptions go first, so exempt
   prefixes never briefly exit via an upstream, and they gate step 4: if one
   cannot be added, the tick logs an `ERROR` naming its prefix and deletes every
   preferred default (fail open), whatever the mode and health say. The tick
   still exits 0. The next tick retries the exemption, and once it is in place
   step 4 installs the preferred route again;
4. installs the mode's upstream as the single preferred default with an atomic
   `ip route replace`, or deletes every preferred default when the mode uses no
   upstream or its upstream is unhealthy (fail open). A live tick that could not
   write the dead-man record also deletes every preferred default, logs an `ERROR`
   and exits 0, as for a missing exemption (see
   [The dead-man switch](#the-dead-man-switch)).

`dry_run: true` logs `DRY-RUN would run: …` and changes nothing. Use it to
shadow-run beside an existing actuator before taking over. A shadow run is safe
even if its config breaks (see [Shadow runs](#shadow-runs)).

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
- `dry_run`: if `true`, logs `DRY-RUN would run: …` without changing routes. The example ships with `true` (safe by default); set to `false` to go live.
- `client.control_url`: the client's desired-mode endpoint (e.g., `http://100.64.0.2:8081/api/desired_egress`). If set to `""` (empty string), polling is disabled and `default_mode` always applies (no HTTP call).
- Timeouts, in seconds: `upstreams.<name>.probe.timeout_s` (default 5, at most 8), `client.fetch_timeout_s` (default 1, at most 5) and `client.bootstrap_timeout_s` (default 5, at most 8; it replaces the fetch timeout while there is no last-known mode, as after a reboot). A config above a cap is rejected (exit 2). The caps are sized to keep a tick inside the unit's `TimeoutStartSec=35`, past which systemd kills the tick and the dead-man switch fires. Each upstream's probe is a link check (up to 2 s) and then curl (up to `timeout_s`, killed 3 s later should it overrun); the upstreams are probed in parallel, and the poll follows. The poll stops waiting at its timeout, whether the request is still looking up the client's name, connecting or reading a slow reply. Then each `ip` command of the route work may take up to 5 s: the table read, and each route change. So the slowest tick that changes one route waits 2 + (8 + 3) + 8 + 5 + 5 = 31 s, leaving 4 s. A tick that also re-adds missing exemption routes, as after a reboot, or clears duplicate preferred defaults before its replace, runs one more `ip` per missing prefix or cleared default, so several stalled `ip` commands can still take it past the unit's limit; it then fails open through the dead-man switch, and the next tick recovers.
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

**Relay drop-in examples:**

Name the egress table for the dead-man switch, for when neither the config nor the
dead-man record can (as after a reboot, before the first tick whose config validates).
Add it once the actuator is live, not during a shadow run (see [Shadow runs](#shadow-runs)).
Create it at `/etc/systemd/system/relay-egress-deadman.service.d/10-local.conf`:
```ini
[Service]
Environment=EGRESS_TABLE=egress
```

Order the watchdog after whatever creates the table and upstream interfaces (create at `/etc/systemd/system/relay-egress-watchdog.service.d/10-local.conf`):
```ini
[Unit]
After=network-online.target some-veth-setup.service
```

After creating both drop-ins, make systemd read them:
```bash
sudo systemctl daemon-reload
```

## The dead-man switch

`relay-egress-deadman` runs when a tick crashes, times out, exits 1 (table read failure or preferred-route command failure), or exits 2 (config error). It deletes every preferred default (fail open). Four cases do NOT trigger it, because in each the tick deletes the preferred default itself and exits 0:
- an unhealthy upstream;
- an upstream `link` that is missing or down, which makes the upstream unhealthy on the same tick. So the fail-open drill, stopping the tunnel that an upstream's `link` names (`systemctl stop wg-quick@<tunnel>`), normally logs no `ERROR` and runs no dead-man. A stop that lands mid-tick can still give one `ERROR` tick and one dead-man run: if the tunnel goes after the tick has checked its link but before it reads the table, the kernel has already dropped the route with the device, and the tick's attempt to put it back fails on the missing device (exit 1). That window is only the tick's probe and poll time. The next tick finds the link missing and fails open with no `ERROR`;
- an exemption route that cannot be added (the tick logs an `ERROR`; see step 3 above);
- a dead-man record that a live tick cannot write (the tick logs an `ERROR`; see the record below).

**The record.** Every tick, dry runs included, writes `/run/relay-egress-watchdog/deadman.json` as soon as its config has validated, before it touches a route, so the record stands even if the tick then fails:
```json
{"dry_run": false, "preferred_metric": 100, "table": "egress", "written_at": 1759650000.0}
```
The tick writes a temporary file and renames it over the record, so the dead-man never reads half of one. A write that fails logs an `ERROR` and does not fail the tick. It does cost a live tick its preferred route, because **a live preferred route stands only after the tick that keeps it has written a current record**: the tick deletes every preferred default (fail open), logs a second `ERROR` naming the record, and exits 0, as for a missing exemption. A dry run changes no route either way. Without that rule a stale record could outlive the route it describes: should the config then break, the dead-man would act on it, and one a shadow run left (`"dry_run": true`) would make it skip. The path is fixed (in the unit's `RuntimeDirectory`), so the dead-man finds the record without the config, wherever `state_path` points.

**How the dead-man finds the table.** It takes the first of these that answers:
1. **The config**, if it parses as a JSON object. `"dry_run": true` makes the dead-man skip (exit 0). Otherwise its `table`, if a non-empty string, is used with its `preferred_metric` (100 unless that is an integer >= 1).
2. **The record**, if it parses. `"dry_run": true` there means the last tick was a dry run, so the dead-man skips (exit 0). Otherwise its `table` is used with its `preferred_metric`: the table and the metric of the routes the actuator last installed, even when the config that set them is now broken or used a metric other than 100.
3. **`$EGRESS_TABLE`**, if set and not empty (the drop-in above), with metric 100.

With none of the three, the dead-man deletes nothing, names all three in its message and exits 1. Only a real `true` is a dry run, in the config or the record: the actuator rejects any other `dry_run` value rather than read it as one. The dead-man also exits 1 when a delete fails for any reason but "nothing left to delete" (an empty error and a signal included), when `ip` hangs (5 s) or cannot run, or when a delete still succeeds after 16 have (it gives up there, and preferred defaults may remain). It prints why it stopped, then how many preferred defaults it removed.

### Shadow runs

Run the new actuator with `dry_run: true` beside the existing one before taking over. The preferred default in the table then belongs to the existing actuator, and the dead-man leaves it alone: through the config's `dry_run` while the config parses, and through the record should an edit break the config, since every dry-run tick records `"dry_run": true`. So a shadow run is safe even if its config breaks. Going live is safe too: the dry-run record stays until a live tick replaces it, and a live tick that cannot replace it installs no preferred route (see the record above), so that record never makes the dead-man skip while a live route stands.

One gap remains: `$EGRESS_TABLE` knows nothing of dry runs. A shadow run whose config is broken from its first tick after a boot has written no record, so with the `EGRESS_TABLE` drop-in in place the dead-man would delete the existing actuator's route. Add that drop-in when the actuator goes live.

> **Regression watch:** `journalctl -u relay-egress-watchdog` shows one summary line per tick,
> plus `ROUTE:` and `HEALTH:` lines on every change. Covered by
> `tests/test_relay_egress_watchdog.py`.
