"""Single source of truth for the coding-agent harnesses ATV-bench can fingerprint.

ATV-bench is **harness-agnostic by design**: the whole thesis is that we benchmark the
*harness* (skills, MCP servers, plugins, custom agents, config) rather than the raw model.
The CLI must therefore never present itself as a claude-code tool — it presents a generic
"fingerprint your coding-agent harness" surface and dispatches to whichever harness reader
matches the local machine (or the one the user names with `--harness`).

`live` is True only when a harness has a real, leak-safe fingerprint reader in this repo
(`fingerprint/probe.py`). v1 ships `claude-code` live; `copilot-cli` and `codex` are
planned (their surfaces emit as `unknown[]` until a reader + canary leak-test lands — see
CONTRIBUTING → Add a harness adapter). Adding a harness flips its status here and nothing
else in the CLI needs to change — this mirrors `games.py`.
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
        live=False,
        config_root=".codex",
        summary="Planned. No leak-safe reader in this repo yet — its surfaces emit as "
        "unknown[] until a reader + canary leak-test ships (see CONTRIBUTING → Add a harness).",
    ),
)

DEFAULT_HARNESS = "claude-code"

_BY_KEY: dict[str, Harness] = {h.key: h for h in HARNESSES}


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


def detect_harness(home: Path | None = None) -> str | None:
    """Best-effort local harness detection: first LIVE harness whose config dir exists.

    Returns the harness key, or None if no live harness's config root is present. Detection
    is deliberately limited to live harnesses — pointing a user at a planned harness we
    can't yet fingerprint would be a dead end.
    """
    base = home if home is not None else Path.home()
    for h in HARNESSES:
        if h.live and (Path(base) / h.config_root).exists():
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
