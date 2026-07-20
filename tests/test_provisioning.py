"""SECTION 0 provisioning tests: CodeClash vendored as a pinned git submodule
and exposed as a git-based `run` optional-dependency.

These tests are written RED-first: they must fail on the current tree because
the provisioning work (submodule conversion, git dependency) is not done yet.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

try:  # Python 3.11+ stdlib
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

CODECLASH_PIN = "f0694c64ecf6abfca2bc867bad2de9333fef5be8"


def _repo_root() -> Path:
    out = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent,
        text=True,
    ).strip()
    return Path(out)


def test_codeclash_importable() -> None:
    """CodeClash and its player agent import cleanly from the vendored source."""
    from codeclash import agents  # noqa: F401
    from codeclash.agents.player import Player  # noqa: F401

    assert Player is not None


def test_submodule_pinned() -> None:
    """vendor/CodeClash must be a real git submodule pinned to CODECLASH_PIN."""
    root = _repo_root()
    gitmodules = root / ".gitmodules"
    assert gitmodules.exists(), ".gitmodules does not exist (CodeClash is a plain clone)"

    content = gitmodules.read_text()
    assert "vendor/CodeClash" in content, ".gitmodules does not reference vendor/CodeClash"

    # The submodule gitlink must be recorded at the pinned commit.
    ls = subprocess.run(
        ["git", "ls-tree", "HEAD", "vendor/CodeClash"],
        cwd=root,
        text=True,
        capture_output=True,
    )
    assert ls.returncode == 0, f"git ls-tree failed: {ls.stderr}"
    assert "commit" in ls.stdout, (
        "vendor/CodeClash is not recorded as a submodule gitlink in the index; "
        f"got: {ls.stdout!r}"
    )
    assert CODECLASH_PIN in ls.stdout, (
        f"submodule gitlink is not pinned to {CODECLASH_PIN}; got: {ls.stdout!r}"
    )

    # The checked-out submodule working tree must be at the pin.
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=root / "vendor" / "CodeClash",
        text=True,
    ).strip()
    assert head == CODECLASH_PIN, f"submodule checked out at {head}, expected {CODECLASH_PIN}"


def test_run_extra_is_git_dependency() -> None:
    """The `run` optional-dependency must be a pinned git dependency, not bare PyPI."""
    root = _repo_root()
    with (root / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)

    run_extra = data["project"]["optional-dependencies"]["run"]
    assert run_extra != ["codeclash"], "run extra is still the bare PyPI 'codeclash' string"

    joined = " ".join(run_extra)
    assert "codeclash @ git+" in joined, f"run extra is not a git dependency: {run_extra!r}"
    assert "CodeClash-ai/CodeClash" in joined, f"run extra missing CodeClash repo: {run_extra!r}"
    assert CODECLASH_PIN in joined, f"run extra not pinned to {CODECLASH_PIN}: {run_extra!r}"
