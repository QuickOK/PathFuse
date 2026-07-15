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

# 3) spool-notify's spool dir: a real prerequisite, not just documentation. The
# client-only root units that notify (maintenance-reboot.service, hotspot-watchdog.service)
# use a tolerant "-/var/spool/spool-notify" ReadWritePaths entry so a missing dir degrades
# to a lost notification instead of an unstartable unit (226/NAMESPACE) — but under
# ProtectSystem=strict a missing path left out of the mount table stays read-only, so
# spool-notify's own `mkdir -p` cannot self-heal from inside the sandbox. Create it here
# so notifications actually work instead of silently degrading every time.
#
# Creation-only, and group-owned + 0770: sbfd-ctl.service also spools into this
# same directory as the unprivileged sbfd-ctl user (via a SupplementaryGroups=
# drop-in — see deploy/spool-notify/README.md), which needs group write access,
# not world-readable 0755. Only chgrp/chmod when we're the one creating the
# directory — never touch an existing dir's owner/mode, so a re-run on a
# correctly-configured box is a no-op instead of reverting a working setup.
if [ "$ROLE" = "client" ]; then
  getent group spool-notify >/dev/null 2>&1 || run "sudo groupadd -f spool-notify"
  if [ ! -d /var/spool/spool-notify ]; then
    run "sudo mkdir -p /var/spool/spool-notify"
    run "sudo chgrp spool-notify /var/spool/spool-notify"
    run "sudo chmod 0770 /var/spool/spool-notify"
  fi
fi

# 4) ntfy-control (reboot-trigger) service user + scoped sudoers: the
# generic service-user loop in step 2 does not support supplementary groups
# (this user needs the `spool-notify` group — already created in step 3 — to
# read the group-readable /etc/spool-notify.auth
# ntfy-control.service sources), and a NOPASSWD sudoers file needs its own
# install + validate step — a bad sudoers file must fail the install, not
# silently ship, so `visudo -cf` runs right after placing it. ntfy-dispatch
# itself is code (like the .py modules), not config, so it is NOT installed
# here — see deploy/README.md's manual install list.
if [ "$ROLE" = "client" ]; then
  # The spool-notify group is created in step 3 (client role); reuse it here.
  getent passwd ntfy-ctl >/dev/null 2>&1 || run "sudo useradd --system --no-create-home --shell /usr/sbin/nologin -G spool-notify ntfy-ctl"
  # Validate the REPO SOURCE first, so a malformed sudoers file never lands in
  # /etc/sudoers.d (where a broken file could break the sudo this very script
  # relies on for its remaining steps). Only install once the source parses;
  # the post-install check then confirms the placed copy too.
  run "sudo visudo -cf '$HERE/ntfy-control/sudoers-ntfy-ctl'"
  run "sudo install -D -m0440 '$HERE/ntfy-control/sudoers-ntfy-ctl' /etc/sudoers.d/ntfy-ctl"
  run "sudo visudo -cf /etc/sudoers.d/ntfy-ctl"
fi

# 5) place rendered files per manifest (dest + mode), backing up existing
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

# 6) reload systemd; do NOT auto-enable/start (see README cutover order)
run "sudo systemctl daemon-reload"
echo "Rendered+installed. NOT auto-enabling/starting services — see deploy/README.md for the"
echo "start order (the client's udpspeeder-client must start only after the wg0 Endpoint cutover)."
