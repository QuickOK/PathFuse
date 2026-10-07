"""Pins on docs prose that describes behaviour, where no other test reads the doc."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_architecture_actual_exit_check_names_both_pages_and_the_exit_tile():
    # The Actual-exit check bullet names both of the check's pages (a mismatch, and
    # the check itself failing) and the Exit KPI tile, the wall layout's view of it.
    text = (ROOT / "docs" / "architecture.md").read_text()
    m = re.search(r"- \*\*Actual-exit check:\*\*(.*?)\n\n", text, re.S)
    assert m, "the Actual-exit check bullet is gone"
    bullet = " ".join(m.group(1).split())
    assert "as the Exit KPI tile" in bullet
    assert "for `mismatch_checks` checks in a row" in bullet
    assert "fails `error_checks` checks in a row" in bullet
