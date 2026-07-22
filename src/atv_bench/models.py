"""Routable-model catalog for `atv-bench quickstart`.

The user picks a model that routes through THEIR harness auth (Claude Code / Copilot OAuth /
Codex login). ATV-bench does not — and cannot — be the authority on which models a given
account can route to; the upstream CLI is. So this module is deliberately a *curated,
best-effort* list plus one honest escape hatch: whatever model the harness is CURRENTLY
configured with (read off the leak-safe fingerprint manifest) is always offered, even if it is
not in our list, and marked as the default.

Contract:
  * ``available_models_for(harness)`` — the curated list (may be empty for an unknown harness).
  * ``models_with_current(harness, manifest)`` — the curated list with the manifest's configured
    model prepended (if not already present) and marked ``is_current`` for the picker default.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Mapping


@dataclasses.dataclass(frozen=True)
class ModelChoice:
    """One selectable model. ``id`` is the string passed to the harness CLI's --model."""

    id: str
    label: str
    is_current: bool = False


# Curated, routable-by-default model ids per harness family. These are the common defaults a
# Claude Code / Copilot / Codex user is likely authed for. NOT exhaustive and NOT authoritative
# — the CLI validates at run time; a model missing here can still be typed via --model, and the
# harness's own configured model is always surfaced by models_with_current().
_CATALOG: dict[str, tuple[ModelChoice, ...]] = {
    "claude-code": (
        ModelChoice("claude-opus-4-6", "Claude Opus 4.6 (deepest reasoning)"),
        ModelChoice("claude-sonnet-4-6", "Claude Sonnet 4.6 (best all-round coding)"),
        ModelChoice("claude-haiku-4-5", "Claude Haiku 4.5 (fast / cheap)"),
    ),
    "copilot-cli": (
        ModelChoice("gpt-5", "GPT-5 (Copilot)"),
        ModelChoice("gpt-4.1", "GPT-4.1 (Copilot)"),
        ModelChoice("claude-sonnet-4-6", "Claude Sonnet 4.6 (via Copilot)"),
    ),
    "codex": (
        ModelChoice("gpt-5-codex", "GPT-5 Codex"),
        ModelChoice("o4-mini", "o4-mini (fast reasoning)"),
        ModelChoice("gpt-5", "GPT-5"),
    ),
}

_NONCONFIGURED = {"", "unknown", "auto", "none"}


def available_models_for(harness: str) -> list[ModelChoice]:
    """Curated routable models for a harness, or [] for an unknown/non-runnable harness."""
    return list(_CATALOG.get(harness, ()))


def _configured_model(manifest: Mapping[str, Any] | None) -> str | None:
    """The harness's currently-configured model from the fingerprint manifest, or None."""
    if not manifest:
        return None
    m = manifest.get("model")
    if isinstance(m, str) and m.strip().lower() not in _NONCONFIGURED:
        return m.strip()
    return None


def models_with_current(
    harness: str, manifest: Mapping[str, Any] | None = None
) -> list[ModelChoice]:
    """Curated list with the manifest's configured model marked ``is_current`` and sorted first.

    If the configured model is not already in the curated list it is PREPENDED (the upstream CLI
    is authoritative — a configured model routes even if we didn't list it). If no model is
    configured (or it's 'unknown'), nothing is marked current and the plain curated list is
    returned so the picker still has options.
    """
    curated = available_models_for(harness)
    current = _configured_model(manifest)
    if current is None:
        return curated

    out: list[ModelChoice] = []
    seen = False
    for c in curated:
        if c.id == current:
            out.append(dataclasses.replace(c, is_current=True))
            seen = True
        else:
            out.append(c)
    if not seen:
        out.insert(0, ModelChoice(current, f"{current} (your configured model)", is_current=True))
        return out
    # sort the current one to the front, preserving the rest of the order
    out.sort(key=lambda c: (not c.is_current,))
    return out
