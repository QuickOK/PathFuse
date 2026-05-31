#!/usr/bin/env bash
# PathFuse preflight — run before pushing to the public repo. Blocks anything that would
# break tests, fail to render, or LEAK real infrastructure identifiers / secrets.
# Used directly (`scripts/preflight.sh`) and by the pre-push hook (scripts/hooks/pre-push).
set -uo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1   # repo root
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
fail=0

echo "== tests =="
"$PY" -m pytest -q || fail=1

echo "== deploy render --check =="
"$PY" deploy/render.py --check || fail=1

echo "== gate: no deployment vocabulary =="
# Scan TRACKED files only (skips .pytest_cache/.venv/out and other local cruft). Exclude the
# gate's own files, which legitimately contain the term list / workflow docs.
if git ls-files | grep -vE '^(scripts/preflight\.sh|MAINTAINING\.md)$' \
     | xargs grep -nIE "truck|[^a-z]ovh|starlink|t-?mobile|\btmo\b|tailscale|warp|cloudflare|raspberr|\bpi\b|\bcab\b" 2>/dev/null \
     | grep -viE "pip |pipe|pid |mapping"; then
  echo "!! deployment vocabulary found (above) — generalize it before pushing"; fail=1
else
  echo "ok: no deployment vocabulary"
fi

echo "== gate: every IP is RFC-reserved (examples only) =="
bad=$(git ls-files -z | xargs -0 grep -hoIE "([0-9]{1,3}\.){3}[0-9]{1,3}" 2>/dev/null | sort -u \
      | grep -vE "^(0\.0\.0\.0|127\.|10\.|100\.64\.|169\.254\.|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|224\.|255\.)" || true)
if [ -n "$bad" ]; then
  echo "!! non-reserved IP literal(s) found — replace with RFC-5737/6598 examples:"; echo "$bad"; fail=1
else
  echo "ok: all IP literals are RFC-reserved"
fi

echo "== gate: no obvious secrets =="
if git ls-files -z | xargs -0 grep -nIE "BEGIN (OPENSSH|RSA|EC) PRIVATE KEY|PrivateKey *= *[A-Za-z0-9+/]{40}" 2>/dev/null; then
  echo "!! private-key material found — never commit secrets"; fail=1
else
  echo "ok: no private-key material"
fi

if [ "$fail" = 0 ]; then echo "PREFLIGHT OK"; else echo "PREFLIGHT FAILED — fix the above before pushing"; exit 1; fi
