#!/usr/bin/env bash
# PathFuse — verify a deployed node. Usage: healthcheck.sh -c values.json
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"; VALUES="$HERE/values.example.json"
while [ $# -gt 0 ]; do case "$1" in -c) VALUES="${2:?-c requires a values.json path}"; shift;; esac; shift; done
ROLE=$(python3 -c "import json;print(json.load(open('$VALUES'))['role'])")
UI=$(python3 -c "import json;print(json.load(open('$VALUES'))['ports']['ui'])")
fail=0
chk(){ if eval "$2" >/dev/null 2>&1; then echo "PASS: $1"; else echo "FAIL: $1"; fail=1; fi; }

if [ "$ROLE" = client ]; then
  chk "sbfd active"        "systemctl is-active --quiet sbfd"
  chk "sbfd-ctl active"    "systemctl is-active --quiet sbfd-ctl"
  chk "udpspeeder-client"  "systemctl is-active --quiet udpspeeder-client"
  chk "UI /api/state"      "curl -fs --max-time 3 http://127.0.0.1:$UI/api/state"
else
  chk "sbfd active"        "systemctl is-active --quiet sbfd"
  chk "udpspeeder-server"  "systemctl is-active --quiet udpspeeder-server"
  chk "udpspeeder-fec"     "systemctl is-active --quiet udpspeeder-fec"
fi
chk "wg0 has a peer"       "[ -n \"\$(wg show wg0 peers)\" ]"
[ "$fail" = 0 ] && echo "ALL CHECKS PASSED" || { echo "SOME CHECKS FAILED"; exit 1; }
