"""Sandbox-flag parity tripwire (santa re-review #8) — runs on EVERY push.

The gated integration test (`tests/test_action_malicious_bot.py`) claims to exercise a
hostile bot under "the SAME container flags the league Action uses". That claim is only
true if its `SANDBOX_FLAGS` and image actually match the real match step in
`.github/workflows/league.yml`. The PR shipped with drift: the test used `--memory 256m`
and `python:3.12-alpine`, while the workflow uses `--memory 512m` and the in-repo arena
image. Drift silently weakens the parity guarantee.

This tripwire parses the real `docker run` invocation out of the workflow and asserts the
integration test's constants are in sync — so a future flag change in one place fails
here until the other is updated. Mirrors the test_publish_race / test_action_isolation
tripwire pattern (comment-stripped, real-behavior assertions).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import tests.test_action_malicious_bot as mal

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "league.yml"


@pytest.fixture(scope="module")
def match_run_shell() -> str:
    """The match job's `docker run` step shell, comment-stripped (real behavior only)."""
    wf = yaml.safe_load(WORKFLOW.read_text())
    for step in wf["jobs"]["match"]["steps"]:
        run = str(step.get("run", ""))
        if "docker run" in run:
            lines = []
            for ln in run.splitlines():
                code = ln.split("#", 1)[0]
                if code.strip():
                    lines.append(code)
            return "\n".join(lines)
    raise AssertionError("no `docker run` step found in the match job")


def _flag_value(shell: str, flag: str) -> str | None:
    """Return the token following `flag` in the workflow's docker run (e.g. --memory 512m)."""
    m = re.search(rf"{re.escape(flag)}\s+(\S+)", shell)
    return m.group(1) if m else None


def test_memory_cap_matches_workflow(match_run_shell):
    wf_mem = _flag_value(match_run_shell, "--memory")
    assert wf_mem is not None, "workflow match step must set --memory"
    idx = mal.SANDBOX_FLAGS.index("--memory")
    assert mal.SANDBOX_FLAGS[idx + 1] == wf_mem, (
        f"integration test --memory ({mal.SANDBOX_FLAGS[idx + 1]}) drifted from the "
        f"workflow ({wf_mem}); the 'exact sandbox flags' parity claim is broken"
    )


def test_memory_swap_matches_workflow(match_run_shell):
    wf_swap = _flag_value(match_run_shell, "--memory-swap")
    assert wf_swap is not None, "workflow match step must set --memory-swap"
    idx = mal.SANDBOX_FLAGS.index("--memory-swap")
    assert mal.SANDBOX_FLAGS[idx + 1] == wf_swap, (
        f"integration test --memory-swap drifted from the workflow ({wf_swap})"
    )


def test_core_isolation_flags_present_in_both(match_run_shell):
    # Every hard-isolation flag the workflow relies on must also be asserted by the gated
    # integration test, else the test could pass under weaker isolation than production.
    required = [
        "--network", "none",
        "--cpus", "1",
        "--pids-limit", "128",
        "--read-only",
        "--user", "65534:65534",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
    ]
    for tok in required:
        assert tok in match_run_shell, f"workflow match step missing {tok!r}"
        assert tok in mal.SANDBOX_FLAGS, (
            f"integration test SANDBOX_FLAGS missing {tok!r} present in the workflow"
        )


def test_integration_test_uses_the_real_arena_image(match_run_shell):
    # The workflow runs the bot in the in-repo arena image (built from arena/Dockerfile),
    # NOT python:3.12-alpine. The integration test must exercise the SAME image so its
    # parity claim holds; a stock alpine image has a different runtime/attack surface.
    assert "atv-bench/arena" in match_run_shell, (
        "workflow match step must run the in-repo arena image"
    )
    assert mal.ARENA_IMAGE_REF == "atv-bench/arena", (
        "integration test must build+run the in-repo arena image, not a stock base image"
    )
    assert "alpine" not in mal.ARENA_IMAGE_REF, (
        "integration test must not run against python:3.12-alpine (drifted from arena image)"
    )
