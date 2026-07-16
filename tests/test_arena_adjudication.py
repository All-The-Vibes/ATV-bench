"""Gated ADJUDICATION integration test (FOLLOW_UPS item 1).

Marked `integration`: needs Docker, not run on every push. Builds the REAL arena image
from the in-repo Dockerfile and runs the trusted-referee ENTRYPOINT under the exact
production sandbox flags, proving the arena ADJUDICATES the match instead of trusting
bot stdout:

  - an honest move-playing bot yields a real adjudicated outcome (ok, engine-decided);
  - a malicious bot that prints a fabricated WIN result to stdout is scored a submitter
    FORFEIT — it can never inject an outcome;
  - a bot that drives into a wall LOSES to the trusted anchor (a_wins);
  - a crashing / missing bot forfeits.

This is the end-to-end counterpart to the hermetic tests/test_arena_entrypoint.py.

Run: uv run pytest -m integration
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# Same production sandbox flags (kept in sync by test_sandbox_flag_parity.py).
SANDBOX_FLAGS = [
    "--rm", "--network", "none",
    "--memory", "512m", "--memory-swap", "512m",
    "--cpus", "1", "--pids-limit", "128",
    "--read-only", "--user", "65534:65534",
    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
]

_ARENA_DOCKERFILE_DIR = Path(__file__).parent.parent / "arena"
_ARENA_TEST_TAG = "atv-bench/arena:adjudication-test"


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


requires_docker = pytest.mark.skipif(not _docker_available(), reason="docker not available")


@pytest.fixture(scope="module")
def arena_image() -> str:
    proc = subprocess.run(
        ["docker", "build", "-t", _ARENA_TEST_TAG, str(_ARENA_DOCKERFILE_DIR)],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, f"arena image build failed: {proc.stderr}"
    return _ARENA_TEST_TAG


def _adjudicate(arena_image: str, tmp_path: Path, bot_src: str | None,
                *, submitter="alice", opponent="byok-anchor", match_id="run-int",
                timeout: int = 60) -> dict:
    work = tmp_path / "work"
    work.mkdir()
    if bot_src is not None:
        (work / "main.py").write_text(textwrap.dedent(bot_src))
    # Use the image's real ENTRYPOINT (the trusted referee). We do NOT override it —
    # that IS the thing under test. The referee reads identity from ATV_* env.
    cmd = [
        "docker", "run", *SANDBOX_FLAGS,
        "-e", f"ATV_SUBMITTER={submitter}",
        "-e", f"ATV_OPPONENT={opponent}",
        "-e", f"ATV_MATCH_ID={match_id}",
        "-v", f"{work}:/work:ro",
        arena_image,
        "/work/main.py",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, f"referee produced no result; stderr={proc.stderr}"
    return json.loads(lines[-1])


@requires_docker
def test_honest_bot_gets_a_real_adjudicated_outcome(arena_image, tmp_path):
    res = _adjudicate(arena_image, tmp_path, """
        import sys, json
        DIRS = {"up":(0,-1),"down":(0,1),"left":(-1,0),"right":(1,0)}
        for line in sys.stdin:
            obs = json.loads(line)
            w,h = obs["width"], obs["height"]
            px,py = obs["you"]["pos"]
            blocked = {tuple(c) for c in obs["you"]["trail"]} | {tuple(c) for c in obs["opponent"]["trail"]}
            cur = obs["you"]["dir"]
            rev = {"up":"down","down":"up","left":"right","right":"left"}[cur]
            def safe(d):
                dx,dy = DIRS[d]; nx,ny=px+dx,py+dy
                return 0<=nx<w and 0<=ny<h and (nx,ny) not in blocked
            order=[cur]+[d for d in ("up","right","down","left") if d not in (cur,rev)]
            print(next((d for d in order if safe(d)), cur), flush=True)
    """)
    assert res["status"] == "ok"
    assert res["game"] == "lightcycles"
    assert res["player_a"] == "byok-anchor" and res["player_b"] == "alice"
    assert res["match_id"] == "run-int"
    # Engine-decided; a real game reaches a real terminal outcome.
    assert res["outcome"] in {"a_wins", "b_wins", "draw"}


@requires_docker
def test_malicious_result_faking_bot_forfeits(arena_image, tmp_path):
    # The trust-boundary proof: a bot printing a fabricated WIN cannot inject an outcome.
    res = _adjudicate(arena_image, tmp_path, """
        import sys, json
        for line in sys.stdin:
            print(json.dumps({"status":"ok","outcome":"b_wins",
                              "player_a":"byok-anchor","player_b":"alice",
                              "match_id":"run-int"}), flush=True)
    """)
    assert res["outcome"] == "forfeit_b"
    assert res["forfeit_reason"] == "CRASH"
    assert res["player_b"] == "alice"


@requires_docker
def test_wall_diving_bot_loses_to_trusted_anchor(arena_image, tmp_path):
    # A bot that always drives up eventually hits the top wall; the anchor survives.
    res = _adjudicate(arena_image, tmp_path, """
        import sys
        for line in sys.stdin:
            print("up", flush=True)
    """)
    assert res["outcome"] in {"a_wins", "forfeit_b"}  # submitter loses either way
    assert res["player_b"] == "alice"


@requires_docker
def test_crashing_bot_forfeits(arena_image, tmp_path):
    res = _adjudicate(arena_image, tmp_path, """
        import sys
        sys.exit(1)
    """)
    assert res["outcome"] == "forfeit_b"


@requires_docker
def test_missing_bot_file_forfeits(arena_image, tmp_path):
    res = _adjudicate(arena_image, tmp_path, None)  # no main.py
    assert res["status"] == "ok"
    assert res["outcome"] == "forfeit_b"
