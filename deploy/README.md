# PathFuse deployment kit

Deterministic, per-host deployment of a PathFuse node (`client` or `relay`) from one
values file. No secrets are shipped — `gen-secrets.sh` generates them. Cross-host
coordination and an agent-driven setup come in the wizard (separate); this is the manual path.

> Run each step on the host you're setting up. Do the **relay** first, then the **client**.

## 0. Prerequisites
```bash
deploy/scripts/detect-os.sh        # needs: systemd, python3, nft, wg
deploy/scripts/fetch-deps.sh       # wireguard-tools + build speederv2 + engarde (see notes it prints)
```
You must separately provide three operator-managed pieces (PathFuse integrates with them but
does not ship or manage them):
- **engarde** — the multipath relay tunnel. `fetch-deps.sh` builds the `engarde-client` /
  `engarde-server` binaries, but you install + configure engarde yourself per engarde's own
  docs (its `engarde.yml` and its service). This kit does **not** ship engarde unit files.
- a **management overlay** (any VPN/mesh, e.g. a WireGuard mesh) giving the relay a stable
  address the client can reach for the `/state` (9275) and `/fec` (9276) control endpoints;
- an **egress VPN/overlay at the relay** if you want `relay_vpn` egress mode (otherwise use
  `relay_direct`).

## 1. Install the PathFuse code
Copy the daemons to the install paths from `values.json` (`paths.*`):
```bash
sudo install -D -m0755 sbfd.py            /opt/sbfd/sbfd.py
sudo install -D -m0644 fec_control.py     /opt/sbfd/fec_control.py
sudo install -D -m0644 fec_report.py      /opt/sbfd/fec_report.py
sudo install -D -m0755 udpspeeder_fec.py  /opt/sbfd/udpspeeder_fec.py     # relay
# client also needs sbfd-ctl + the UI:
sudo install -D -m0755 sbfd_ctl.py        /opt/sbfd-ctl/sbfd_ctl.py
sudo install -D -m0644 fec_control.py     /opt/sbfd-ctl/fec_control.py
sudo install -D -m0644 fec_report.py      /opt/sbfd-ctl/fec_report.py
sudo install -D -m0755 environ_ctl.py     /opt/sbfd-ctl/environ_ctl.py   # optional: environmental redundancy (precip+smoke), needs gpsd reachable
sudo install -d /opt/sbfd-ctl/ui && sudo install -m0644 ui/* /opt/sbfd-ctl/ui/
sudo install -D -m0644 notify.py             /opt/sbfd-ctl/notify.py             # required by sbfd-ctl and hotspot_watchdog
sudo install -D -m0755 hotspot_watchdog.py   /opt/sbfd-ctl/hotspot_watchdog.py   # optional: wan1 auto-reboot watchdog
sudo install -D -m0755 maintenance_reboot.py /opt/sbfd-ctl/maintenance_reboot.py # optional: daily maintenance reboot
sudo install -D -m0755 deploy/ntfy-control/ntfy-dispatch /usr/local/sbin/ntfy-dispatch # optional: ntfy reboot-trigger dispatcher
```

## 2. Fill in your values
Copy `deploy/values.example.json` to `deploy/values.json` and edit: `role` (`client`|`relay`),
`relay_public_ip`, the overlay IPs + `overlay_iface`, the `wg` subnet/ips/ports, your `wans`
(any number — `key → {iface, session_id, label}`), and leave `wg.*_pubkey` as placeholders for now.

## 3. Generate secrets on each end, then exchange public keys
```bash
deploy/scripts/gen-secrets.sh           # prints THIS host's WireGuard public key
```
- Put the **relay's** printed pubkey into the **client's** `values.json` `wg.relay_pubkey`,
  and the **client's** printed pubkey into the **relay's** `wg.client_pubkey`.
- Copy `/etc/udpspeeder/key` from one host to the other so the **UDPspeeder key matches**.

## 4. Render + install
```bash
deploy/scripts/install.sh -c deploy/values.json --dry-run    # preview
deploy/scripts/install.sh -c deploy/values.json              # render + place files, daemon-reload
```
`install.sh` renders only your role's files, backs up anything it overwrites (`.bak.<ts>`), and
does **not** auto-start services (the client has a cutover ordering constraint — next step).

On the client, `install.sh` also creates the `spool-notify` group and, if it doesn't already
exist, `/var/spool/spool-notify` (the spool dir the `spool-notify` helper drains from — see
`deploy/spool-notify/README.md`), group-owned `spool-notify` and mode `0770` so the unprivileged
`sbfd-ctl` user can spool into it via its `SupplementaryGroups=` drop-in. This is a real
prerequisite, not just documentation: `maintenance-reboot.service` and `hotspot-watchdog.service`
mount it via a tolerant `ReadWritePaths=-/var/spool/spool-notify` entry so a *missing* dir only
costs a lost notification, but under `ProtectSystem=strict` the sandbox is otherwise read-only —
`spool-notify`'s own `mkdir -p` cannot create the directory from inside the unit, so it must exist
beforehand for notifications to actually work. `install.sh` only creates the directory — it never
chgrp/chmods an existing one, so a re-run leaves an already-correctly-permissioned dir untouched.
The `sbfd-ctl.service.d/notifications.conf` drop-in itself is still a manual step — see
`deploy/spool-notify/README.md`.

On the client, `install.sh` also creates the `pi-notify` group, the `ntfy-ctl` service
user (member of `pi-notify`, no login shell, no home), and `/etc/sudoers.d/ntfy-ctl`
(mode `0440`, validated with `visudo -cf` — a bad sudoers file fails the install rather
than shipping silently) — the pieces `ntfy-control.service` needs for the optional ntfy
reboot-trigger feature (see `deploy/ntfy-control/`). `ntfy-dispatch` itself is code, not
config, so it is installed manually (step 1 above), like the `.py` daemons.

To make the maintenance-reboot notifications carry a **"Reboot now" button**, set
`maintenance.control_topic` (the same hard-to-guess control topic, below) in **your
`values.json`** and re-run `install.sh`. It renders into
`/etc/sbfd-ctl/maintenance.json` as `control_topic`, which `maintenance_reboot.py`
reads to build the button. Do **not** hand-edit the deployed `maintenance.json` —
`install.sh` re-renders and overwrites it on every run, so a hand-edit is lost on the
next deploy (and hand-editing deploy targets is against this kit's rules). An empty
`control_topic` (the example default) means no button, which is the safe default until
you have a control topic. Use the **same topic value** here and in the `topic.conf`
drop-in below — one is what the button POSTs to, the other is what the subscriber
listens on; they must match.

Two pieces remain manual, on-box, hand steps — nothing in this repo creates them:
- **`/etc/pi-notify.auth`** (shell vars `NTFY_BASE`, `NTFY_USER`, `NTFY_PASS`) —
  root-owned; `maintenance_reboot.py`'s "Reboot now" button reads it as root, but
  `ntfy-control.service` runs as the unprivileged `ntfy-ctl` user, so this file must
  additionally be group-owned `pi-notify` and mode `0640` (not the `0600` a
  root-only reader would need) for the subscriber to source it. `ntfy-ctl` reaches
  it via the `pi-notify` supplementary group `install.sh` grants it.
- **`/etc/systemd/system/ntfy-control.service.d/topic.conf`** — copy from
  `deploy/ntfy-control/ntfy-control.service.d/topic.conf.example` and set a real,
  hard-to-guess `CONTROL_TOPIC` (not installed by `install.sh` on purpose — a topic
  is secret-adjacent, chosen per box). Use the **same** topic as
  `maintenance.control_topic` in your `values.json` (above). Then `systemctl daemon-reload`.

## 5. Start services — order matters

**Relay:**
```bash
sudo systemctl enable --now wg-quick@wg0
# bring up YOUR engarde-server (operator-managed; see step 0), then the PathFuse units:
sudo systemctl enable --now udpspeeder-server udpspeeder-fec sbfd
sudo nft -f /etc/nftables.d/pathfuse.nft     # merge the control-port allows into your ruleset
```

**Client (start order):**
```bash
sudo systemctl enable --now wg-quick@wg0
# bring up YOUR engarde-client (operator-managed; see step 0) FIRST, then the FEC client:
sudo systemctl enable --now udpspeeder-client
sudo systemctl enable --now sbfd sbfd-ctl
# optional: wan1 hotspot auto-reboot watchdog
sudo systemctl enable --now hotspot-watchdog
# optional: daily maintenance reboot (enable the TIMER, not the service; needs
# hotspot-watchdog above for its wan1 leg)
sudo systemctl enable --now maintenance-reboot.timer
# optional: ntfy reboot-trigger subscriber — only after creating BOTH
# /etc/pi-notify.auth (group pi-notify, mode 0640) and the
# ntfy-control.service.d/topic.conf drop-in (see step 4 above); otherwise it
# starts with no topic/credentials to subscribe with.
sudo systemctl enable --now ntfy-control.service
```

### Daily maintenance reboot — turning it on
Enabling the timer is only half of it. The timer ticks **hourly** on purpose and the run
self-gates: it exits immediately unless the feature is enabled *and* the current local hour is
the configured one. Two separate switches, both shipped **off/safe** by default:

| switch | where | default | meaning |
|---|---|---|---|
| `maintenance.enabled` | `values.json` → `/etc/sbfd-ctl/config.json` | `false` | boot default for the feature. Turn it on here, or from the UI toggle (which overrides it at runtime). |
| `maintenance.hour` | `values.json` → `/etc/sbfd-ctl/config.json` | `3` | local hour (0–23) the reboot runs. Also settable from the UI. |
| `maintenance.dry_run` | `values.json` → `/etc/sbfd-ctl/maintenance.json` | `true` | **`true` = no WAN is actually rebooted** — it only logs/notifies what it *would* do. |

Leave `dry_run: true` for at least one cycle and read the notifications to confirm the sequence
looks right. Only then set `"dry_run": false` in `values.json`'s `maintenance` block, re-run
`install.sh`, and `systemctl restart maintenance-reboot.timer`. Until you do, **no real reboot
ever happens** — a `dry_run` node with the timer enabled is a no-op by design.

The run also takes an exclusive `flock` on `lock_path` (`/run/sbfd-ctl/maintenance.lock`, already
covered by the unit's `ReadWritePaths=/run/sbfd-ctl`) so an hourly tick can never overlap a run
still in progress.

### If your host uses a different notifier spool
These units notify by invoking `spool-notify`, which writes into `/var/spool/spool-notify`. If
your host already runs a *different* notifier (its own auth file and spool directory under
another name), do **not** edit the tracked unit files — `install.sh` overwrites them. Instead add
a host-local drop-in per unit, which both points the helper at your paths and re-grants write
access (`ProtectSystem=strict` makes everything outside `ReadWritePaths=` read-only, so a spool
directory the unit is not granted becomes silently unwritable and alerts stop):

```ini
# /etc/systemd/system/<unit>.service.d/notifications.conf
[Service]
Environment=NOTIFY_AUTH=/etc/<your-notifier>.auth
Environment=NOTIFY_SPOOL=/var/spool/<your-notifier>
ReadWritePaths=/var/spool/<your-notifier>
```
Apply it to **`maintenance-reboot.service`** and **`hotspot-watchdog.service`** (and, for the
unprivileged `sbfd-ctl.service`, add `SupplementaryGroups=<your-notifier-group>` too — it needs
group write access, the root units do not). Then `systemctl daemon-reload`. Drop-ins survive
`install.sh`, which only rewrites the main unit file.
> wg0's Endpoint is the local udpspeeder-client (`127.0.0.1:59411`) from the start in this kit.
> Start `udpspeeder-client` only **after** engarde-client is up: engarde-client latches its
> single return path to whatever last sent to its input port, so `udpspeeder-client` must
> start after engarde-client, never before.

## 6. Verify
```bash
deploy/scripts/healthcheck.sh -c deploy/values.json
```
Open the client's `:8081` page to confirm WAN links, failover, and FEC status.

## Rollback
Each rendered file has a `.bak.<ts>` beside it; restore it and `systemctl restart` the unit.
To remove PathFuse, `systemctl disable --now` the units and delete the installed files.

## Notes
- Secrets (`/etc/wireguard/wg0.key`, `/etc/udpspeeder/key`) are generated, mode 0600, and never
  committed. The WireGuard private key loads via the unit's `PostUp` — it is never written into
  `wg0.conf`.
- `engarde`, `speederv2`, and `wireguard-tools` keep their own licenses and are fetched/built,
  not redistributed by PathFuse.
