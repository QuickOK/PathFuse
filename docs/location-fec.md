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

The floor lifts **both legs**. On the client it lifts the parity this box
sends; the level is also pushed to the relay over `/fec` as `location_level`,
where it is applied raise-only against the table of the profile that push
selected, so a level learned on a longer table cannot index past a shorter
one. It rides the same record as the pushed profile and signal floor, so it
expires with them — a client that stops talking withdraws its floor from the
relay too. A relay too old to know the key ignores it and keeps its own
profile and config floor.

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

A zone pins a minimum level to a place. There are two ways to make one, and
the daemon treats them identically once they exist.

### Draw it on the map

Open the `:8081` map, press **Zones**, and click where the bad stretch is. The
editor opens at that point: name it, drag the radius until the circle covers
the place, choose a level, tick the WANs it applies to, and Save. Click a zone
you drew to edit or delete it; Escape cancels. Leave every WAN unticked to
cover all of them.

Each level in the list names its ratio and what it costs — `level 3 — 8:6 (75%
overhead)` — on the loss table of the link **currently driving FEC**, so the
choice is made against the ladder that will actually be applied. Levels at or
below the current floor say `already covered by the floor` and are dimmed;
they remain selectable, because a zone chosen there still holds if the floor
later drops.

The radius has a slider for quick adjustment and a number beside it for exact
or large values. The slider stops at 2 km; the number accepts anything the
endpoint does, up to 50 km. Opening a wider zone shows its real radius and
keeps it — the editor never shrinks a zone you only opened to look at.

Drawn zones live in `/var/lib/sbfd-ctl/location_zones.json`. `sbfd-ctl` writes
it and `location_fec` re-reads it whenever the file changes, so a zone takes
effect within a poll — no signal, no restart. A missing file simply means no
drawn zones; an unusable one is logged once and treated the same way.

Each zone in that file carries an `id`, and the file carries a `next_id`
counter beside them. Ids are handed out from the counter and never reused, so
an editor panel left open on a zone that has since been deleted cannot save
over a different zone that came later. A zone edited into the file by hand
without an `id` still works — the daemon never asked for one — but the map
cannot address it, so it is drawn dashed and says so in its tooltip. Delete it
and redraw it to make it editable again.

### Declare it in the config file

```json
"zones": [
  {"label": "depot", "lat": 0.000, "lon": 0.000, "radius_m": 300, "level": 3},
  {"label": "underpass", "lat": 0.000, "lon": 0.000, "radius_m": 150,
   "level": 4, "wans": ["wan1"], "suppress_learned": false}
]
```

Zones in `/etc/sbfd-ctl/location-fec.json` are for places that should ship
with the box: they are deployed with the rest of the config and take effect on
a reload. That is the whole difference. Config zones need a deploy and a
reload; map-drawn zones need neither. The map shows config zones as dashed
circles it will not let you edit, and drawn ones as solid circles it will.

### What a zone does

A zone matches when its centre is within `radius_m` of the position or any
look-ahead point. `level` is a guaranteed minimum for the listed `wans`
(default: all); the learned value can only push above it, and every source
combines by `max`. `suppress_learned: true` ignores the learned value inside
that zone, for a place the learner has simply got wrong — inside it only: the
tiles whose centres fall in the circle are dropped from the look-ahead, and a
confirmed bad tile on the approach still raises the floor.

Config zones are re-read on `SIGHUP`: `systemctl reload location-fec`, or
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
- *active* means the location floor is currently why the wire carries more
  parity than the loss-driven decision alone would send, whether or not it
  triggered the most recent write; a refused write is never counted.
- The map draws learned tiles as loss-coloured squares and zones as circles:
  solid for a zone it can edit, dashed for one it cannot. Every zone the
  daemon is acting on is drawn, and a dashed one's tooltip says why it is not
  editable here.
- **Zones** on the map page opens the zone editor. With it off the map
  behaves exactly as it did before, and config zones stay read-only in
  either state.

## Fail-safe

| Condition | Behaviour |
|---|---|
| gpsd down or no fix | explicit withdrawal; learning pauses; one log line per outage, one on recovery |
| `state.json` stale | learning skipped that tick; the floor still resolves |
| daemon dies | floor withdrawn by `stale_after_s` |
| daemon alive but blind for `withdraw.max_stale_s` | already withdrawn (no fix ⇒ withdrawal is immediate); logs one warning per blind episode, not one per tick, so the blindness is visible in the journal without flooding it |
| corrupt store | starts empty, one log line |
| invalid zone | logged and skipped |
| drawn-zone file missing or corrupt | no drawn zones, one log line; config zones and learning are unaffected |

Known hole: in a tunnel the fix dies where the link does, so nothing is learned
there. The approach tiles usually still learn; a manual zone covers the rest.

## Run

```bash
cp config/location-fec.example.json config/location-fec.json   # then edit it
location_fec.py -c config/location-fec.json
```

A deployed node reads the rendered `/etc/sbfd-ctl/location-fec.json` instead.

Requires `gpsd`. Deploy via `deploy/templates/systemd/location-fec.service.tmpl`.
