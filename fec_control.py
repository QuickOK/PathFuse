"""Pure FEC-ratio decision logic shared by the client (sbfd-ctl) and relay
(udpspeeder_fec) adaptive controllers. No I/O except write_fifo()."""
from dataclasses import dataclass
import os

# The "off" tier is 8:0 (zero parity = no redundancy/overhead), NOT 1:0.
# In UDPspeeder mode 0 the data count x caps how many segments a packet may be
# split into; x=1 disables the splitter, so any packet whose wg datagram exceeds
# --mtu (1250) gets dropped (large-packet/PMTU black-hole). x=8 keeps the
# splitter working while still sending no parity.
DEFAULT_LOSS_TABLE = [
    {"max_loss_pct": 0.5,   "fec": "8:0"},
    {"max_loss_pct": 2.0,   "fec": "8:2"},
    {"max_loss_pct": 5.0,   "fec": "8:4"},
    {"max_loss_pct": 10.0,  "fec": "8:6"},
    {"max_loss_pct": 100.0, "fec": "8:8"},
]

# Cellular-specific ladder: caps at 8:1 (12.5%). HARQ/RLC below IP already
# repairs random loss; ratios past 8:1 mostly buy metered overhead without
# recovering the correlated (handoff) loss bursts block FEC can't cover.
DEFAULT_CELL_LOSS_TABLE = [
    {"max_loss_pct": 0.5,   "fec": "8:0"},
    {"max_loss_pct": 2.0,   "fec": "20:1"},
    {"max_loss_pct": 5.0,   "fec": "12:1"},
    {"max_loss_pct": 100.0, "fec": "8:1"},
]

# Operator-selectable FEC modes.
#   off          - force the off tier (8:0), independent of loss
#   fixed        - hold a single operator-chosen ratio
#   adaptive     - scale ratio to measured loss via the loss table (idle = 8:0)
#   min_adaptive - same as adaptive but the idle/backoff tier is replaced with
#                  a small always-on floor (default 20:1 = ~5% overhead)
MODE_OFF = "off"
MODE_FIXED = "fixed"
MODE_ADAPTIVE = "adaptive"
MODE_MIN_ADAPTIVE = "min_adaptive"
ALL_MODES = (MODE_OFF, MODE_FIXED, MODE_ADAPTIVE, MODE_MIN_ADAPTIVE)
DEFAULT_MODE = MODE_MIN_ADAPTIVE
DEFAULT_FLOOR_RATIO = "20:1"
DEFAULT_FIXED_RATIO = "20:1"
OFF_RATIO = "8:0"
FIXED_RATIO_PRESETS = ("20:1", "12:1", "8:1", "8:2", "8:4", "8:6", "8:8")
DEFAULT_SIGNAL_FLOOR_FEC = "12:1"

# Snap target for percent entry. Separate from FIXED_RATIO_PRESETS because it
# must include the zero-parity rung (an operator may legitimately ask for 0%)
# while the dropdown must not offer it — "off" is already its own mode.
# Ascending overhead; resolve_ratio relies on that ordering for tie-breaking.
RATIO_LADDER = ("8:0", "20:1", "12:1", "8:1", "8:2", "8:4", "8:6", "8:8")


def parse_ratio(s):
    a, b = s.split(":")
    return int(a), int(b)


def format_ratio(a, b):
    return f"{a}:{b}"


def validate_ratio(a, b):
    return a >= 1 and b >= 0 and (a + b) <= 254


def ratio_overhead_pct(a, b):
    """Parity as a percentage of data. '20:1' -> 5.0, '8:8' -> 100.0."""
    return (b / a) * 100.0


def resolve_ratio(text):
    """Normalize an operator entry to a canonical 'x:y' ratio.

    Accepts an explicit 'x:y' (returned verbatim once validated) or a percent
    of overhead with an optional trailing '%', snapped to the nearest rung of
    RATIO_LADDER. Snapping rather than deriving an exact ratio keeps every
    value that reaches UDPspeeder on a rung already exercised in production.

    Raises ValueError with a message fit to return to an HTTP caller.
    """
    # bool is an int subclass and would otherwise stringify as 'True'.
    if not isinstance(text, str):
        raise ValueError("ratio must be a string like '8:2' or '5%'")
    s = text.strip()
    if not s:
        raise ValueError("ratio must not be empty")
    if ":" in s:
        try:
            a, b = parse_ratio(s)
        except (ValueError, AttributeError):
            raise ValueError(f"{s!r} is not a ratio like '8:2'")
        if not validate_ratio(a, b):
            raise ValueError(f"{s!r} out of bounds (a>=1, b>=0, a+b<=254)")
        return format_ratio(a, b)
    try:
        pct = float(s[:-1] if s.endswith("%") else s)
    except ValueError:
        raise ValueError(
            f"{s!r} is not a ratio like '8:2' or a percent like '5%'")
    if not (0.0 <= pct <= 100.0):
        raise ValueError(f"percent must be 0..100, got {pct:g}")
    best_rung, best_dist = None, None
    for rung in RATIO_LADDER:
        dist = abs(ratio_overhead_pct(*parse_ratio(rung)) - pct)
        # <= (not <) with an ascending ladder means an exact tie is won by the
        # later, higher-overhead rung: an operator asking for protection must
        # never silently get less than they typed.
        if best_dist is None or dist <= best_dist:
            best_rung, best_dist = rung, dist
    return best_rung


_warned_ratios = set()


def safe_ratio(value, fallback, logger=None):
    """Coerce a ratio to canonical 'x:y', falling back if it is unusable.

    The API boundary normalizes on write, so stored values are canonical — but
    config and overlay files are hand-editable, and an unusable ratio reaching
    write_fifo would break the control loop rather than a single request.

    Warns once per distinct bad value: the callers run in a sub-second loop, so
    an unconditional warning would spam the journal forever."""
    if not value:
        return fallback
    try:
        return resolve_ratio(value)
    except ValueError:
        if logger:
            # A malformed value can be an unhashable list/dict from a
            # hand-edited JSON file; key the debounce on its repr so the set
            # lookup can't raise TypeError inside the fallback path itself.
            key = value if isinstance(value, str) else repr(value)
            if key not in _warned_ratios:
                _warned_ratios.add(key)
                logger.warning("fec ratio %r unusable; falling back to %s",
                               value, fallback)
        return fallback


def loss_to_level(loss_pct, table=DEFAULT_LOSS_TABLE):
    for i, row in enumerate(table):
        if loss_pct <= row["max_loss_pct"]:
            return i
    return len(table) - 1


def level_to_ratio(level, table=DEFAULT_LOSS_TABLE):
    level = max(0, min(level, len(table) - 1))
    return table[level]["fec"]


def ratio_to_level(ratio, table=DEFAULT_LOSS_TABLE):
    for i, row in enumerate(table):
        if row["fec"] == ratio:
            return i
    return 0


def is_level_at_max(level, table=DEFAULT_LOSS_TABLE):
    """True once the adaptive engine has climbed to the top row of the
    ACTIVE profile's table. Callers must pass the profile table actually
    driving the level (e.g. resolve_fec_profile's prof_table) — comparing
    against a different table (like the base cfg.fec.loss_table while a
    shorter cellular profile table is active) under- or over-counts the
    max index and silently breaks the fec-at-max notification."""
    return level >= len(table) - 1


def ratio_rung(ratio, table=DEFAULT_LOSS_TABLE):
    """Index of the highest rung in `table` whose overhead is <= this ratio's.

    Unlike ratio_to_level this does NOT require an exact match: an operator's
    floor or fixed ratio is free to sit between two rungs (20:1 = 5% falls
    between the base table's 8:0 and 8:2), and a strict lookup would collapse
    every such value to 0. Rounding DOWN is deliberate — the UI's pip row must
    never claim more protection than the ratio actually delivers.

    An unparseable ratio is rung 0: a display helper must degrade, not raise.
    """
    try:
        pct = ratio_overhead_pct(*parse_ratio(ratio))
    except (ValueError, AttributeError, TypeError, ZeroDivisionError):
        return 0
    rung = 0
    for i, row in enumerate(table):
        try:
            if ratio_overhead_pct(*parse_ratio(row["fec"])) <= pct:
                rung = i
        except (ValueError, AttributeError, TypeError,
                KeyError, ZeroDivisionError):
            continue
    return rung


def ladder_state(mode, ratio, floor_ratio, table=DEFAULT_LOSS_TABLE):
    """{levels, floor_level, applied_level} describing where the ratio on the
    wire sits on the active profile's ladder. Consumed by the UI's pip row,
    which shows the applied level RELATIVE to the floor.

    applied_level comes from the ratio actually applied, not the adaptive
    engine's level index: that makes it right in every mode for free — fixed
    and off ignore the engine entirely, and the signal floor lifts the ratio
    without the published level always reflecting it.

    floor_level is 0 outside min_adaptive; nothing is being held up there, so
    the whole ladder is available.
    """
    return {
        "levels": len(table),
        "floor_level": (ratio_rung(floor_ratio, table)
                        if mode == MODE_MIN_ADAPTIVE else 0),
        "applied_level": ratio_rung(ratio, table) if ratio else 0,
    }


def mode_aware_level(mode, up_count, loss_pct, table=DEFAULT_LOSS_TABLE,
                     full_min_up_wans=2, backoff_ratio="8:0"):
    """In full-redundancy with enough healthy links, engarde already
    duplicates, so the loss-driven level backs off to backoff_ratio.
    Otherwise scale FEC to measured loss.

    This bounds only the ADAPTIVE input to apply_mode: in min_adaptive the
    floor re-raises anything below it, so a configured floor keeps its
    parity on top of full-mode duplication (deliberate — see
    docs/design-notes.md "Mode-aware backoff")."""
    if mode == "full" and up_count >= full_min_up_wans:
        return ratio_to_level(backoff_ratio, table)
    return loss_to_level(loss_pct, table)


@dataclass
class FecHysteresis:
    ramp_up_ticks: int = 2
    ramp_down_hold_s: float = 20.0


@dataclass
class FecRuntime:
    current_level: int = 0
    up_streak: int = 0
    last_change_ts: float = 0.0


def step_level(target_level, rt, hyst, now):
    """Pure: returns (new_FecRuntime, changed). Ramps up only after
    ramp_up_ticks consecutive higher targets; ramps down only after the
    current level has been held ramp_down_hold_s seconds."""
    cur = rt.current_level
    if target_level > cur:
        streak = rt.up_streak + 1
        if streak >= hyst.ramp_up_ticks:
            return FecRuntime(target_level, 0, now), True
        return FecRuntime(cur, streak, rt.last_change_ts), False
    if target_level < cur:
        if now - rt.last_change_ts >= hyst.ramp_down_hold_s:
            return FecRuntime(target_level, 0, now), True
        return FecRuntime(cur, 0, rt.last_change_ts), False
    return FecRuntime(cur, 0, rt.last_change_ts), False


@dataclass
class SignalThresholds:
    """RSRQ is primary with an explicit hysteresis pair; RSRP is consulted
    ONLY when RSRQ is absent (some firmwares omit it), with its own 2 dB band."""
    rsrq_degrade_db: float = -12.0
    rsrq_recover_db: float = -10.0
    rsrp_degrade_dbm: float = -110.0
    rsrp_recover_dbm: float = -108.0


class SignalFloor:
    """Hysteretic radio-degradation latch. update() is called once per control
    tick with the freshest telemetry (None = unavailable); callers gate
    freshness — a stale sample must be passed as (None, None), which
    disengages: measured loss stays the ground truth (fail-open)."""

    def __init__(self, thresholds=None):
        self.th = thresholds or SignalThresholds()
        self.engaged = False

    def update(self, rsrq, rsrp):
        if rsrq is not None:
            if rsrq < self.th.rsrq_degrade_db:
                self.engaged = True
            elif rsrq >= self.th.rsrq_recover_db:
                self.engaged = False
        elif rsrp is not None:
            if rsrp < self.th.rsrp_degrade_dbm:
                self.engaged = True
            elif rsrp >= self.th.rsrp_recover_dbm:
                self.engaged = False
        else:
            self.engaged = False
        return self.engaged


def apply_signal_floor(level, engaged, table, floor_fec=DEFAULT_SIGNAL_FLOOR_FEC):
    """Lift the loss-driven level to the signal-floor rung while degraded.
    ratio_to_level returns 0 for a ratio not in the table, making the floor a
    no-op there — a profile/table mismatch must weaken, never break."""
    if not engaged:
        return level
    return max(level, ratio_to_level(floor_fec, table))


def apply_mode(mode, adaptive_ratio, fixed_ratio=DEFAULT_FIXED_RATIO,
               floor_ratio=DEFAULT_FLOOR_RATIO):
    """Map (mode, adaptive_ratio) → the ratio actually sent to UDPspeeder.

    adaptive_ratio is whatever the loss-driven logic chose (e.g. level_to_ratio
    over the loss table). For 'off' and 'fixed' it is ignored; for
    'min_adaptive' it's lifted to floor_ratio when it would be 8:0.
    """
    if mode == MODE_OFF:
        return OFF_RATIO
    if mode == MODE_FIXED:
        return fixed_ratio
    if mode == MODE_MIN_ADAPTIVE:
        # A floor must hold against EVERY tier below it, not just the 8:0 idle
        # tier. While the floor was hardcoded to 20:1 (5%) this never mattered —
        # 5% sits below the lowest non-off rung (8:2 = 25%) — but an operator
        # who sets a floor of 8:4 would otherwise see adaptive 8:2 sail under it.
        try:
            adaptive_pct = ratio_overhead_pct(*parse_ratio(adaptive_ratio))
            floor_pct = ratio_overhead_pct(*parse_ratio(floor_ratio))
        except (ValueError, AttributeError, ZeroDivisionError):
            return adaptive_ratio
        if adaptive_pct < floor_pct:
            return floor_ratio
    return adaptive_ratio


def normalize_mode(value):
    """Coerce arbitrary input to a valid mode string, falling back to DEFAULT_MODE."""
    if isinstance(value, str) and value in ALL_MODES:
        return value
    return DEFAULT_MODE


def fifo_command(ratio):
    return f"fec {ratio}\n"


def write_fifo(path, ratio, logger=None):
    """Best-effort non-blocking write of 'fec x:y' to the UDPspeeder FIFO.
    Returns False (never raises) if the FIFO is absent or has no reader."""
    cmd = fifo_command(ratio).encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        if logger:
            logger.warning("fec fifo open failed (%s): %s", path, e)
        return False
    try:
        os.write(fd, cmd)
        return True
    except OSError as e:
        if logger:
            logger.warning("fec fifo write failed (%s): %s", path, e)
        return False
    finally:
        os.close(fd)
