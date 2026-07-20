"""Regression tests for the installable, source-pinned CodeClash run extra."""
from __future__ import annotations

import builtins
import re
import tomllib
from pathlib import Path

import pytest

from atv_bench.codeclash_env import (
    CODECLASH_INSTALL_HINT,
    CODECLASH_LIGHTCYCLES_PIN,
    CODECLASH_PIN,
    CODECLASH_REQUIREMENT,
    CODECLASH_UBUNTU_2204_DIGEST,
    CODECLASH_VERSION,
    CodeClashUnavailable,
    import_codeclash,
)

ROOT = Path(__file__).parent.parent


def test_run_extra_installs_the_exact_upstream_codeclash_pin():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    run = project["project"]["optional-dependencies"]["run"]

    assert CODECLASH_REQUIREMENT in run
    assert project["tool"]["hatch"]["metadata"]["allow-direct-references"] is True
    assert project["tool"]["uv"]["link-mode"] == "copy"
    assert re.fullmatch(r"[0-9a-f]{40}", CODECLASH_PIN)
    assert CODECLASH_REQUIREMENT.endswith(f"@{CODECLASH_PIN}")
    assert "github.com/CodeClash-ai/CodeClash.git" in CODECLASH_REQUIREMENT
    assert all(item != "codeclash" for item in run), (
        "a bare registry dependency is unsatisfiable because CodeClash is not on PyPI"
    )


def test_install_metadata_does_not_claim_an_absent_vendor_tree():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    module = (ROOT / "src" / "atv_bench" / "codeclash_env.py").read_text(
        encoding="utf-8"
    )

    assert "vendor/CodeClash" not in pyproject
    assert "vendor/CodeClash" not in module
    assert CODECLASH_VERSION == f"git@{CODECLASH_PIN[:12]}"


def test_packaged_lightcycles_builder_pins_game_source_and_base_image():
    dockerfile = (
        ROOT
        / "src"
        / "atv_bench"
        / "assets"
        / "codeclash-lightcycles.Dockerfile"
    )
    text = dockerfile.read_text(encoding="utf-8")

    assert dockerfile.is_file()
    assert f"ubuntu@{CODECLASH_UBUNTU_2204_DIGEST}" in text
    assert f"git checkout --detach {CODECLASH_LIGHTCYCLES_PIN}" in text
    assert CODECLASH_PIN in text


def test_missing_codeclash_error_points_to_the_real_install_path(monkeypatch):
    real_import = builtins.__import__

    def reject_codeclash(name, *args, **kwargs):
        if name == "codeclash" or name.startswith("codeclash."):
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_codeclash)
    with pytest.raises(CodeClashUnavailable) as exc:
        import_codeclash()

    message = str(exc.value)
    assert CODECLASH_INSTALL_HINT in message
    assert "uv sync --extra run" in message
    assert "vendor/CodeClash" not in message


def test_sdist_configuration_is_an_explicit_source_only_allowlist():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sdist = project["tool"]["hatch"]["build"]["targets"]["sdist"]

    assert set(sdist["include"]) == {
        "/src/atv_bench",
        "/pyproject.toml",
        "/README.md",
        "/LICENSE",
        "/NOTICE",
    }
    excluded = set(sdist["exclude"])
    for repository_only in (
        "/reports",
        "/tests",
        "/tasks",
        "/docs",
        "/examples",
        "/league",
        "/schemas",
        "/scripts",
        "/arena",
        "/.github",
    ):
        assert repository_only in excluded
