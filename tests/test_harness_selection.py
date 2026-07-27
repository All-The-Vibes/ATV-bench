"""Phase 1 — keyboard harness dropdown for `atv-bench quickstart`.

TDD contract for `atv_bench.harness_selection.select_harness`, the arrow-key harness
picker (Copilot CLI / Codex / Claude Code). Mirrors the model-picker contract in
`test_interactive_select.py`: explicit/preselected and non-interactive paths NEVER block on a
TUI; a cancelled picker or an empty catalog with no explicit choice fails closed.
"""
from __future__ import annotations

import pytest

from atv_bench.harness_selection import HarnessChoice, select_harness


def _choices() -> list[HarnessChoice]:
    return [
        HarnessChoice(key="claude-code", title="Claude Code", configured=True, cli_found=True),
        HarnessChoice(key="copilot-cli", title="GitHub Copilot CLI", configured=False, cli_found=True),
        HarnessChoice(key="codex", title="OpenAI Codex CLI", configured=True, cli_found=False),
    ]


def test_preselected_bypasses_picker(monkeypatch):
    """An explicit --harness is honored verbatim and never opens the TUI."""
    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("questionary must not be called when preselected")
    monkeypatch.setattr("atv_bench.harness_selection._questionary_select", _boom)
    assert select_harness(_choices(), preselected="codex") == "codex"


def test_non_interactive_picks_first_configured():
    """No TTY → first configured+cli-found harness, no prompt."""
    got = select_harness(_choices(), non_interactive=True)
    assert got == "claude-code"


def test_non_interactive_falls_back_to_first_when_none_ready():
    choices = [
        HarnessChoice(key="copilot-cli", title="Copilot", configured=False, cli_found=False),
        HarnessChoice(key="codex", title="Codex", configured=False, cli_found=False),
    ]
    assert select_harness(choices, non_interactive=True) == "copilot-cli"


def test_single_choice_auto_selected(monkeypatch):
    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("single choice must not prompt")
    monkeypatch.setattr("atv_bench.harness_selection._questionary_select", _boom)
    one = [HarnessChoice(key="codex", title="Codex", configured=True, cli_found=True)]
    assert select_harness(one, non_interactive=False) == "codex"


def test_empty_choices_raises():
    with pytest.raises(ValueError):
        select_harness([], non_interactive=True)


def test_interactive_shows_picker_and_returns_choice(monkeypatch):
    """Interactive path calls questionary.select with all 3 harnesses, status-annotated."""
    captured = {}

    class _Q:
        def ask(self):
            return "copilot-cli"

    def _fake_select(message, choices, default=None):
        captured["message"] = message
        captured["choices"] = choices
        captured["default"] = default
        return _Q()

    monkeypatch.setattr("atv_bench.harness_selection._questionary_select", _fake_select)
    got = select_harness(_choices(), non_interactive=False)
    assert got == "copilot-cli"
    # all three harnesses offered
    assert len(captured["choices"]) == 3
    titles = " ".join(getattr(c, "title", str(c)) for c in captured["choices"])
    assert "Claude Code" in titles and "Copilot" in titles and "Codex" in titles
    # status annotations surfaced to the user
    assert "config missing" in titles or "not on PATH" in titles


def test_cancel_raises(monkeypatch):
    class _Q:
        def ask(self):
            return None

    monkeypatch.setattr("atv_bench.harness_selection._questionary_select",
                        lambda *a, **k: _Q())
    with pytest.raises(ValueError, match="cancel"):
        select_harness(_choices(), non_interactive=False)


def test_questionary_import_failure_falls_back(monkeypatch):
    """If questionary import fails on the interactive path, degrade to non-interactive."""
    def _raise(*a, **k):
        raise ImportError("no questionary")
    monkeypatch.setattr("atv_bench.harness_selection._questionary_select", _raise)
    # non_interactive left None → interactive attempted → import fails → first-ready fallback
    got = select_harness(_choices(), non_interactive=False, allow_fallback=True)
    assert got == "claude-code"


def test_choices_from_registry():
    """`harness_choices()` builds annotated choices from the harness registry + detectors."""
    from atv_bench.harness_selection import harness_choices

    choices = harness_choices(
        config_present=lambda k: k == "claude-code",
        cli_present=lambda k: k in ("claude-code", "copilot-cli"),
    )
    by_key = {c.key: c for c in choices}
    assert set(by_key) == {"claude-code", "copilot-cli", "codex"}
    assert by_key["claude-code"].configured and by_key["claude-code"].cli_found
    assert not by_key["copilot-cli"].configured and by_key["copilot-cli"].cli_found
    assert not by_key["codex"].cli_found
