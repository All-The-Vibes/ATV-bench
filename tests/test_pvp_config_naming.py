"""Live-caught bug: a player name with a colon (bare:claude-code) is an illegal git branch name.

CodeClash creates a branch named after the player, so build_pvp_config must emit a
git-branch-safe player NAME while keeping the `agent` routing key intact.
"""
from __future__ import annotations

import re

from atv_bench.config import build_pvp_config


def _branch_safe(name: str) -> bool:
    # git refname rules (subset): no ':', no whitespace, no '~^?*[', no leading '-'.
    return bool(name) and not re.search(r"[:\s~^?*\[\]\\]", name) and not name.startswith("-")


def test_bare_player_name_is_git_branch_safe():
    """bare:claude-code must yield a branch-safe player name (the colon is illegal in refs)."""
    cfg = build_pvp_config(game="lightcycles", a="claude-code", b="bare:claude-code",
                           model="sonnet", rounds=1)
    names = [p["name"] for p in cfg["players"]]
    agents = [p["agent"] for p in cfg["players"]]
    # the AGENT routing key is preserved (resolve_player_class needs the real 'bare:' key)
    assert agents == ["claude-code", "bare:claude-code"]
    # every player NAME is a legal git branch component
    for n in names:
        assert _branch_safe(n), f"player name {n!r} is not git-branch-safe"
    # names stay DISTINCT so containers/branches don't collide
    assert len(set(names)) == 2


def test_safe_names_stay_distinct_for_selfplay():
    """A/A self-play still yields two distinct, branch-safe names."""
    cfg = build_pvp_config(game="lightcycles", a="claude-code", b="claude-code",
                           model="sonnet", rounds=1)
    names = [p["name"] for p in cfg["players"]]
    assert len(set(names)) == 2
    assert all(_branch_safe(n) for n in names)


def test_summarize_tournament_maps_sanitized_winner_back_to_harness():
    """CodeClash reports the winner by the branch-safe name (bare-claude-code); summarize must
    map it back to the harness key (bare:claude-code) so the rating row accepts it."""
    from atv_bench.runner import RunConfig, summarize_tournament

    cfg = RunConfig(game="lightcycles", a="claude-code", b="bare:claude-code",
                    model="sonnet", rounds=1)
    raw = {"metadata": {"round_stats": {
        "0": {"winner": "Tie"},
        "1": {"winner": "bare-claude-code"},   # branch-safe name from CodeClash
    }}}
    outcome, _models = summarize_tournament(raw, cfg)
    assert outcome["winner"] == "bare:claude-code"  # mapped back to the harness key
