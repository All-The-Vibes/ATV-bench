"""CLI-surface disambiguation tests (DX-6).

Four ways to view a match (run --demo / play / demo-match / board --demo) confuse
a new user. Section 7 either collapses demo-match into `run --demo` OR makes the
help text disambiguate each command's role so the surface isn't ambiguous.
"""
from __future__ import annotations

from typer.testing import CliRunner

from atv_bench.cli import app

runner = CliRunner()


def _command_names() -> set[str]:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    names = set()
    for line in result.output.splitlines():
        # typer lists commands indented at the start of the Commands section
        m = line.strip().split()
        if m and m[0].isidentifier() or (m and "-" in m[0]):
            names.add(m[0])
    return names


def test_match_view_commands_consolidated():
    """demo-match must be collapsed into `run --demo`, OR its help must clearly
    disambiguate it from run/play/board so the surface isn't ambiguous."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    top = result.output

    if "demo-match" not in top:
        # Consolidated away — good. Nothing further to check.
        return

    # Still present: its short help must state how it differs from `run --demo`
    # and from `play`, so a user isn't left guessing which of four to use.
    dm = runner.invoke(app, ["demo-match", "--help"])
    assert dm.exit_code == 0, dm.output
    # Normalize: strip box-drawing borders and collapse wrapped whitespace so a
    # phrase split across the help box's lines still matches.
    import re
    help_low = re.sub(r"[│|]", " ", dm.output.lower())
    help_low = re.sub(r"\s+", " ", help_low)
    disambiguated = (
        "run --demo" in help_low
        or "deprecated" in help_low
        or "prefer `run" in help_low
        or "use `run" in help_low
    )
    assert disambiguated, (
        "demo-match still overlaps `run --demo` with no disambiguation in its "
        "help — a new user faces four undifferentiated ways to view a match."
    )
