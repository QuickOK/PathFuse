# PathFuse design notes

Why the system is built the way it is. (The published code is the source of truth; this captures
the reasoning a reader would otherwise have to reverse-engineer.)

## Software BFD, not hardware/BGP
Link liveness is a small stdlib UDP daemon (`sbfd`) that runs a lightweight BFD-equivalent: one UDP
session per WAN, each pinned to its interface with `SO_BINDTODEVICE` and a port offset by
`session_id`, exchanging keepalives and tracking up/down + EWMA RTT + loss. This needs no routing
protocol, no special hardware, and no privileges beyond `CAP_NET_RAW` on the client — so it runs on
commodity edge devices and detects a dead uplink in well under a second.

## Redundancy, not bandwidth aggregation
PathFuse deliberately trades bandwidth for reliability. Full-redundancy mode *duplicates* packets
across links (so any single link can vanish with zero impact and the lower-latency copy wins);
master/backup uses one link and fails over. Neither *sums* link bandwidth. This is the right
trade-off for keeping a connection **alive and low-loss** over flaky mobile links, which is the goal
— not maximizing throughput.

## Adaptive FEC: table + hysteresis, sender-applied
Forward error correction recovers lost packets without waiting for retransmits, which matters on
high-latency links. Redundancy is wasteful when links are clean, so it's **adaptive**: a loss→ratio
table scales parity with measured loss, and hysteresis (ramp-up ticks before raising, a hold time
before lowering) stops the ratio from flapping on transient spikes. FEC is applied by the **sender**
and recovered at the **decoder**, so each direction is controlled independently by that direction's
sender.

**Mode-aware backoff:** in full-redundancy with enough healthy links, engarde already duplicates
every packet, so the *loss-driven* component backs off to the off tier (`full_mode_backoff_fec`)
and the pre-emptive *signal* floor disengages. The `min_adaptive` profile/config **floor is
deliberately kept** on top of duplication: full mode engages at reliability-critical moments
(environmental hazard, cell-handoff windows, both-WANs-down fallback), and duplication only saves a
packet lost on *one* link — floor parity is what recovers packets dropped on both at once, for
~8–12% overhead on an already-doubled stream. Net effect: with a floor configured,
`full_mode_backoff_fec` bounds only the adaptive component and cannot undercut the floor.

**The driver WAN is sticky, because it selects policy and not just a number.** The driver is the
worst active link, and it picks the whole FEC policy — table, floor, hysteresis — so changing it
swaps the ladder beneath the adaptive engine and reseeds its runtime. `step_level` damps the
*ratio* against loss jitter, but nothing damped the *driver*: on a duplicated stream where both
links sit near zero loss, it flipped on noise (measured: 82 profile switches in 24h, median 95s
apart, every one in full redundancy where the ratio should not have moved at all). The worst active link
must now hold that position for `driver_dwell_s` before taking over, mirroring
`policy.dynamic_swap_dwell_s` for master selection.

Dwell alone is the whole mechanism: an excursion shorter than the dwell is ignored, a sustained one
is followed, and because a tie resolves to the same WAN every tick the driver returns to the quiet
state's pick once the excursion passes. That return is load-bearing rather than tidy — the cellular
signal floor only engages while the cellular WAN is the driver, so a driver that never came home
would silently disarm it. A loss *margin* was tried alongside the dwell and removed: whenever no WAN
cleared it the same WAN challenged anyway as the canonical pick, so it never changed whether a flip
was damped, only which WAN challenged when three or more were active — and there it picked the first
by name rather than the worst. Only one WAN active — every mode but full redundancy — short-circuits
entirely, so this cannot delay a master_backup failover.

**The wall display draws one fixed scale, and shades what is reachable.** Each direction publishes a
`ladder`: a `scale` of rungs, the inclusive `reach_lo`/`reach_hi` span currently available, and
`applied_index` / `floor_index` within it. The row always draws the whole scale, so a position means
one ratio no matter which profile is driving — which is what lets the *shaded span alone* say which
profile that is.

The span is the point. A leg's reachable set is not its loss table: `off` and `fixed` ignore the
table entirely, a `min_adaptive` floor makes every rung beneath it unreachable, and full-redundancy
backoff pins the adaptive engine to a single rung. In steady full redundancy the client leg has
**exactly one** reachable ratio — its floor — at every loss value from 0% to 100%, so the row shows
one shaded dot and says `pinned`. The relay leg, whose `run_once` calls `loss_to_level` directly and
never consults `mode_aware_level`, keeps its full span under the same conditions; the two cards
differing is that asymmetry made visible, not a rendering bug.

Only the client can build this: it knows every profile's table *and* floor, while the relay is
pushed a single resolved floor and never sees the client's link states. So the client computes both
legs' views and re-scales the relay's reported ratio onto the shared scale. `applied_index` comes
from the ratio actually on the wire rather than the adaptive engine's level index, which makes it
correct in `fixed`/`off` and under the signal floor with no special cases, and `below_floor`
compares the ratios themselves — a floor between two rungs shares a position with the rung beneath
it, so a position compare would call a leg carrying less parity than its floor "at floor".

**Why "off" is `8:0`, not `1:0`:** in UDPspeeder mode 0 the data count caps how many segments a
packet may split into; `1:0` disables the splitter, so any datagram larger than the FEC MTU is
dropped (a large-packet black hole). `8:0` sends zero parity (same "off" bandwidth) while keeping
the splitter working.

## Control endpoints survive the boot race
The sbfd `/state` and FEC `/fec` listeners bind their management-overlay address with `IP_FREEBIND`,
so they come up even if the overlay interface isn't assigned yet at boot. Without it, the control
plane could silently fail to bind until a restart.

## Secrets never live in version control
Setup generates the WireGuard keypair and the UDPspeeder shared key at deploy time. The WireGuard
**private key is loaded into the interface via `PostUp` from a 0600 file** and is never written into
`wg0.conf` — so the conf is safe to template/version and a config rewrite can't accidentally drop the
key. Only **public** keys and the shared UDPspeeder key are exchanged between the two ends.

## Single operator switch, process stays up
The `:8081` UI can disable FEC in both directions with one switch. "Disabled" forces the off tier
(`8:0`) and freezes adaptation — it does **not** stop the UDPspeeder process, because the data path
runs *through* UDPspeeder and stopping it would black-hole the tunnel.

## Third-party components are integrated, not bundled
engarde (the multipath relay), UDPspeeder/speederv2 (the FEC transport), and WireGuard are separate
projects with their own licenses. PathFuse invokes them as external programs and the deploy kit
*fetches/builds* them; it never redistributes their code, which keeps PathFuse's own code cleanly MIT.
