# Maintaining PathFuse

**This repo is the single source of truth.** Develop fixes and features *here*, in the generic
vocabulary (`relay`/`client`, `static_primary`, `relay_vpn`, RFC-reserved example IPs). Your live
deployment consumes this repo via the deploy kit — it does not have its own diverging copy.

## Day-to-day: change → verify → push
```bash
cd /path/to/PathFuse
# ... edit code / templates / docs ...
.venv/bin/python -m pytest -q          # (first time: python3 -m venv .venv && .venv/bin/pip install pytest)
scripts/preflight.sh                    # tests + render check + sanitization + secret scan
git add -A && git commit -m "fix: ..."  # or feat: / docs: / refactor: / test:
git push origin main
```
The **pre-push hook** runs `preflight.sh` automatically and **blocks the push** if anything fails.
Enable it once per clone:
```bash
git config core.hooksPath scripts/hooks
```

## The rules the gate enforces (keep the repo public-safe)
- **Generic vocabulary only.** No deployment-specific names (provider / ISP / host / hardware
  terms). The preflight gate rejects them — see the exact pattern in `scripts/preflight.sh`. Use
  `relay` / `client` / generic labels. Kept names: `sbfd`, `engarde`, `udpspeeder`,
  `wireguard`/`wg`, `fec`.
- **Only RFC-reserved example IPs:** `192.0.2.x`, `198.51.100.x`, `203.0.113.x` (RFC 5737),
  `100.64.x` (RFC 6598), `10.x`, `127.x`. Never a real public IP.
- **No secrets, ever.** Keys are generated at deploy time (`deploy/scripts/gen-secrets.sh`).

## Deploying a change to your live boxes
Your real, host-specific settings live in **`deploy/values.json`** — gitignored, never committed
(real IPs, interfaces, the exchanged WG public keys). To roll an update to a live node:
```bash
git pull                                                  # get the new PathFuse
deploy/scripts/install.sh -c deploy/values.json --dry-run # preview
deploy/scripts/install.sh -c deploy/values.json           # render + place (backs up, no auto-start)
# restart per deploy/README.md step 5 (relay first; client udpspeeder after engarde-client)
deploy/scripts/healthcheck.sh -c deploy/values.json
```
If you changed a daemon (`sbfd.py`, `sbfd_ctl.py`, `udpspeeder_fec.py`, `fec_*`), also copy the new
file to its install path (`/opt/sbfd`, `/opt/sbfd-ctl`) per `deploy/README.md` step 1, then restart.

## Releasing
Optional: tag releases (`git tag vX.Y && git push origin vX.Y`). Keep the README's component table
and `docs/` in sync with code changes.

## One-time: adopting this layout on existing live boxes
If your live nodes still run pre-PathFuse configs, migrate them once onto this repo's
generic vocabulary + the deploy kit (real values in a gitignored `deploy/values.json`). After that,
all future updates are just the "Deploying a change" loop above. (Do it carefully on the relay first,
with backups + healthcheck + rollback ready, since it's the live failover system.)
