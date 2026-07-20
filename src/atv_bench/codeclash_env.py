"""Import shim + source pin for the official CodeClash dependency.

CodeClash is a research repo, not published to PyPI, so the ``run`` optional
dependency installs it from an immutable upstream Git commit. This module
is the ONE place ATV-bench imports CodeClash internals, so a drift in its
(internal, unstable) API surface fails loudly here -- see
``tests/test_codeclash_drift.py`` --
instead of deep inside the runner.

Pinned upstream commit:
    f0694c64ecf6abfca2bc867bad2de9333fef5be8

Seam facts verified against this pin (the drift test asserts they still hold):
  * `codeclash.agents.get_agent(config, game_context, environment) -> Player`
    maps a function-local literal {"dummy", "mini"} — NOT a module dict, so it
    cannot be extended; `integration.register()` must REPLACE it.
  * `codeclash.tournaments.pvp` does `from codeclash.agents import get_agent`,
    binding the name into its own module namespace. Agents are constructed
    HOST-SIDE in `PvpTournament.__init__` (verified: `agent.run()` executes in
    the tournament process via ThreadPoolExecutor, talking to Docker through
    `environment.execute`). So the authoritative monkeypatch site is
    `codeclash.tournaments.pvp.get_agent`.
  * `Player.__init__(self, config, environment, game_context)`.
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

# Pinned upstream commit this build is verified against.
CODECLASH_PIN = "f0694c64ecf6abfca2bc867bad2de9333fef5be8"
CODECLASH_REPOSITORY = "https://github.com/CodeClash-ai/CodeClash.git"
CODECLASH_SOURCE_ENV = "ATV_CODECLASH_SOURCE"
CODECLASH_LIGHTCYCLES_PIN = "32e4218844805340371e9fe11902a49e5a1e40a6"
CODECLASH_LIGHTCYCLES_IMAGE = "codeclash/lightcycles"
CODECLASH_UBUNTU_2204_DIGEST = (
    "sha256:0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982"
)
CODECLASH_REQUIREMENT = (
    f"codeclash @ git+{CODECLASH_REPOSITORY}@{CODECLASH_PIN}"
)
CODECLASH_INSTALL_HINT = (
    "install the pinned dependency with `uv sync --extra run` "
    f"or point {CODECLASH_SOURCE_ENV} at an exact checkout of {CODECLASH_PIN}"
)

# Short version tag stamped into match records / leaderboard rows (schema v2).
CODECLASH_VERSION = f"git@{CODECLASH_PIN[:12]}"
_CODECLASH_SOURCE_ROOT: Path | None = None


class _ContainerPath(str):
    """POSIX container path that still satisfies CodeClash's path-like operations."""

    def __new__(cls, value: str) -> "_ContainerPath":
        normalized = str(PurePosixPath(value))
        if not normalized.startswith("/"):
            raise ValueError("container paths must be absolute POSIX paths")
        return super().__new__(cls, normalized)

    def __truediv__(self, other: object) -> "_ContainerPath":
        return _ContainerPath(str(PurePosixPath(self) / str(other)))


def _git_check(source_root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
        }
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return subprocess.run(
        ["git", "-C", str(source_root), *argv],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        env=environment,
        shell=False,
        check=False,
    )


def _verified_source_root(candidate: Path) -> Path | None:
    try:
        root = candidate.expanduser().resolve(strict=True)
    except OSError:
        return None
    if root.name == "codeclash" and (root / "arenas").is_dir():
        root = root.parent
    asset = (
        root
        / "codeclash"
        / "arenas"
        / "lightcycles"
        / "LightCycles.Dockerfile"
    )
    if not asset.is_file() or asset.is_symlink():
        return None
    try:
        head = _git_check(root, "rev-parse", "HEAD")
        worktree = _git_check(
            root,
            "diff",
            "--quiet",
            "HEAD",
            "--",
            "codeclash/arenas",
        )
        index = _git_check(
            root,
            "diff",
            "--cached",
            "--quiet",
            "HEAD",
            "--",
            "codeclash/arenas",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if (
        head.returncode != 0
        or head.stdout.strip().lower() != CODECLASH_PIN
        or worktree.returncode != 0
        or index.returncode != 0
    ):
        return None
    return root


def _uv_cache_candidates() -> tuple[Path, ...]:
    roots: list[Path] = []
    configured = os.environ.get("UV_CACHE_DIR")
    if configured:
        roots.append(Path(configured))
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "uv" / "cache")
    if os.environ.get("XDG_CACHE_HOME"):
        roots.append(Path(os.environ["XDG_CACHE_HOME"]) / "uv")
    roots.append(Path.home() / ".cache" / "uv")

    candidates: list[Path] = []
    seen: set[str] = set()
    for cache_root in roots:
        checkouts = cache_root / "git-v0" / "checkouts"
        try:
            owners = sorted(checkouts.iterdir())[:128]
        except OSError:
            continue
        for owner in owners:
            try:
                revisions = sorted(owner.iterdir())[:128]
            except OSError:
                continue
            for revision in revisions:
                key = os.path.normcase(str(revision.resolve(strict=False)))
                if key not in seen:
                    seen.add(key)
                    candidates.append(revision)
    return tuple(candidates)


def resolve_codeclash_source() -> Path:
    """Locate the exact upstream checkout that carries non-wheel arena assets."""

    global _CODECLASH_SOURCE_ROOT
    if _CODECLASH_SOURCE_ROOT is not None:
        return _CODECLASH_SOURCE_ROOT

    candidates: list[Path] = []
    explicit = os.environ.get(CODECLASH_SOURCE_ENV)
    if explicit:
        candidates.append(Path(explicit))
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            import codeclash
    except Exception as exc:
        raise CodeClashUnavailable(
            "CodeClash is not importable. "
            f"{CODECLASH_INSTALL_HINT}."
        ) from exc
    candidates.append(Path(codeclash.__file__).resolve().parent.parent)
    candidates.extend(_uv_cache_candidates())

    for attempt in range(3):
        for candidate in candidates:
            verified = _verified_source_root(candidate)
            if verified is not None:
                _CODECLASH_SOURCE_ROOT = verified
                return verified
        if attempt < 2:
            time.sleep(0.1 * (attempt + 1))
    raise CodeClashUnavailable(
        "CodeClash Python modules are installed, but the pinned arena source assets "
        "are unavailable. The built VCS wheel omits arena Dockerfiles. "
        f"{CODECLASH_INSTALL_HINT}."
    )


def _bind_source_assets(source_root: Path) -> None:
    import codeclash
    from codeclash import arenas as cc_arenas
    from codeclash import constants as cc_constants

    codeclash.REPO_DIR = source_root
    cc_constants.REPO_DIR = source_root
    package_root = source_root / "codeclash"
    for arena_class in cc_arenas.ARENAS:
        module = sys.modules.get(arena_class.__module__)
        if module is None:
            continue
        relative = Path(*arena_class.__module__.split(".")[1:]).with_suffix(".py")
        source_file = package_root / relative
        if source_file.is_file() and not source_file.is_symlink():
            module.__file__ = str(source_file)


def _bind_posix_container_paths() -> None:
    """Undo host-OS ``Path`` coercion for CodeClash's in-container paths."""

    replacements = {
        "DIR_WORK": _ContainerPath("/workspace"),
        "DIR_LOGS": _ContainerPath("/logs"),
    }
    for module_name, module in tuple(sys.modules.items()):
        if not module_name.startswith("codeclash.") or module is None:
            continue
        for name, value in replacements.items():
            if hasattr(module, name):
                setattr(module, name, value)


def import_codeclash():
    """Import the CodeClash internals ATV-bench depends on, or raise a clear error.

    Returns a namespace object with the seam handles. Raises `CodeClashUnavailable`
    (an ImportError subclass) with an actionable message if CodeClash is not
    installed — the runner turns this into exit code 9 (codeclash-dep).
    """
    try:
        # mini-swe-agent prints migration text and a host config path at import time.
        # Those are neither API output nor safe public verification evidence.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            from codeclash import agents as cc_agents
            from codeclash.agents.player import Player
            from codeclash.agents.utils import GameContext
            from codeclash.tournaments import pvp as cc_pvp
    except Exception as exc:  # pragma: no cover - exercised via CodeClashUnavailable
        raise CodeClashUnavailable(
            "CodeClash is not importable. ATV-bench requires the official upstream "
            f"commit {CODECLASH_PIN[:12]}; {CODECLASH_INSTALL_HINT}, or run "
            "`atv-bench doctor` for a full prerequisite report."
        ) from exc
    source_root = resolve_codeclash_source()
    _bind_source_assets(source_root)
    _bind_posix_container_paths()

    return _Seam(
        agents=cc_agents,
        pvp=cc_pvp,
        Player=Player,
        GameContext=GameContext,
        get_agent=cc_agents.get_agent,
    )


class CodeClashUnavailable(ImportError):
    """Raised when the pinned CodeClash dependency cannot be imported."""


class _Seam:
    __slots__ = ("agents", "pvp", "Player", "GameContext", "get_agent")

    def __init__(self, *, agents, pvp, Player, GameContext, get_agent):
        self.agents = agents
        self.pvp = pvp
        self.Player = Player
        self.GameContext = GameContext
        self.get_agent = get_agent


def codeclash_available() -> bool:
    try:
        import_codeclash()
        return True
    except CodeClashUnavailable:
        return False
