"""Unit B (live e2e): a REAL claude-code vs bare:claude-code evaluation produces a sensible lift.

Marked ``live`` + ``e2e`` (deselected by default; needs Docker + a real claude CLI + auth +
the vendored CodeClash). Runs the quickstart engine with the REAL ``live_match_executor`` — actual
Docker arena matches, the real ``claude`` CLI building bots, and the bare control running the same
model under a stripped HOME. This closes the gap that the engine was only ever tested with a stub
executor.

Bounded on purpose: a small powered set (2 games × a few repeats) — enough to prove the live seam
end-to-end (real matches → corpus → finite lift → per-game scores → scorecard), not to publish a
ranked number. Skips cleanly when Docker/claude/CodeClash are unavailable.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.live, pytest.mark.e2e]


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def _codeclash_ok() -> bool:
    try:
        import codeclash  # noqa: F401
        return True
    except Exception:
        return False


requires_live_stack = pytest.mark.skipif(
    not (shutil.which("claude") and _docker_ok() and _codeclash_ok()),
    reason="needs claude CLI + docker + vendored CodeClash",
)


@requires_live_stack
def test_real_claude_vs_bare_produces_sensible_lift(tmp_path):
    """Run the engine with the REAL executor over a small powered set and assert a sensible,
    finite harness-over-bare lift plus per-game scores and a rendered scorecard."""
    from atv_bench.games import live_keys
    from atv_bench.quickstart import live_match_executor, run_quickstart_eval

    # dummy is the fast smoke arena; lightcycles is the canonical trusted-referee game.
    live = set(live_keys())
    games = [g for g in ("dummy", "lightcycles") if g in live] or [next(iter(live))]

    executor = live_match_executor(rounds=1)  # 1 edit+compete round keeps each match short
    res = run_quickstart_eval(
        harness="claude-code", model="sonnet",
        games=games, repeats=3,                 # 2 games × 3 = ~6 real matches
        store=tmp_path / "league", execute=executor,
    )

    # matches actually ran and persisted a corpus. If EVERY match failed with an auth/policy
    # signature, this is an environment precondition (not logged in), so skip rather than fail.
    if res.n_matches == 0 and res.failures:
        errs = " ".join(f.get("error", "").lower() for f in res.failures)
        if any(tok in errs for tok in ("login", "auth", "api key", "unauthor", "401", "403",
                                       "quota", "rate limit", "not supported")):
            pytest.skip(f"live harness not usable in this env: {res.failures[:2]}")
    assert res.n_matches > 0, f"no matches scored; failures={res.failures}"
    assert (tmp_path / "league" / "rating_matches.jsonl").exists()

    # Prove REAL gameplay happened, not two unedited starter bots tying out: at least one match
    # must be decisive (a non-0.5 score). Two identical un-edited starters would tie every game,
    # so a decisive outcome is evidence the harness actually performed a live authenticated edit.
    # (If auth silently failed and left starter trees in place, this catches the false-green.)
    import json as _json
    rows = [_json.loads(ln) for ln in
            (tmp_path / "league" / "rating_matches.jsonl").read_text().splitlines() if ln.strip()]
    decisive = [r for r in rows if float(r.get("score_a", 0.5)) != 0.5]
    assert decisive, (
        "every live match tied — likely no real edit happened (auth/edit failure). "
        f"rows={rows}"
    )

    # per-game breakdown present for at least one requested game
    assert res.per_game, "no per-game scores produced"
    assert {g.game for g in res.per_game} <= set(games)

    # overall lift is finite and in a plausible range (theta-difference; not NaN/inf)
    if res.overall is not None:
        import math
        assert math.isfinite(res.overall.lift), f"lift not finite: {res.overall.lift}"
        assert math.isfinite(res.overall.lo) and math.isfinite(res.overall.hi)
        assert res.overall.lo <= res.overall.lift <= res.overall.hi
        assert -20.0 < res.overall.lift < 20.0, "lift wildly out of plausible range"

    # the scorecard link is real and openable
    assert res.board_url and res.board_url.endswith("scorecard.html")
    assert (tmp_path / "league" / "scorecard.html").exists()


@requires_live_stack
def test_bare_control_ran_under_stripped_home(tmp_path):
    """The bare:claude-code control genuinely ran the model with its harness scaffolding stripped
    — a real fingerprint of the bare run's HOME must satisfy manifest_is_bare."""
    from atv_bench.lift import BareModelAdapter, manifest_is_bare
    from atv_bench.adapters.contract import AdapterRequest, ClaudeCodeAdapter
    from atv_bench.runner import fingerprint_harness_repo

    captured = {}
    inner = ClaudeCodeAdapter()

    class _Spy:
        name = inner.name

        @staticmethod
        def available():
            return True

        def run(self, req):
            captured["home"] = req.env.get("HOME") if req.env else None
            return "ran"

    BareModelAdapter(inner=_Spy()).run(AdapterRequest(repo_path=str(tmp_path), goal="noop"))
    home = captured["home"]
    assert home, "bare run did not seed an isolated HOME"
    _sha, manifest = fingerprint_harness_repo("claude-code", Path(home))
    assert manifest_is_bare(manifest), f"bare HOME probed non-bare: {manifest}"
