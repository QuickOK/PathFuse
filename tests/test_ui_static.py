"""Pins on the static UI files that no served-page or behaviour test covers."""
import re
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("element", ["kpi-exit-val", "kpi-exit-sub"])
def test_the_wall_lets_the_exit_tile_wrap(element):
    # The wall layout hides the control panel, so the Exit tile is its only view of the
    # actual exit: at wall widths under ~1460 px an ellipsis would cut the exit's name
    # ("relay Back…") and, under ~1500 px, the end of the reason ("since 20:4…"), so in
    # that layout the value and the sub-line wrap instead.
    css = (UI / "wall.css").read_text()
    m = re.search(r'\nbody\[data-layout="wall"\] #' + element + r'\s*\{([^}]*)\}', css)
    assert m, f"the wall layout no longer has a rule for #{element}"
    assert re.search(r"white-space:\s*normal", m.group(1)), m.group(1)
    assert re.search(r"overflow-wrap:\s*anywhere", m.group(1)), m.group(1)
