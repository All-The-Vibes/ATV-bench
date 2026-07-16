"""Arena-image tripwire (santa re-review #2) — runs on EVERY push.

The league match job runs the submitted bot inside a container image. The PR that
introduced the league referenced `atv-bench/arena:latest` in `docker run` but shipped
NO Dockerfile and NO build step, so every match failed to pull the image and fell
through to the CRASH fallback: the ok/scoring path was dead in practice (only forfeits
were ever recorded).

This test asserts, hermetically (pure YAML/text parse, no Docker), that:
  1. a buildable arena Dockerfile exists in the repo,
  2. the match job builds it from the trusted checkout (not an unresolved pull),
  3. the image is run by a pinned local tag, never the mutable `:latest`,
  4. the base image is pinned by digest for reproducibility.

Mirrors the tests/test_action_isolation.py tripwire pattern.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "league.yml"
DOCKERFILE = ROOT / "arena" / "Dockerfile"


@pytest.fixture(scope="module")
def wf():
    assert WORKFLOW.exists(), "league.yml workflow must exist"
    return yaml.safe_load(WORKFLOW.read_text())


def test_arena_dockerfile_exists():
    assert DOCKERFILE.exists(), (
        "arena/Dockerfile must exist — the match job runs the bot inside this image. "
        "Without it, `docker run` fails and every match is a forfeit."
    )


def test_arena_dockerfile_pins_base_by_digest():
    # A mutable base tag (python:3.12) can be swapped under the sandbox. Pin by digest.
    text = DOCKERFILE.read_text()
    from_lines = [ln for ln in text.splitlines() if ln.strip().upper().startswith("FROM")]
    assert from_lines, "arena/Dockerfile must have a FROM line"
    for ln in from_lines:
        assert "@sha256:" in ln, f"arena base image must be pinned by digest, got: {ln.strip()}"


def test_arena_dockerfile_runs_as_nonroot():
    # Defense-in-depth: the sandbox already forces --user 65534, but the image should
    # not assume root either. Parse the LAST effective USER directive (comments stripped)
    # and assert it is a real non-root uid/name — `USER root`, `USER 0`, or a commented
    # line must NOT satisfy this.
    directives = []
    for ln in DOCKERFILE.read_text().splitlines():
        code = ln.split("#", 1)[0].strip()
        if code.upper().startswith("USER "):
            directives.append(code.split(None, 1)[1].strip())
    assert directives, "arena/Dockerfile must declare a USER directive (not just a comment)"
    effective = directives[-1]
    user_part = effective.split(":", 1)[0]  # strip optional :group
    assert user_part not in ("root", "0"), (
        f"arena/Dockerfile must run as a non-root USER, got: {effective!r}"
    )


def _match_run_code(wf):
    """All non-comment shell from the match job's run: steps, concatenated."""
    lines = []
    for step in wf["jobs"]["match"]["steps"]:
        for ln in str(step.get("run", "")).splitlines():
            code = ln.split("#", 1)[0]
            if code.strip():
                lines.append(code)
    return "\n".join(lines)


def test_match_job_builds_arena_image_from_trusted_checkout(wf):
    # The image must be produced by a build step from the trusted arena/ dir, not
    # pulled from an unresolved registry reference. The build CONTEXT must be exactly
    # the arena/ dir — NOT `.` (which would pull the whole trusted checkout into the
    # build, a needless surface) and never a pr-src/ path.
    code = _match_run_code(wf)
    build_lines = [ln for ln in code.splitlines() if "docker build" in ln]
    assert build_lines, "match job must `docker build` the arena image from the in-repo Dockerfile"
    for ln in build_lines:
        ctx = ln.split()[-1]  # trailing build-context arg
        assert ctx in ("./arena", "arena", "arena/"), (
            f"arena build context must be the arena/ dir, got: {ctx!r}"
        )
        assert "pr-src" not in ln, "arena build context must never be the untrusted PR head"


def test_match_job_does_not_run_mutable_latest_tag(wf):
    # `:latest` is mutable and (here) unbuilt. The actual shell (comments stripped) must
    # not reference it — comments explaining why it was removed are fine.
    code = _match_run_code(wf)
    assert "atv-bench/arena:latest" not in code, (
        "match job must not run the mutable/unbuilt `atv-bench/arena:latest` tag"
    )


def test_match_job_runs_a_built_local_image(wf):
    # The image passed to `docker run` must be EXACTLY the tag produced by `docker build
    # -t <tag>` — proving the run uses what we built, not some other/unbuilt image. Parse
    # the actual `docker run` image argument, not just "the tag appears somewhere".
    code = _match_run_code(wf)
    build_tags = re.findall(r"docker build[^\n]*-t\s+(\S+)", code)
    assert build_tags, "match job must tag the arena image it builds (`docker build -t <tag>`)"
    build_tag = build_tags[0].strip('"').strip("'")
    assert not build_tag.endswith(":latest"), "arena build tag must be pinned, not :latest"

    # `docker run [flags...] <image> <cmd...>`: the image is the first non-flag,
    # non-`docker`/`run` token that isn't an option or an option's value. The run script
    # uses backslash line-continuations, so join then tokenize.
    joined = re.sub(r"\\\s*\n", " ", code)
    run_match = re.search(r"docker run\b(.*?)(?:\n|$)", joined)
    assert run_match, "match job must `docker run` the arena image"
    run_segment = run_match.group(1)
    # The arena image token is the one that starts with the build tag's repo.
    repo = build_tag.split(":", 1)[0]
    run_images = re.findall(rf"{re.escape(repo)}:\S+", run_segment)
    assert run_images, f"`docker run` must use the built image {repo}:… (got: {run_segment.strip()[:120]})"
    run_image = run_images[0]
    assert run_image == build_tag, (
        f"`docker run` image {run_image!r} must be exactly the built tag {build_tag!r}"
    )
    assert not run_image.endswith(":latest"), "arena run image must be pinned, not :latest"
