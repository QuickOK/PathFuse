---
name: pathfuse-setup
description: Guided setup of a PathFuse node (relay or client) on this machine, and optionally the other end over SSH. Use ONLY when the operator explicitly asks to set up or deploy PathFuse.
disable-model-invocation: true
---

# PathFuse guided setup

Help the operator deploy a PathFuse node from this repo (PathFuse bonds multiple WAN uplinks with
failover + adaptive FEC over a relay). Deterministic mechanics live in `deploy/scripts/` — use them,
don't reinvent them.

## Rules (non-negotiable)
- **Confirm before every privileged or remote command.** Show the exact command; wait for a yes.
- **Never print, store, or transmit private keys.** `deploy/scripts/gen-secrets.sh` creates them
  (mode 0600, never committed). Exchange only PUBLIC keys + the shared UDPspeeder key, via the
  operator's own SSH/channel.
- Prefer `--dry-run` first for fetch-deps/gen-secrets/install. Stop on any healthcheck failure.
- Use the operator's existing SSH for the remote end; never create keys or weaken auth.

## Step 0 — scope + mode
Ask which end(s): `relay`, `client`, or `both` (this host + the other over SSH); and the mode:
- **assisted** (recommended): you RUN `deploy/scripts/*` and orchestrate/troubleshoot.
- **native**: you perform each step yourself (still using `render.py`/`gen-secrets.sh` for secrets),
  narrating and confirming each action.

## Step 1 — prerequisites
`deploy/scripts/detect-os.sh` (needs systemd/python3/nft/wg). Offer `deploy/scripts/fetch-deps.sh`
(builds wireguard-tools/speederv2/engarde binaries). engarde + the management overlay + egress VPN
are operator-managed — see `deploy/README.md`.

## Step 2 — values
Copy `deploy/values.example.json` -> `deploy/values.json`; fill role, `relay_public_ip`, overlay IPs
+ `overlay_iface`, `wans` (any number: key->{iface,session_id,label}); leave `wg.*_pubkey` for step 3.

## Step 3 — secrets (per end) + exchange
`deploy/scripts/gen-secrets.sh` on each end; it prints that host's WG PUBLIC key. Put the relay's
pubkey in the client's `values.json` `wg.relay_pubkey` and vice-versa; copy `/etc/udpspeeder/key` so
it MATCHES on both ends. Confirm each cross-host copy.

## Step 4 — render + install
`deploy/scripts/install.sh -c deploy/values.json --dry-run` (review), then without `--dry-run`
(confirm). Renders only this role's files, backs up overwrites, does NOT auto-start.

## Step 5 — start (order matters) + verify
Follow `deploy/README.md` step 5 exactly (relay first; on the client start `udpspeeder-client` only
after engarde-client is up). Then `deploy/scripts/healthcheck.sh -c deploy/values.json`; open the
client `:8081` page. On any failure, diagnose from unit logs — do not press on.

## Both-over-SSH
Set up the local end fully, then `ssh <target>` and repeat steps 1–5 there (ensure the repo is
present on the remote; clone it with confirmation if needed), then exchange the two ends' WG public
keys + shared UDPspeeder key. Confirm every remote command.
