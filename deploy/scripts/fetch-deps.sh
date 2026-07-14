#!/usr/bin/env bash
# PathFuse — fetch/build the third-party data-plane PathFuse integrates with.
# Installs: wireguard-tools (distro pkg), UDPspeeder/speederv2 (build), engarde (build),
# grpcurl (pinned binary download, checksum-verified).
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

echo "== grpcurl (wan2 terminal gRPC) =="
GRPCURL_VERSION="${GRPCURL_VERSION:-1.9.1}"
GRPCURL_SHA256="${GRPCURL_SHA256:-fc0d0453dd9f276fa2158f34ba1666f7fd4d6e4053f781d0945226ebe8914cb1}"
if ! command -v grpcurl >/dev/null && [ ! -x /usr/local/bin/grpcurl ]; then
  case "$(uname -m)" in
    aarch64|arm64) GRPCURL_ARCH=arm64 ;;
    x86_64|amd64)  GRPCURL_ARCH=x86_64 ;;
    *) echo "unsupported arch $(uname -m) for grpcurl — install it manually"; GRPCURL_ARCH="" ;;
  esac
  if [ -n "$GRPCURL_ARCH" ]; then
    TARBALL="grpcurl_${GRPCURL_VERSION}_linux_${GRPCURL_ARCH}.tar.gz"
    URL="https://github.com/fullstorydev/grpcurl/releases/download/v${GRPCURL_VERSION}/${TARBALL}"
    # Only create a real temp dir (and only clean it up) outside --dry-run: under
    # --dry-run the whole block below is just echoed via run(), but mktemp -d
    # itself is a real side effect that ran unconditionally before, leaking a
    # directory even in preview mode. The trap covers a mid-download failure
    # under `set -e` (e.g. checksum mismatch) that would otherwise skip the
    # final `rm -rf` and leak too.
    if [ "$DRY" = 1 ]; then
      TMP="/tmp/fetch-deps-grpcurl.XXXXXX"   # preview path only; nothing created
    else
      TMP=$(mktemp -d)
      trap 'rm -rf "$TMP"' EXIT
    fi
    run "curl -fsSL -o '$TMP/$TARBALL' '$URL'"
    # Checksum is pinned for the arm64 tarball; skip the gate on other arches
    # rather than assert a hash we have not verified.
    if [ "$GRPCURL_ARCH" = arm64 ]; then
      run "echo '$GRPCURL_SHA256  $TMP/$TARBALL' | sha256sum -c -"
    fi
    run "tar -xzf '$TMP/$TARBALL' -C '$TMP' grpcurl"
    run "sudo install -m0755 '$TMP/grpcurl' /usr/local/bin/grpcurl"
    if [ "$DRY" = 1 ]; then
      echo "DRY: rm -rf '$TMP'"
    else
      rm -rf "$TMP"
      trap - EXIT
    fi
  fi
fi

echo "Management overlay (for the relay's control ports) and the egress VPN (relay_vpn mode)"
echo "are operator-provided — see deploy/README.md."
