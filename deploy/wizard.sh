#!/usr/bin/env bash
# PathFuse setup wizard — entry-point chooser. Routes to deploy/scripts/* (never duplicates
# their mechanics). Works with zero Claude. For agent-driven setup, choose a Claude mode and it
# points you at the /pathfuse-setup skill. --check prints the plan and changes nothing.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS="$HERE/scripts"
CHECK=0
case "${1:-}" in
  -h|--help)
    cat <<EOF
PathFuse setup wizard
Usage: wizard.sh [--check]
Interactively choose which end (relay / client / both-over-SSH) and how to drive setup
(manual / Claude-assisted / Claude-native). Manual mode runs the deploy scripts in
$SCRIPTS. --check prints the planned actions and exits without changing anything.
EOF
    exit 0;;
  --check|--dry-run) CHECK=1;;
esac

ask(){ local a; read -r -p "$1 " a; printf '%s' "$a"; }

plan_for(){
  local role="$1" tgt="${2:-}" pfx=""
  [ -n "$tgt" ] && pfx="ssh $tgt "
  echo "== setup: role=$role ${tgt:+(over ssh: $tgt)} =="
  echo "-> ${pfx}$SCRIPTS/detect-os.sh"
  echo "-> ${pfx}$SCRIPTS/fetch-deps.sh --dry-run"
  echo "-> ${pfx}$SCRIPTS/gen-secrets.sh --dry-run   (prints this end's WG public key to exchange)"
  echo "-> ${pfx}$SCRIPTS/install.sh -c $HERE/values.json --dry-run"
  echo "-> ${pfx}$SCRIPTS/install.sh -c $HERE/values.json"
  echo "-> ${pfx}$SCRIPTS/healthcheck.sh -c $HERE/values.json"
}

run_manual(){
  local role="$1" tgt="${2:-}" pfx=""
  [ -n "$tgt" ] && pfx="ssh $tgt "
  plan_for "$role" "$tgt"
  echo "Each step is confirmed before it runs."
  for step in \
    "$SCRIPTS/detect-os.sh" \
    "$SCRIPTS/fetch-deps.sh --dry-run" \
    "$SCRIPTS/gen-secrets.sh --dry-run" \
    "$SCRIPTS/install.sh -c $HERE/values.json --dry-run" \
    "$SCRIPTS/install.sh -c $HERE/values.json" \
    "$SCRIPTS/healthcheck.sh -c $HERE/values.json"; do
    a=$(ask "run: ${pfx}${step}  ? [y/N]")
    case "$a" in y|Y) eval "${pfx}${step}";; *) echo "skipped";; esac
  done
}

echo "PathFuse setup wizard"
if [ "$CHECK" = 1 ]; then
  echo "[--check] would ask: which end (relay/client/both) + mode (manual/Claude-assisted/Claude-native)."
  echo "[--check] manual mode runs: detect-os, fetch-deps, gen-secrets, install, healthcheck (in deploy/scripts/)."
  plan_for client ""
  exit 0
fi

end=$(ask "Which end? [1] relay  [2] client  [3] both (this host + other over SSH):")
mode=$(ask "Drive how? [1] manual  [2] Claude-assisted  [3] Claude-native:")
case "$mode" in
  2|3) echo "Agent-driven: open Claude Code in this repo and run:  /pathfuse-setup";
       echo "(it uses the same scripts in $SCRIPTS, confirming each privileged step). Exiting wizard."; exit 0;;
esac
case "$end" in
  1) run_manual relay "";;
  2) run_manual client "";;
  3) run_manual "$(ask 'local end role? [relay/client]:')" "";
     rt=$(ask 'remote SSH target (user@host):'); rr=$(ask 'remote end role? [relay/client]:');
     echo "Now the remote end (every command confirmed):"; run_manual "$rr" "$rt";
     echo "Finally: exchange the two ends' WG PUBLIC keys + copy /etc/udpspeeder/key so it matches, then re-run install without --dry-run.";;
  *) echo "unknown choice"; exit 1;;
esac
echo "Done. Run healthcheck.sh on each end to verify."
