"""Pins on the static UI files that no served-page or behaviour test covers."""
import re
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "ui"


def test_kpi_strip_has_a_column_per_tile():
    # The only thing that keeps the KPI strip and its tiles in step is a number in
    # wall.css: a sixth tile, or a revert of the 4 -> 5 column change, would wrap a
    # tile onto its own row in both layouts with nothing to notice.
    tiles = re.findall(r'<div class="kpi" id="kpi-[a-z]+">', (UI / "index.html").read_text())
    assert len(tiles) >= 5, tiles                       # System, Mode, Active, relay sync, Exit
    css = (UI / "wall.css").read_text()
    m = re.search(r"\n\.kpi-strip \{[^}]*grid-template-columns:\s*repeat\((\d+),\s*1fr\)", css)
    assert m, "the .kpi-strip rule no longer sets repeat(N, 1fr)"
    assert int(m.group(1)) == len(tiles)
