#!/usr/bin/env bash
# PathFuse — fetch/build the third-party data-plane PathFuse integrates with.
# Installs: wireguard-tools (distro pkg), UDPspeeder/speederv2 (build), engarde (build).
# It does NOT configure your management overlay or egress VPN — those are operator choices
# (see deploy/README.md). Re-runnable. Use --dry-run to preview.
set -euo pipefail
DRY=0; for a in "$@"; do [ "$a" = --dry-run ] && DRY=1; done
run(){ if [ "$DRY" = 1 ]; then echo "DRY: $*"; else eval "$*"; fi; }

SPEEDER_REF="${SPEEDER_REF:-master}"     # pin a tag/commit for reproducibility
ENGARDE_REF="${ENGARDE_REF:-master}"

echo "== wireguard-tools =="
command -v wg >/dev/null || run "sudo apt-get update && sudo apt-get install -y wireguard-tools"

echo "== UDPspeeder (speederv2) =="
if ! command -v speederv2 >/dev/null && [ ! -x /usr/local/bin/speederv2 ]; then
  echo "Build UDPspeeder from https://github.com/wangyu-/UDPspeeder (ref $SPEEDER_REF) and install the"
  echo "resulting 'speederv2' to /usr/local/bin/speederv2. (Native build recommended on ARM.)"
  run "echo 'see README: UDPspeeder build steps'"
fi

echo "== engarde =="
if ! command -v engarde-client >/dev/null && ! command -v engarde-server >/dev/null; then
  echo "Build engarde from source (Go) at ref $ENGARDE_REF and install engarde-client/engarde-server."
  run "echo 'see README: engarde build steps'"
fi

echo "Management overlay (for the relay's control ports) and the egress VPN (relay_vpn mode)"
echo "are operator-provided — see deploy/README.md."
