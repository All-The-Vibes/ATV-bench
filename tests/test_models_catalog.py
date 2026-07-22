"""Unit 1 (quickstart): routable-model catalog.

quickstart lets the user arrow-select a model that routes through their harness auth. This
module offers a curated, honest per-harness list and surfaces the harness's CURRENTLY
configured model (read off the fingerprint manifest) as the highlighted default.
"""
from __future__ import annotations

import pytest

from atv_bench.models import ModelChoice, available_models_for, models_with_current


def test_each_runnable_harness_has_models():
    """claude-code / copilot-cli / codex each expose a non-empty curated model list."""
    for h in ("claude-code", "copilot-cli", "codex"):
        models = available_models_for(h)
        assert models, f"{h} has no catalog"
        assert all(isinstance(m, ModelChoice) for m in models)
        assert all(m.id and m.label for m in models)


def test_unknown_harness_is_empty():
    assert available_models_for("does-not-exist") == []


def test_alias_ids_are_harness_appropriate():
    """The curated ids look routable for their harness family (sanity, not exhaustive)."""
    claude_ids = {m.id for m in available_models_for("claude-code")}
    assert any("sonnet" in i or "opus" in i or "haiku" in i for i in claude_ids)
    codex_ids = {m.id for m in available_models_for("codex")}
    assert any(i.startswith("gpt-") or i.startswith("o") for i in codex_ids)


def test_current_model_surfaced_and_first(monkeypatch):
    """A configured model from the manifest is marked is_current and sorted first."""
    manifest = {"model": "claude-sonnet-4-6"}
    choices = models_with_current("claude-code", manifest)
    assert choices[0].is_current is True
    assert choices[0].id == "claude-sonnet-4-6"
    # exactly one current
    assert sum(1 for c in choices if c.is_current) == 1


def test_current_model_not_in_catalog_is_prepended():
    """A configured model we don't have in the curated list is still offered (routable),
    prepended and marked current — the upstream CLI is authoritative, not our list."""
    manifest = {"model": "claude-some-future-model"}
    choices = models_with_current("claude-code", manifest)
    assert choices[0].id == "claude-some-future-model"
    assert choices[0].is_current is True


def test_unknown_or_missing_current_marks_none(monkeypatch):
    """An 'unknown'/absent configured model marks nothing current (nothing to default to)."""
    for manifest in ({"model": "unknown"}, {}, {"model": ""}):
        choices = models_with_current("codex", manifest)
        assert all(c.is_current is False for c in choices)
        assert choices, "still offers the curated list"
