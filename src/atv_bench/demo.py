"""Demo data for `atv-bench board --demo`.

A populated, obviously-synthetic leaderboard so a first-time user can *see* what the
board looks like — with several ranked harnesses and real fingerprint chips — before
they've submitted anything or run a single match. Nothing here touches the network or
the real league store; it writes a throwaay store into a temp dir the caller owns.

The entrants + matches are fixed (no RNG) so the demo board is deterministic.
"""
from __future__ import annotations

from typing import Any

from atv_bench.elo import ANCHOR_IDENTITY

# Synthetic entrants: (identity, harness, model, gstack, skills, mcps, plugins, agents).
# Deliberately fictional logins so no real person is implied on a demo board.
_ENTRANTS: list[dict[str, Any]] = [
    {
        "identity": "ada-demo", "harness": "claude-code", "model": "claude-opus-4-8",
        "gstack": True, "skills": ["gstack", "office-hours", "tdd"],
        "mcps": ["github", "grafana"], "plugins": ["compound-engineering"],
        "custom_agents_count": 7,
    },
    {
        "identity": "grace-demo", "harness": "claude-code", "model": "claude-sonnet-4-6",
        "gstack": True, "skills": ["gstack", "systematic-debugging"],
        "mcps": ["github"], "plugins": [], "custom_agents_count": 4,
    },
    {
        "identity": "linus-demo", "harness": "copilot-cli", "model": "gpt-5",
        "gstack": False, "skills": [], "mcps": [], "plugins": [],
        "custom_agents_count": 0,
    },
]

# Fixed head-to-head results, enough matches per entrant to clear the rated gate and
# produce a spread. player_a beats player_b unless noted.
_MATCHES: list[tuple[str, str, str]] = []


def _round_robin() -> list[tuple[str, str, str]]:
    """Deterministic results: ada > grace > linus, with a few upsets for realism."""
    names = [e["identity"] for e in _ENTRANTS]
    ada, grace, linus = names
    seq: list[tuple[str, str, str]] = []
    # 12 matches per pairing so each entrant clears MIN_RATED_MATCHES.
    for i in range(12):
        seq.append((ada, grace, "a_wins" if i % 4 else "b_wins"))      # ada mostly beats grace
        seq.append((ada, linus, "a_wins"))                              # ada beats linus
        seq.append((grace, linus, "a_wins" if i % 3 else "draw"))      # grace beats linus
    return seq


def build_demo_store(store_dir: str) -> None:
    """Populate a LeagueStore at `store_dir` with the synthetic demo league."""
    from atv_bench.store import LeagueStore

    store = LeagueStore(store_dir)
    for e in _ENTRANTS:
        store.add_submission(
            {
                "identity": e["identity"],
                "game": "lightcycles",
                "bot_sha256": (e["identity"].encode().hex() * 8)[:64].ljust(64, "0"),
                "bot_filename": "main.py",
                "pr_url": "https://github.com/All-The-Vibes/ATV-bench/pull/1",
                "logs_url": "https://all-the-vibes.github.io/ATV-bench/logs/1",
                "fingerprint": {
                    "harness": e["harness"], "model": e["model"], "gstack": e["gstack"],
                    "skills": e["skills"], "mcps": e["mcps"], "plugins": e["plugins"],
                    "custom_agents_count": e["custom_agents_count"],
                    "probe_version": "1.0.0", "unknown": [],
                },
            },
            bot_source="def move(state):\n    return 'up'\n",
        )
    for i, (a, b, outcome) in enumerate(_round_robin()):
        store.append_match({
            "match_id": f"demo-{i}",
            "player_a": a, "player_b": b,
            "outcome": outcome, "game": "lightcycles", "seed": i,
        })
