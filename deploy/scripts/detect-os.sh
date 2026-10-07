#!/usr/bin/env bash
# PathFuse — report environment + gate on prerequisites.
set -euo pipefail
# /etc/os-release is the host's own file, read when this runs: the repo has none to follow.
# shellcheck disable=SC1091
echo "os: $(if . /etc/os-release 2>/dev/null; then echo "$PRETTY_NAME"; else uname -s; fi)"
echo "arch: $(uname -m)"
echo "init: $(ps -p1 -o comm= 2>/dev/null)"
for c in python3 systemctl nft wg; do
  if command -v "$c" >/dev/null; then echo "ok: $c"; else echo "MISSING: $c"; fi
done
[ "$(ps -p1 -o comm= 2>/dev/null)" = systemd ] || { echo "ERROR: systemd required" >&2; exit 1; }
