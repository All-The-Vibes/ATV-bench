"""End-to-end live match proof (gated @integration — needs Docker + real harness CLIs).

These are NOT run in the default suite (they build Docker images and drive real claude/
copilot CLIs, costing minutes + tokens). They are the executable proof that the Phase-1
spine is real: a genuine harness-vs-harness match through the patched CodeClash path.

Run explicitly:
    GITHUB_TOKEN=$(gh auth token) uv run pytest -m integration tests/test_e2e_live.py -s
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from atv_bench.codeclash_env import codeclash_available

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not codeclash_available(), reason="vendored CodeClash not installed"),
]


def _docker_ok() -> bool:
    from atv_bench.preflight import check_docker
    return check_docker().ok


@pytest.mark.skipif(not _docker_ok(), reason="Docker daemon not available")
def test_live_aa_selfplay_lightcycles(tmp_path):
    """A/A self-play: the same harness builds a bot twice, they compete, a real winner
    is adjudicated by CodeClash. Zero hand-written bot code; model tag parsed from the
    real run. This is the cheapest honest proof (Phase 1.5 variance control)."""
    import shutil

    if not shutil.which("claude"):
        pytest.skip("claude CLI not on PATH")
    os.environ.setdefault("GITHUB_TOKEN", "")

    from atv_bench.players import clear_artifact_cache
    from atv_bench.runner import RunConfig, run_live_match

    clear_artifact_cache()
    cfg = RunConfig(game="lightcycles", a="claude-code", b="claude-code",
                    model="claude-opus-4-8", rounds=1)
    raw = run_live_match(cfg, output_dir=tmp_path / "run")
    md = raw["metadata"]
    # A real tournament produced round stats with a decided (or tied) outcome.
    assert "round_stats" in md
    # round 0 is the identical-seed control; at least one round exists.
    assert md["round_stats"], "no rounds recorded"
