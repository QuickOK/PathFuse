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
sudo install -d /opt/sbfd-ctl/ui && sudo install -m0644 ui/* /opt/sbfd-ctl/ui/
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
```
> wg0's Endpoint is the local udpspeeder-client (`127.0.0.1:59411`) from the start in this kit.
> Start `udpspeeder-client` only **after** engarde-client is up — engarde-client latches its
> single return path to whatever last sent to its input port, so order matters.
> Why the order: engarde-client latches its single return path to whatever last sent to its
> input port, so `udpspeeder-client` must start after `engarde-client`, never before.

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
