#!/usr/bin/env bash
# PathFuse setup wizard — entry-point chooser. Routes to deploy/scripts/* (never duplicates
# their mechanics). Works with zero Claude. For agent-driven setup, choose a Claude mode and it
# points you at the /pathfuse-setup skill. --check prints the plan and changes nothing.
# (no `-e`: a skipped or failing confirm step should not abort the whole wizard.)
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
  local role="$1" tgt="${2:-}"
  plan_for "$role" "$tgt"
  # Remote steps send LOCAL absolute paths ($SCRIPTS/$HERE) verbatim over ssh, so the
  # remote must have this repo checked out at the same path with values.json present.
  # Validate that precondition up front instead of failing opaquely mid-run.
  if [ -n "$tgt" ] && ! ssh "$tgt" "test -d '$SCRIPTS' && test -f '$HERE/values.json'"; then
    echo "ERROR: remote '$tgt' must have this repo checked out at the same path:" >&2
    echo "         $HERE   (with values.json present)." >&2
    echo "       The wizard sends local paths verbatim over ssh; aborting remote steps." >&2
    return 1
  fi
  echo "Each step is confirmed before it runs."
  # Fixed internal commands (script paths + fixed flags) — never user free-text.
  local -a steps=(
    "$SCRIPTS/detect-os.sh"
    "$SCRIPTS/fetch-deps.sh --dry-run"
    "$SCRIPTS/gen-secrets.sh --dry-run"
    "$SCRIPTS/install.sh -c $HERE/values.json --dry-run"
    "$SCRIPTS/install.sh -c $HERE/values.json"
    "$SCRIPTS/healthcheck.sh -c $HERE/values.json"
  )
  local step a
  for step in "${steps[@]}"; do
    if [ -n "$tgt" ]; then
      a=$(ask "run on $tgt: $step  ? [y/N]")
      # $tgt is a single quoted argument to ssh (no local shell injection from the
      # operator-typed target); $step is the fixed remote command string.
      case "$a" in y|Y) ssh "$tgt" "$step";; *) echo "skipped";; esac
    else
      a=$(ask "run: $step  ? [y/N]")
      # intentional word-split of $step into argv; contents are fixed, not user input
      # shellcheck disable=SC2086
      case "$a" in y|Y) $step;; *) echo "skipped";; esac
    fi
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
