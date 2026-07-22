"""Per-run HOME isolation + A/A serialization guard (ENG-B).

Two primitives keep concurrent harness runs from cross-contaminating each
other's on-disk state:

- ``isolated_home(harness_config)`` — a context manager yielding the env dict an
  adapter subprocess must run under. HOME/XDG_CONFIG_HOME/XDG_CACHE_HOME all
  point at a distinct per-run ``mkdtemp`` dir (never the shared host ``$HOME``),
  seeded from the harness's own config so the harness still resolves its
  skills/plugins/MCP. The temp dir is removed on exit.

- ``aa_lock(game, pair)`` — a per-``(game, adapter-pair)`` ``filelock`` that
  serializes A-vs-A self-play so two concurrent same-harness runs cannot share
  (and clobber) one profile.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import filelock

__all__ = ["isolated_home", "aa_lock"]

# Default location for A/A serialization lockfiles.
_DEFAULT_LOCK_DIR = Path(tempfile.gettempdir()) / "atv-bench-locks"


@contextmanager
def isolated_home(harness_config: Optional[Path] = None) -> Iterator[dict]:
    """Yield an env dict rooted at a fresh per-run HOME.

    ``HOME``, ``XDG_CONFIG_HOME`` and ``XDG_CACHE_HOME`` are all pointed at a
    distinct ``mkdtemp`` directory. If ``harness_config`` is provided, its
    contents are copied into the isolated HOME so the harness still finds its
    skills/plugins/MCP. The directory is removed on exit.
    """
    root = Path(tempfile.mkdtemp(prefix="atv-home-"))
    try:
        config_home = root / ".config"
        cache_home = root / ".cache"
        config_home.mkdir(parents=True, exist_ok=True)
        cache_home.mkdir(parents=True, exist_ok=True)

        if harness_config is not None:
            src = Path(harness_config)
            if src.is_dir():
                # Seed the isolated HOME with the harness config contents.
                shutil.copytree(src, root, dirs_exist_ok=True)

        env = dict(os.environ)
        env["HOME"] = str(root)
        env["XDG_CONFIG_HOME"] = str(config_home)
        env["XDG_CACHE_HOME"] = str(cache_home)
        # Drop harness-specific HOME overrides that would point a CLI back at the HOST config,
        # defeating the isolated/stripped HOME. CODEX_HOME in particular takes precedence over
        # $HOME for codex; leaving the host value set would leak the host's model/config into a
        # bare or cloned-home run. The isolated HOME (seeded or empty) is the sole authority.
        for leak in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "COPILOT_HOME"):
            env.pop(leak, None)
        yield env
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _lock_key(game: str, pair: tuple[str, str]) -> str:
    a, b = pair
    safe = "_".join(part.replace(os.sep, "-") for part in (game, a, b))
    return f"aa-{safe}.lock"


@contextmanager
def aa_lock(
    game: str,
    pair: tuple[str, str],
    *,
    lock_dir: Optional[Path] = None,
    timeout: float = -1,
) -> Iterator[None]:
    """Serialize A/A self-play on a per-``(game, pair)`` filelock.

    Blocks until the lock is acquired. If ``timeout`` (seconds) elapses while
    another holder owns the lock, ``filelock.Timeout`` is raised.
    """
    directory = Path(lock_dir) if lock_dir is not None else _DEFAULT_LOCK_DIR
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / _lock_key(game, pair)
    lock = filelock.FileLock(str(lock_path), timeout=timeout)
    with lock:
        yield
