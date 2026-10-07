# Maintaining PathFuse

**This repo is the single source of truth.** Develop fixes and features *here*, in the generic
vocabulary (`relay`/`client`, `static_primary`, `relay_vpn`, RFC-reserved example IPs). Your live
deployment consumes this repo via the deploy kit — it does not have its own diverging copy.

## Day-to-day: change → verify → push
```bash
cd /path/to/PathFuse
# ... edit code / templates / docs ...
.venv/bin/python -m pytest -q          # (first time: python3 -m venv .venv && .venv/bin/pip install pytest)
scripts/preflight.sh                    # tests + render check + types + lint + sanitization + secret scan
git add -A && git commit -m "fix: ..."  # or feat: / docs: / refactor: / test:
git push origin main
```
The **pre-push hook** runs `preflight.sh` automatically and **blocks the push** if anything fails.
Enable it once per clone:
```bash
git config core.hooksPath scripts/hooks
```

## Type checking
`scripts/preflight.sh` runs `scripts/typecheck.py`: pyright in strict mode (`pyrightconfig.json`)
and mypy (`mypy.ini`, check_untyped_defs) over every tracked Python file. The gate is a per-file
ratchet: no file may have more errors from either tool than `scripts/typecheck-baseline.json`
records, and a file it does not list must have none. Strict counts an unannotated parameter or a
value of unknown type as an error, so new and edited code must be typed, and a new file must be
clean. When new code has to call a function that is not typed yet, type that function, or
silence that one line with `# pyright: ignore[<rule>]` and say why in a comment. After fixing
errors, run `scripts/typecheck.py --update-baseline` to record the lower floor.

The ratchet only stops counts rising, so the gate also refuses these ways of lowering one file's
checking, before either checker runs:
- `# pyright: basic` or `# pyright: standard`, wherever pyright would read it: among other
  operands, or after code on the same line;
- a `# pyright:` comment that sets a rule below an error for the whole file (`=false`, `=none`,
  `=warning` or `=information`; the gate counts errors only);
- a `# type: ignore` before the file's first statement, which mypy reads as silencing all of
  it;
- a `# mypy:` line of settings: mypy's settings belong in `mypy.ini`;
- `@no_type_check`, which switches both checkers off for a whole function, and any other use of
  `no_type_check` in code (an assignment, a call, an import that renames it), which could make an
  alias that does the same. The gate goes by the name, so it refuses any use of anything of your
  own called `no_type_check` too: name it something else.

To silence one line, write `# pyright: ignore[<rule>]` on it and say why beside it. pyright
disregards `# type: ignore` (`enableTypeIgnoreComments` is off), so a line is silenced for pyright
only by `# pyright: ignore[<rule>]`, and for mypy by
`# type: ignore[<code>]`. Tests pin both config files, and refuse a tracked stub (`*.pyi`), which
would change what the checkers see of a module in every file that imports it: a change to any of
these is a deliberate one. pyright reads every file as UTF-8, so a file that declares an encoding
other than UTF-8 or ASCII, as Python or as mypy reads the declaration, gets no verdict (exit 2), as
does one that does not tokenize or parse.

pyright is the pinned one that `npm ci` installs in `node_modules/` (see Linting below for
`npm ci`). Without it the gate falls back to a `pyright` on PATH, and its first line says which
one ran. Install mypy once per machine: `sudo apt install mypy`.

The gate sees tracked files only, so `git add` a new file before running it. `--update-baseline`
records the counts as they are, higher ones included, so its diff should only lower numbers. The
baseline was recorded with pyright 1.1.414 and mypy 1.15.0. Other versions can count differently,
so after upgrading either tool (pyright: its pin in `package.json`, then `npm install`), re-run
`scripts/typecheck.py --update-baseline` and commit the new baseline in a commit of its own, with
the versions named here updated. `tests/test_typecheck.py` asks pyright and mypy which comments
lower a file's checking, so its run after upgrading either re-checks the gate's rule against it.

## Linting
`scripts/preflight.sh` also runs `scripts/lint.py`: ShellCheck over every tracked shell script
(`*.sh`, plus extensionless files with a `sh` or `bash` shebang) and ESLint over the tracked
JavaScript (`*.js` and `*.mjs`, but not `ui/vendor/`), configured by `eslint.config.mjs`:
ESLint's recommended rules, no style rules. There is no baseline: any finding fails the gate,
an ESLint warning included. Run it alone with `scripts/lint.py`; like the type gate it sees
tracked files only, so `git add` a new script first.

To install: `sudo apt install shellcheck` once per machine, and `npm ci` in the repo root once
per clone (ESLint 10 needs Node 20.19+, 22.13+ or 24+). `npm ci` installs the versions pinned in
`package.json` and `package-lock.json` into `node_modules/` (gitignored). The gate runs that
ESLint only, never one on PATH: Debian's is too old to parse the `??` and `?.` the UI uses.

When a finding is intended, silence that one line and say why in a comment: a
`# shellcheck disable=SCxxxx` line directly above it, or
`// eslint-disable-next-line <rule> -- <why>`.

## The rules the gate enforces (keep the repo public-safe)
- **Generic vocabulary only.** No deployment-specific names (provider / ISP / host / hardware
  terms). The preflight gate rejects them — see the exact pattern in `scripts/preflight.sh`. Use
  `relay` / `client` / generic labels. Kept names: `sbfd`, `engarde`, `udpspeeder`,
  `wireguard`/`wg`, `fec`.
- **Only RFC-reserved example IPs:** `192.0.2.x`, `198.51.100.x`, `203.0.113.x` (RFC 5737),
  `100.64.x` (RFC 6598), `10.x`, `127.x`. Never a real public IP.
- **No secrets, ever.** Keys are generated at deploy time (`deploy/scripts/gen-secrets.sh`).

## Deploying a change to your live boxes
Your real, host-specific settings live in **`deploy/values.json`** — gitignored, never committed
(real IPs, interfaces, the exchanged WG public keys). To roll an update to a live node:
```bash
git pull                                                  # get the new PathFuse
deploy/scripts/install.sh -c deploy/values.json --dry-run # preview
deploy/scripts/install.sh -c deploy/values.json           # render + place (backs up, no auto-start)
# restart per deploy/README.md step 5 (relay first; client udpspeeder after engarde-client)
deploy/scripts/healthcheck.sh -c deploy/values.json
```
If you changed a daemon (`sbfd.py`, `sbfd_ctl.py`, `udpspeeder_fec.py`, `fec_*`), also copy the new
file to its install path (`/opt/sbfd`, `/opt/sbfd-ctl`) per `deploy/README.md` step 1, then restart.

## Releasing
Optional: tag releases (`git tag vX.Y && git push origin vX.Y`). Keep the README's component table
and `docs/` in sync with code changes.

## One-time: adopting this layout on existing live boxes
If your live nodes still run pre-PathFuse configs, migrate them once onto this repo's
generic vocabulary + the deploy kit (real values in a gitignored `deploy/values.json`). After that,
all future updates are just the "Deploying a change" loop above. (Do it carefully on the relay first,
with backups + healthcheck + rollback ready, since it's the live failover system.)

## Vendored third-party assets (`ui/vendor/`)

`ui/vendor/` contains unmodified upstream files (currently Leaflet 1.9.4,
BSD-2-Clause) served by the UI for offline capability. The directory is
excluded from preflight's vocabulary and IP gates (minified libraries contain
`Math.PI`, arbitrary dotted numbers, etc.). Only verbatim upstream releases may
live here — never project code, never anything edited.
