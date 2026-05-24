"""Pure FEC-ratio decision logic shared by the truck (sbfd-ctl) and OVH
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


def parse_ratio(s):
    a, b = s.split(":")
    return int(a), int(b)


def format_ratio(a, b):
    return f"{a}:{b}"


def validate_ratio(a, b):
    return a >= 1 and b >= 0 and (a + b) <= 254


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
