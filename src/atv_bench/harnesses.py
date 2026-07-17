"""Single source of truth for the coding-agent harnesses ATV-bench can fingerprint.

ATV-bench is **harness-agnostic by design**: the whole thesis is that we benchmark the
*harness* (skills, MCP servers, plugins, custom agents, config) rather than the raw model.
The CLI must therefore never present itself as a claude-code tool — it presents a generic
"fingerprint your coding-agent harness" surface and dispatches to whichever harness reader
matches the local machine (or the one the user names with `--harness`).

`live` is True only when a harness has a real, leak-safe fingerprint reader in this repo
(`fingerprint/probe.py`). v1 ships `claude-code`, `copilot-cli`, and `codex` live (each
with an allowlist-emit reader + canary leak-test — see CONTRIBUTING → Add a harness
adapter). Adding a harness flips its status here and nothing else in the CLI needs to
change — this mirrors `games.py`.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Harness:
    """One coding-agent harness. `live` is False for a planned harness with no reader yet."""

    key: str
    title: str
    live: bool
    config_root: str  # config dir under $HOME the probe reads (names/counts only)
    summary: str


# Ordered: live harnesses first, then planned. `claude-code` is the shipped reader —
# its allowlist-emit probe (fingerprint/probe.py) produces the leak-safe manifest.
HARNESSES: tuple[Harness, ...] = (
    Harness(
        key="claude-code",
        title="Claude Code",
        live=True,
        config_root=".claude",
        summary="Reads ~/.claude names + counts only (model, skills, MCP servers, plugins, "
        "custom agents) into the fixed leak-safe schema. v1 fingerprint reader.",
    ),
    Harness(
        key="copilot-cli",
        title="GitHub Copilot CLI",
        live=True,
        config_root=".copilot",
        summary="Reads ~/.copilot names + counts only (model from settings.json, MCP "
        "servers from mcp-config.json, skills/agents/plugins from installed-plugins/) "
        "into the fixed leak-safe schema, minus anything disabled in settings.",
    ),
    Harness(
        key="codex",
        title="OpenAI Codex CLI",
        live=True,
        config_root=".codex",
        summary="Reads ~/.codex names + counts only (model from config.toml top-level key, "
        "MCP servers from [mcp_servers.*] tables, skills from skills/, custom agents from "
        "prompts/) into the fixed leak-safe schema. Never reads provider tables or headers.",
    ),
)

DEFAULT_HARNESS = "claude-code"

_BY_KEY: dict[str, Harness] = {h.key: h for h in HARNESSES}

# The primary config file each live harness reads. A harness is only "present" for
# detection/ambiguity purposes when this file exists — a bare config DIR (e.g. a stale
# empty ~/.codex/) is NOT a detected harness. Single source of truth: the CLI's
# fail-closed message and the detect surfaces both consult this.
PRIMARY_CONFIG: dict[str, str] = {
    "claude-code": "settings.json",
    "copilot-cli": "settings.json",
    "codex": "config.toml",
}


def harness_config_present(key: str, base: Path | None = None) -> bool:
    """True when `key`'s primary config FILE exists under `base` (default $HOME).

    Detection is based on the primary config file, not the bare config dir — a stale
    empty config root (dir present, no primary file) must NOT count as a detected
    harness, matching the reader taxonomy where an absent primary config is skipped.
    """
    h = _BY_KEY.get(key)
    if h is None or not h.live:
        return False
    root = (base if base is not None else Path.home()) / h.config_root
    primary = PRIMARY_CONFIG.get(key)
    if primary is None:
        return root.exists()
    # is_symlink() so a dangling-symlink primary still counts as present (it exists as a
    # link) — the probe/CLI will fail it closed rather than treat it as absent.
    p = root / primary
    return p.exists() or p.is_symlink()


def get_harness(key: str) -> Harness | None:
    """Return the Harness for `key`, or None if unknown."""
    return _BY_KEY.get(key)


def is_live(key: str) -> bool:
    """True only if `key` is a known harness with a real, leak-safe fingerprint reader."""
    h = _BY_KEY.get(key)
    return bool(h and h.live)


def live_keys() -> list[str]:
    """Keys of harnesses that can actually be fingerprinted right now."""
    return [h.key for h in HARNESSES if h.live]


def config_root_for(key: str, home: Path | None = None) -> Path:
    """Absolute config root for `key` under `home` (default $HOME)."""
    base = home if home is not None else Path.home()
    h = _BY_KEY.get(key)
    root = h.config_root if h is not None else ".claude"
    return Path(base) / root


def harness_for_root(root: Path) -> str | None:
    """Resolve a harness key from an explicit config ROOT (e.g. a `--home` value).

    Matches on the root's basename against each harness's `config_root` (e.g. a directory
    named `.codex` → codex, `.claude` → claude-code). Returns the LIVE harness key, or
    None if the basename matches no live harness. This lets `--home <root>` pick the right
    reader without a `--harness` flag, instead of falling back to $HOME auto-detect (which
    would mis-resolve a codex root as claude-code on a machine that also has ~/.claude).
    """
    name = Path(root).name
    for h in HARNESSES:
        if h.live and h.config_root == name:
            return h.key
    return None


def detect_harness(home: Path | None = None) -> str | None:
    """Best-effort local harness detection: first LIVE harness whose PRIMARY CONFIG exists.

    Returns the harness key, or None if no live harness's primary config is present.
    Detection is based on the primary config file (not the bare config dir), so a stale
    empty config root does not falsely register as a detected harness. Limited to live
    harnesses — pointing a user at a planned harness we can't fingerprint is a dead end.
    """
    base = home if home is not None else Path.home()
    for h in HARNESSES:
        if h.live and harness_config_present(h.key, base):
            return h.key
    return None


def assert_probeable(key: str) -> None:
    """Raise ValueError with an actionable message if `key` can't be fingerprinted.

    Used by the CLI to fail closed: an unknown harness or a planned harness (no reader)
    must never silently produce an empty/placeholder fingerprint.
    """
    h = _BY_KEY.get(key)
    if h is None:
        raise ValueError(
            f"unknown harness {key!r}. Available: {', '.join(live_keys())} "
            f"(see `atv-bench harnesses`)."
        )
    if not h.live:
        raise ValueError(
            f"harness {key!r} is planned, not fingerprintable yet — it has no leak-safe "
            f"reader in this repo. Use one of: {', '.join(live_keys())} "
            f"(see `atv-bench harnesses`)."
        )
