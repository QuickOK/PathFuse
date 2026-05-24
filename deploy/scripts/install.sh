#!/usr/bin/env bash
# PathFuse — render + install configs/units for this host's role. Idempotent.
# Usage: install.sh -c values.json [--dry-run]
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"   # deploy/
VALUES="$HERE/values.example.json"; DRY=0
while [ $# -gt 0 ]; do case "$1" in -c) VALUES="$2"; shift;; --dry-run) DRY=1;; esac; shift; done
run(){ if [ "$DRY" = 1 ]; then echo "DRY: $*"; else eval "$*"; fi; }

ROLE=$(python3 -c "import json,sys; print(json.load(open('$VALUES'))['role'])")
echo "Installing PathFuse role=$ROLE from $VALUES"

# 1) render
OUT="$HERE/out"
run "python3 '$HERE/render.py' -c '$VALUES' -o '$OUT'"

# 2) service users (client: sbfd + sbfd-ctl; relay: sbfd)
for u in $(python3 -c "import json;v=json.load(open('$VALUES'));print(v['users']['sbfd'], v['users']['sbfdctl'] if v['role']=='client' else '')"); do
  id "$u" >/dev/null 2>&1 || run "sudo useradd --system --no-create-home --shell /usr/sbin/nologin $u"
done

# 3) place rendered files per manifest (dest + mode), backing up existing
python3 - "$VALUES" "$OUT/$ROLE" "$HERE/templates/manifest.json" <<'PY' | while read -r src dest mode; do
import json,sys,os
values=json.load(open(sys.argv[1])); role=values["role"]; out=sys.argv[2]
for e in json.load(open(sys.argv[3])):
    if role in e["roles"]:
        print(os.path.join(out, e["dest"].lstrip("/")), e["dest"], e["mode"])
PY
  ts=$(date +%Y%m%d-%H%M%S)
  [ -f "$dest" ] && run "sudo cp -a '$dest' '$dest.bak.$ts'"
  run "sudo install -D -m '$mode' '$src' '$dest'"
done

# 4) reload systemd; do NOT auto-enable/start (see README cutover order)
run "sudo systemctl daemon-reload"
echo "Rendered+installed. NOT auto-enabling/starting services — see deploy/README.md for the"
echo "start order (the client's udpspeeder-client must start only after the wg0 Endpoint cutover)."
