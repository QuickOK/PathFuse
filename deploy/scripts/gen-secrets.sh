#!/usr/bin/env bash
# PathFuse — generate this host's secrets (WireGuard keypair + UDPspeeder key).
# Idempotent: never overwrites an existing secret unless --rotate (with backup).
# Prints this host's WG public key for you to give the other end.
set -euo pipefail
ROTATE=0; DRY=0
for a in "$@"; do case "$a" in --rotate) ROTATE=1;; --dry-run) DRY=1;; esac; done
run(){ if [ "$DRY" = 1 ]; then echo "DRY: $*"; else eval "$*"; fi; }

WG_KEY=/etc/wireguard/wg0.key
UDP_KEY_DIR=/etc/udpspeeder; UDP_KEY="$UDP_KEY_DIR/key"
ts=$(date +%Y%m%d-%H%M%S)

command -v wg >/dev/null || { echo "wg (wireguard-tools) not found; run fetch-deps first" >&2; exit 1; }

if [ -s "$WG_KEY" ] && [ "$ROTATE" = 0 ]; then
  echo "WG key exists ($WG_KEY); use --rotate to replace."
else
  [ -s "$WG_KEY" ] && run "sudo cp -a $WG_KEY $WG_KEY.bak.$ts"
  run "sudo install -d -m 0700 /etc/wireguard"
  run "umask 077; wg genkey | sudo tee $WG_KEY >/dev/null"
  run "sudo chmod 0600 $WG_KEY"
fi
echo "This host's WireGuard PUBLIC key (give this to the other end):"
if [ "$DRY" = 1 ]; then echo "  DRY: sudo wg pubkey < $WG_KEY"; else sudo wg pubkey < "$WG_KEY"; fi

if [ -s "$UDP_KEY" ] && [ "$ROTATE" = 0 ]; then
  echo "UDPspeeder key exists ($UDP_KEY); use --rotate to replace."
else
  [ -s "$UDP_KEY" ] && run "sudo cp -a $UDP_KEY $UDP_KEY.bak.$ts"
  run "sudo install -d -m 0755 $UDP_KEY_DIR"
  run "printf 'UDPSPEEDER_KEY=%s\n' \"\$(head -c 32 /dev/urandom | base64)\" | sudo tee $UDP_KEY >/dev/null"
  run "sudo chmod 0600 $UDP_KEY"
fi
echo "NOTE: the UDPspeeder key must MATCH on both ends — copy $UDP_KEY to the other host (or let the wizard do it)."
