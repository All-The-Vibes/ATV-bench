"""Interactive selection for `atv-bench quickstart` — an arrow-key model picker.

Design goals:
  * Great interactive UX (arrow keys) via questionary when a TTY is present.
  * Fully scriptable: an explicit choice (``--model``) or non-interactive mode NEVER blocks on
    a TUI, so CI and headless runs work.
  * Fail closed: a cancelled picker or an empty catalog with no explicit choice raises rather
    than silently running an eval with no model.
"""
from __future__ import annotations

import sys
from typing import Sequence

from atv_bench.models import ModelChoice


def _questionary_select(message: str, choices: list, default=None):
    """Thin indirection over questionary.select so tests can monkeypatch it without the TUI.

    Imported lazily: questionary is only needed on the interactive path.
    """
    import questionary

    return questionary.select(message, choices=choices, default=default)


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


def select_model(
    choices: Sequence[ModelChoice],
    *,
    preselected: str | None = None,
    non_interactive: bool | None = None,
) -> str:
    """Resolve the model id to evaluate with.

    Priority:
      1. ``preselected`` (from ``--model``) — honored verbatim, even if not in ``choices``
         (the upstream CLI is authoritative on what routes).
      2. Non-interactive (explicitly, or no TTY): the ``is_current`` default, else the first
         choice. Raises if there are no choices.
      3. Interactive: an arrow-key questionary picker; the ``is_current`` model is highlighted.
         A cancelled picker raises.
    """
    if preselected:
        return preselected

    if non_interactive is None:
        non_interactive = not _stdin_is_tty()

    if non_interactive:
        if not choices:
            raise ValueError(
                "no model to evaluate with: pass --model <id> (no curated models for this harness)"
            )
        current = next((c for c in choices if c.is_current), None)
        return (current or choices[0]).id

    if not choices:
        raise ValueError("no model to evaluate with: pass --model <id>")

    import questionary

    q_choices = [
        questionary.Choice(title=f"{c.label}" + ("  ← your configured model" if c.is_current else ""),
                           value=c.id)
        for c in choices
    ]
    default_val = next((c.id for c in choices if c.is_current), None)
    answer = _questionary_select("Select a model to evaluate:", q_choices, default_val).ask()
    if not answer:
        raise ValueError("model selection cancelled — no model chosen, aborting")
    return answer
