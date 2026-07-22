"""Unit 2 (quickstart): interactive model selector.

An arrow-key picker over routable models, with a fully non-interactive path so quickstart is
scriptable in CI (an explicit choice, or a non-TTY stdin, bypasses the TUI).
"""
from __future__ import annotations

import pytest

from atv_bench.interactive import select_model
from atv_bench.models import ModelChoice

CHOICES = [
    ModelChoice("claude-sonnet-4-6", "Sonnet", is_current=True),
    ModelChoice("claude-opus-4-6", "Opus"),
]


def test_preselected_bypasses_prompt():
    """A preselected id (from --model) is validated against choices and returned, no TUI."""
    assert select_model(CHOICES, preselected="claude-opus-4-6") == "claude-opus-4-6"


def test_preselected_unknown_still_returned():
    """A --model not in the curated list is honored (upstream CLI is authoritative)."""
    assert select_model(CHOICES, preselected="gpt-5-future") == "gpt-5-future"


def test_empty_choices_requires_preselected():
    """With no catalog and no --model, we fail closed with an actionable error."""
    with pytest.raises(ValueError, match="model"):
        select_model([], preselected=None, non_interactive=True)


def test_non_interactive_defaults_to_current():
    """Non-interactive with no --model picks the is_current default (never blocks on a TUI)."""
    assert select_model(CHOICES, preselected=None, non_interactive=True) == "claude-sonnet-4-6"


def test_non_interactive_no_current_picks_first():
    """Non-interactive with no current model falls back to the first choice deterministically."""
    plain = [ModelChoice("a", "A"), ModelChoice("b", "B")]
    assert select_model(plain, preselected=None, non_interactive=True) == "a"


def test_interactive_uses_questionary(monkeypatch):
    """The interactive path delegates to questionary.select and returns its answer."""
    calls = {}

    class _FakeQ:
        def ask(self):
            return "claude-opus-4-6"

    def fake_select(message, choices, default=None):
        calls["message"] = message
        calls["choices"] = choices
        calls["default"] = default
        return _FakeQ()

    import atv_bench.interactive as I
    monkeypatch.setattr(I, "_questionary_select", fake_select)
    got = select_model(CHOICES, preselected=None, non_interactive=False)
    assert got == "claude-opus-4-6"
    # the current model is passed as the highlighted default
    assert calls["default"] is not None


def test_interactive_cancel_raises(monkeypatch):
    """A cancelled picker (questionary returns None) fails closed rather than running a phantom
    eval with no model."""
    class _FakeQ:
        def ask(self):
            return None

    import atv_bench.interactive as I
    monkeypatch.setattr(I, "_questionary_select", lambda *a, **k: _FakeQ())
    with pytest.raises(ValueError, match="cancel|model"):
        select_model(CHOICES, preselected=None, non_interactive=False)
