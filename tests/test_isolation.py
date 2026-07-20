"""Per-run HOME isolation + A/A serialization guard (ENG-B).

RED tests, written before implementation. They pin the intended API:

    from atv_bench.isolation import isolated_home, aa_lock

`isolated_home(harness_config)` is a context manager yielding the env dict an
adapter subprocess must run under — HOME/XDG_CONFIG_HOME/XDG_CACHE_HOME all point
at a per-run mkdtemp dir (NOT the shared host $HOME), seeded from the harness's
own config so the harness still finds its skills/plugins/MCP.

`aa_lock(game, pair)` is a per-(game, adapter-pair) filelock that serializes
A-vs-A self-play so two concurrent same-harness runs cannot cross-contaminate.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from atv_bench.isolation import aa_lock, isolated_home


def _real_home() -> str:
    return os.environ.get("HOME", "")


def test_per_run_temp_home(tmp_path: Path) -> None:
    """Two concurrent isolated runs get DIFFERENT HOMEs, neither the host $HOME.

    Asserts the env dict an adapter would pass carries isolated HOME +
    XDG_CONFIG_HOME + XDG_CACHE_HOME rooted at a per-run mkdtemp dir.
    """
    cfg = tmp_path / "harness_cfg"
    cfg.mkdir()
    (cfg / "settings.json").write_text("{}")

    with isolated_home(cfg) as env_a, isolated_home(cfg) as env_b:
        home_a = env_a["HOME"]
        home_b = env_b["HOME"]

        # Distinct per-run homes.
        assert home_a != home_b
        # Neither is the shared host HOME.
        assert home_a != _real_home()
        assert home_b != _real_home()
        # XDG dirs follow the isolated HOME, not the host.
        for env in (env_a, env_b):
            assert env["XDG_CONFIG_HOME"].startswith(env["HOME"])
            assert env["XDG_CACHE_HOME"].startswith(env["HOME"])
        # The isolated homes actually exist on disk.
        assert Path(home_a).is_dir()
        assert Path(home_b).is_dir()


def test_isolated_home_seeded_from_harness_config(tmp_path: Path) -> None:
    """The isolated HOME is seeded with the harness's config dir.

    Isolation must not blind the harness to its own skills/plugins/MCP: a marker
    placed in the harness config source must be visible inside the isolated HOME.
    """
    cfg = tmp_path / "harness_cfg"
    (cfg / "skills").mkdir(parents=True)
    marker = cfg / "skills" / "MARKER.md"
    marker.write_text("seed-me")

    with isolated_home(cfg) as env:
        home = Path(env["HOME"])
        seeded = [p for p in home.rglob("MARKER.md")]
        assert seeded, f"harness config not seeded into isolated HOME {home}"
        assert seeded[0].read_text() == "seed-me"


def test_aa_serialization_lock(tmp_path: Path) -> None:
    """A second concurrent A/A acquisition BLOCKS until the first releases."""
    game = "lightcycles"
    pair = ("claude-code", "claude-code")

    acquired_second = threading.Event()

    with aa_lock(game, pair, lock_dir=tmp_path):
        def _try_second() -> None:
            # Non-blocking-ish: must time out because the outer lock is held.
            with pytest.raises(Exception):
                with aa_lock(game, pair, lock_dir=tmp_path, timeout=0.2):
                    acquired_second.set()

        t = threading.Thread(target=_try_second)
        t.start()
        t.join(timeout=5)
        assert not t.is_alive(), "second acquisition never returned (deadlock)"
        # Mutual exclusion: the second acquisition did NOT enter the critical section.
        assert not acquired_second.is_set()


class _RecordingContainer:
    """Minimal TreeContainerLike: empty tree in, records nothing out."""

    def read_tree(self) -> dict:
        return {}

    def write_tree(self, files: dict) -> None:  # pragma: no cover - not reached on NO_EDIT
        pass


class _RecordingAdapter:
    """Thin fake adapter that spawns a REAL subprocess under req.env and records the
    HOME that subprocess actually saw. This proves the isolated env reaches the wire,
    not just the returned env dict."""

    name = "recording"

    def __init__(self) -> None:
        self.req_env: dict | None = None
        self.subprocess_home: str | None = None

    def run(self, req):  # AdapterRequest -> AdapterResult
        import subprocess
        import sys

        from atv_bench.adapters.contract import AdapterResult, AdapterStatus, Usage

        self.req_env = req.env
        proc = subprocess.run(
            [sys.executable, "-c", "import os; print(os.environ.get('HOME', ''))"],
            capture_output=True,
            text=True,
            env=req.env,  # exactly what the real adapters pass
        )
        self.subprocess_home = proc.stdout.strip()
        return AdapterResult(
            status=AdapterStatus.NO_EDIT, diff="", log="", usage=Usage()
        )


def test_subprocess_receives_isolated_home(tmp_path: Path) -> None:
    """E2E: driving the PRODUCTION construction path must give the adapter subprocess
    an isolated HOME, never the host $HOME.

    Regression guard: previously integration._make_harness_player built
    HarnessPlayerCore WITHOUT env=, so isolated_home() had zero production call sites
    and the subprocess inherited the host HOME. This drives the real production seam
    (integration.run_isolated_edit_turn) with a recording adapter and asserts the
    subprocess env carried the per-run temp HOME.
    """
    from atv_bench import integration
    from atv_bench.players import clear_artifact_cache

    clear_artifact_cache()

    cfg = tmp_path / "harness_cfg"
    cfg.mkdir()
    (cfg / "settings.json").write_text("{}")

    adapter = _RecordingAdapter()
    integration.run_isolated_edit_turn(
        adapter=adapter,
        container=_RecordingContainer(),
        home=cfg,
        goal="noop",
        model="auto",
        player_id="p-iso",
        game="lightcycles",
        prompt_version="edit@1",
        bot_file="main.py",
    )

    assert adapter.req_env is not None, "adapter got env=None (dead plumbing)"
    iso_home = adapter.req_env["HOME"]
    # The subprocess ACTUALLY ran under the isolated HOME.
    assert adapter.subprocess_home == iso_home
    # And it is NOT the host HOME.
    assert adapter.subprocess_home != _real_home()
    assert iso_home != _real_home()


def test_ab_runs_do_not_share_home(tmp_path: Path) -> None:
    """A-vs-B (different adapters) each get their own isolated HOME, no leak."""
    cfg_a = tmp_path / "cfg_a"
    cfg_b = tmp_path / "cfg_b"
    for c in (cfg_a, cfg_b):
        c.mkdir()
        (c / "settings.json").write_text("{}")

    with isolated_home(cfg_a) as env_a, isolated_home(cfg_b) as env_b:
        assert env_a["HOME"] != env_b["HOME"]
        assert env_a["HOME"] != _real_home()
        assert env_b["HOME"] != _real_home()
        # No shared parent tempdir leakage: writing into A's home is invisible to B.
        (Path(env_a["HOME"]) / "leak.txt").write_text("a")
        assert not (Path(env_b["HOME"]) / "leak.txt").exists()
