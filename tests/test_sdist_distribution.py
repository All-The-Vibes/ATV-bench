"""Built-sdist content and size regression tests."""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).parent.parent
MAX_SDIST_BYTES = 5 * 1024 * 1024
MAX_UNPACKED_BYTES = 5 * 1024 * 1024
FORBIDDEN_TOP_LEVEL = {
    ".github",
    "_demo_replay",
    "arena",
    "docs",
    "examples",
    "leaderboard",
    "league",
    "reports",
    "schemas",
    "scripts",
    "spikes",
    "tasks",
    "tests",
}
PROOF_MEDIA_SUFFIXES = {
    ".gif",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp4",
    ".pdf",
    ".png",
    ".webm",
    ".webp",
}


def _build_sdist(tmp_path: Path) -> Path:
    out = tmp_path / "dist"
    out.mkdir()
    env = dict(os.environ)
    env["UV_LINK_MODE"] = "copy"
    if uv := shutil.which("uv"):
        command = [uv, "build", "--sdist", "--out-dir", str(out)]
    elif importlib.util.find_spec("build") is not None:
        command = [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--outdir",
            str(out),
        ]
    else:
        pytest.skip("an sdist builder (uv or python-build) is not installed")
    subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    archives = list(out.glob("*.tar.gz"))
    assert len(archives) == 1
    return archives[0]


def test_built_sdist_is_source_only_bounded_and_free_of_local_evidence(tmp_path):
    archive = _build_sdist(tmp_path)
    assert archive.stat().st_size < MAX_SDIST_BYTES

    with tarfile.open(archive, "r:gz") as package:
        members = [member for member in package.getmembers() if member.isfile()]

    assert members
    roots = {PurePosixPath(member.name).parts[0] for member in members}
    assert len(roots) == 1
    root = roots.pop()
    relative = {
        PurePosixPath(member.name).relative_to(root).as_posix(): member.size
        for member in members
    }

    assert sum(relative.values()) < MAX_UNPACKED_BYTES
    assert len(relative) < 200
    assert "pyproject.toml" in relative
    assert "README.md" in relative
    assert "LICENSE" in relative
    assert "NOTICE" in relative
    assert "src/atv_bench/__init__.py" in relative
    assert "src/atv_bench/view/index.html" in relative
    assert "src/atv_bench/view/eval.html" in relative
    assert "src/atv_bench/eval/report.schema.json" in relative

    top_level = {
        PurePosixPath(path).parts[0]
        for path in relative
        if PurePosixPath(path).parts
    }
    assert not (top_level & FORBIDDEN_TOP_LEVEL)
    assert not any(
        PurePosixPath(path).suffix.lower() in PROOF_MEDIA_SUFFIXES
        for path in relative
    )
    assert not any(path.endswith(".jsonl") for path in relative)


def test_built_sdist_can_produce_an_importable_clean_wheel(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build a wheel directly from an sdist archive")
    archive = _build_sdist(tmp_path)
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        [uv, "build", "--wheel", str(archive), "--out-dir", str(wheel_dir)],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]

    with zipfile.ZipFile(wheel) as package:
        names = package.namelist()
    assert "atv_bench/__init__.py" in names
    assert "atv_bench/view/index.html" in names
    assert "atv_bench/view/eval.html" in names
    assert "atv_bench/eval/report.schema.json" in names
    assert not any(
        PurePosixPath(name).parts
        and PurePosixPath(name).parts[0] in FORBIDDEN_TOP_LEVEL
        for name in names
    )
    assert not any(
        PurePosixPath(name).suffix.lower() in PROOF_MEDIA_SUFFIXES
        for name in names
    )

    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(wheel)!r}); "
                "import atv_bench; "
                "assert atv_bench.__version__ == '0.1.0'"
            ),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
