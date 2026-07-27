"""Interactive harness selection for `atv-bench quickstart` — an arrow-key harness picker.

Mirrors `interactive.select_model`: a keyboard dropdown (via questionary) when a TTY is
present, but explicit selection (``--harness``) and non-interactive/headless modes NEVER block
on a TUI. Fail closed: a cancelled picker or an empty catalog with no explicit choice raises
rather than silently evaluating an unintended harness.

The picker offers the three live harnesses — Claude Code, GitHub Copilot CLI, OpenAI Codex CLI
— each annotated with whether its config is present and whether its CLI is on PATH, so the user
can see at a glance which harness is actually ready to evaluate.
"""
from __future__ import annotations

import dataclasses
import sys
from typing import Callable, Sequence


@dataclasses.dataclass(frozen=True)
class HarnessChoice:
    """One selectable harness plus the readiness signals shown in the picker."""

    key: str
    title: str
    configured: bool
    cli_found: bool

    @property
    def ready(self) -> bool:
        """A harness that can actually be evaluated now (config present AND CLI on PATH)."""
        return self.configured and self.cli_found

    def status_note(self) -> str:
        """Short human annotation of readiness for the dropdown row."""
        if self.ready:
            return "configured · cli found"
        if not self.cli_found:
            return "cli not on PATH"
        if not self.configured:
            return "config missing"
        return ""


def _questionary_select(message: str, choices: list, default=None):
    """Thin indirection over questionary.select so tests can monkeypatch without the TUI.

    Imported lazily: questionary is only needed on the interactive path.
    """
    import questionary

    return questionary.select(message, choices=choices, default=default)


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


def _first_ready_or_first(choices: Sequence[HarnessChoice]) -> str:
    """The first fully-ready harness, else the first choice. Assumes choices is non-empty."""
    ready = next((c for c in choices if c.ready), None)
    return (ready or choices[0]).key


def harness_choices(
    *,
    config_present: Callable[[str], bool],
    cli_present: Callable[[str], bool],
) -> list[HarnessChoice]:
    """Build annotated harness choices from the live harness registry + readiness detectors.

    ``config_present(key)`` / ``cli_present(key)`` are injected so this is unit-testable without
    touching the filesystem or PATH; the CLI wires them to
    ``harnesses.harness_config_present`` and a PATH probe.
    """
    from atv_bench.harnesses import HARNESSES

    out: list[HarnessChoice] = []
    for h in HARNESSES:
        if not h.live:
            continue
        out.append(
            HarnessChoice(
                key=h.key,
                title=h.title,
                configured=bool(config_present(h.key)),
                cli_found=bool(cli_present(h.key)),
            )
        )
    return out


def select_harness(
    choices: Sequence[HarnessChoice],
    *,
    preselected: str | None = None,
    non_interactive: bool | None = None,
    allow_fallback: bool = False,
) -> str:
    """Resolve the harness key to evaluate.

    Priority:
      1. ``preselected`` (from ``--harness``) — honored verbatim, no TUI.
      2. Non-interactive (explicitly, or no TTY): the first fully-ready harness, else the first
         choice. Raises if there are no choices.
      3. A single choice: auto-selected, no prompt.
      4. Interactive: an arrow-key questionary picker; the first ready harness is the default.
         A cancelled picker raises. If questionary is unavailable and ``allow_fallback`` is set,
         degrade to the non-interactive pick instead of raising.
    """
    if preselected:
        return preselected

    if non_interactive is None:
        non_interactive = not _stdin_is_tty()

    if non_interactive:
        if not choices:
            raise ValueError(
                "no harness to evaluate: none detected — install/configure a supported harness "
                "(Claude Code / GitHub Copilot CLI / OpenAI Codex CLI) or pass --harness <key>"
            )
        return _first_ready_or_first(choices)

    if not choices:
        raise ValueError("no harness to evaluate: pass --harness <key>")

    if len(choices) == 1:
        return choices[0].key

    import questionary

    q_choices = [
        questionary.Choice(title=f"{c.title}  ({c.status_note()})", value=c.key)
        for c in choices
    ]
    default_val = _first_ready_or_first(choices)
    try:
        answer = _questionary_select("Select the harness to evaluate:", q_choices, default_val).ask()
    except ImportError:
        if allow_fallback:
            return _first_ready_or_first(choices)
        raise
    if not answer:
        raise ValueError("harness selection cancelled — no harness chosen, aborting")
    return answer
