"""Local arena-image integrity tripwire.

The arena remains available for explicit local testing and demonstrations, but GitHub
Actions never builds or runs it. These tests validate the image definition and ensure the
baked referee is byte-identical to the unit-tested source package.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parent.parent
DOCKERFILE = ROOT / "arena" / "Dockerfile"


def test_arena_dockerfile_exists():
    assert DOCKERFILE.exists()


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


ARENA_PKG = ROOT / "arena" / "pkg" / "atv_bench" / "arena"
SRC_PKG = ROOT / "src" / "atv_bench" / "arena"


def test_entrypoint_is_the_trusted_referee_not_bare_python():
    text = DOCKERFILE.read_text()
    ep_lines = [ln for ln in text.splitlines()
                if ln.split("#", 1)[0].strip().upper().startswith("ENTRYPOINT")]
    assert ep_lines, "arena/Dockerfile must declare an ENTRYPOINT"
    ep = ep_lines[-1]
    assert "atv_bench.arena" in ep, (
        f"arena ENTRYPOINT must run the trusted referee (`python3 -m atv_bench.arena`), "
        f"not a bare interpreter that trusts bot stdout. Got: {ep.strip()!r}"
    )
    assert ep.strip() != 'ENTRYPOINT ["python3"]', (
        "ENTRYPOINT must not be a bare python3 (that trusts bot stdout as the result)"
    )


def test_referee_package_is_baked_into_the_image():
    text = DOCKERFILE.read_text()
    copy_lines = [ln for ln in text.splitlines()
                  if ln.split("#", 1)[0].strip().upper().startswith("COPY")]
    assert any("pkg/" in ln for ln in copy_lines), (
        "arena/Dockerfile must COPY the trusted referee package (pkg/) into the image"
    )
    assert (ARENA_PKG / "__main__.py").exists(), "baked referee must include __main__.py entrypoint"
    assert (ARENA_PKG / "engine.py").exists(), "baked referee must include the engine"
    assert (ARENA_PKG / "referee.py").exists(), "baked referee must include the referee"


def test_baked_referee_is_byte_identical_to_tested_src():
    for name in ("__init__.py", "engine.py", "referee.py", "__main__.py"):
        baked = (ARENA_PKG / name).read_bytes()
        src = (SRC_PKG / name).read_bytes()
        assert baked == src, (
            f"arena/pkg/atv_bench/arena/{name} has drifted from "
            f"src/atv_bench/arena/{name} — re-sync so the image runs tested code"
        )
    assert (ROOT / "arena" / "pkg" / "atv_bench" / "__init__.py").exists()
