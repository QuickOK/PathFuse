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
FIXED_RATIO_PRESETS = ("20:1", "8:1", "8:2", "8:4", "8:6", "8:8")

# Snap target for percent entry. Separate from FIXED_RATIO_PRESETS because it
# must include the zero-parity rung (an operator may legitimately ask for 0%)
# while the dropdown must not offer it — "off" is already its own mode.
# Ascending overhead; resolve_ratio relies on that ordering for tie-breaking.
RATIO_LADDER = ("8:0", "20:1", "8:1", "8:2", "8:4", "8:6", "8:8")


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


def mode_aware_level(mode, up_count, loss_pct, table=DEFAULT_LOSS_TABLE,
                     full_min_up_wans=2, backoff_ratio="8:0"):
    """In full-redundancy with enough healthy links, engarde already
    duplicates, so FEC backs off. Otherwise scale FEC to measured loss."""
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
