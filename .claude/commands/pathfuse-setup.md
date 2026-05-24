---
description: Guided PathFuse node setup (relay/client/both) — walks the deploy steps, confirming before every privileged or remote command.
---
Use the `pathfuse-setup` skill (`.claude/skills/pathfuse-setup/SKILL.md`) to set up a PathFuse node
from this repo: choose the end(s) and mode, then walk steps 0–5 (prerequisites -> values -> secrets ->
render+install -> start+verify). Confirm before every privileged or remote command, and never expose
private keys (only exchange public keys + the shared UDPspeeder key). The deterministic mechanics are
in `deploy/scripts/`; the manual entry point is `deploy/wizard.sh`.
