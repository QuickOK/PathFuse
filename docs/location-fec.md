# Location-aware FEC floors (optional)

`location_fec.py` is an optional daemon for mobile deployments. It remembers
which places degrade which WAN and raises the FEC floor *before* the vehicle
arrives, so the adaptive engine — which can only react after loss is felt —
is already protected when the bad stretch begins. Operators can also pin a
minimum level to a named place.

It is **raise-only**. Nothing here ever lowers a ratio below what `sbfd-ctl`
would apply on its own. The core failover daemon has no GPS coupling; this is
a separate, independently toggled layer, like `environ_ctl`.

## How it drives FEC

Every second the daemon reads the vehicle's position from gpsd and the loss
`sbfd-ctl` already publishes in `/run/sbfd-ctl/state.json`, and writes
`/run/sbfd-ctl/location_fec.json`:

```json
{"set_ts": 1750000000.0, "source": "location_fec",
 "wans": {"wan1": {"level": 3, "reason": "learned dr79z6n (4 passes)"}}}
```

`sbfd-ctl` reads it every tick. For the WAN currently **driving** FEC, the
adaptive level is lifted to `level` (resolved against that WAN's own loss
table) — never lowered. A record older than `location_fec.stale_after_s`
(default 30 s) is ignored, so a stopped daemon withdraws its floor with no
cleanup. An empty `wans` is an explicit withdrawal.

The floor lifts the **client→relay leg only** — the parity this box sends. The
relay→client leg keeps its own profile and config floor, untouched by
location: pushing the location level to the relay is a planned follow-up, so a
place known to hurt the downstream direction is not yet protected in it.

## Learning

Positions are keyed to ~150 m geohash tiles. One contiguous visit to a tile is
a **pass**; each pass contributes that WAN's p90 loss to a per-tile running
average. A tile actuates after `min_passes` (3) distinct passes, so one bad
day cannot brand a place, and a vehicle parked in a dead spot cannot confirm it
by sheer sample count. Clean passes decay the average — about six clear a
level-3 tile. Tiles not seen for `max_age_days`, or clean for
`clean_drop_days`, are dropped; the store is capped at `max_tiles`.

Post-FEC residual loss is recorded per tile and shown on the map, but never
actuated: acting on it would form a feedback loop with the floor it produced.

## Look-ahead and exit hold

Above `min_speed_ms`, the resolver projects the position along the gpsd track
for `lookahead.seconds` and takes the worst tile or zone between here and
there. At 25 m/s that raises the floor about 600 m early. Stopped, or with no
track, only the current tile counts. When the responsible tile leaves the
window the level is held for `exit_hold_s` before dropping.

## Manual zones

```json
"zones": [
  {"label": "depot", "lat": 0.000, "lon": 0.000, "radius_m": 300, "level": 3},
  {"label": "underpass", "lat": 0.000, "lon": 0.000, "radius_m": 150,
   "level": 4, "wans": ["wan1"], "suppress_learned": false}
]
```

A zone matches when its centre is within `radius_m` of the position or any
look-ahead point. `level` is a guaranteed minimum for the listed `wans`
(default: all); the learned value can only push above it, and the two combine
by `max`. `suppress_learned: true` ignores the learned value inside that zone,
for a place the learner has simply got wrong — inside it only: the tiles whose
centres fall in the circle are dropped from the look-ahead, and a confirmed bad
tile on the approach still raises the floor. Read a station's label off the
`:8081` map and paste its centroid to name a place you already recognise.

Edited zones are re-read on `SIGHUP`: `systemctl reload location-fec`, or
`kill -HUP <pid>` where the daemon is not run under systemd. A reload re-reads
the **whole** config file — zones, the gpsd host/port, `wans`, the intervals,
the loss table — and re-applies `exit_hold_s` and the `learning` parameters to
the running hold and tile store, naming what it changed in one log line. A
value that will not parse is logged and left as it was. `store_path` is the one
exception: the running store keeps writing the old path, because rebinding it
mid-run would strand everything learned since boot. Changing it needs a
restart, and the reload says so.

## Operator controls

- **Location FEC** toggle on the `:8081` page, beside Environment. Off
  withdraws the floor immediately; the daemon keeps learning.
- `location_fec.enabled` in `sbfd-ctl.json` is the boot-time default.
- The FEC card names the floor when it binds: `· location floor (learned …)`.
- The map draws learned tiles as loss-coloured squares and zones as circles.

## Fail-safe

| Condition | Behaviour |
|---|---|
| gpsd down or no fix | explicit withdrawal; learning pauses; one log line per outage, one on recovery |
| `state.json` stale | learning skipped that tick; the floor still resolves |
| daemon dies | floor withdrawn by `stale_after_s` |
| daemon alive but blind for `withdraw.max_stale_s` | already withdrawn (no fix ⇒ withdrawal is immediate); logs one warning per blind episode, not one per tick, so the blindness is visible in the journal without flooding it |
| corrupt store | starts empty, one log line |
| invalid zone | logged and skipped |

Known hole: in a tunnel the fix dies where the link does, so nothing is learned
there. The approach tiles usually still learn; a manual zone covers the rest.

## Run

```bash
cp config/location-fec.example.json config/location-fec.json   # then edit it
location_fec.py -c config/location-fec.json
```

A deployed node reads the rendered `/etc/sbfd-ctl/location-fec.json` instead.

Requires `gpsd`. Deploy via `deploy/templates/systemd/location-fec.service.tmpl`.
