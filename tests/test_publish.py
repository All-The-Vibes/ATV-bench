"""Tests for the publish-side entrypoint (trusted job)."""
from __future__ import annotations

import json

import pytest

from atv_bench.publish import build_site, validate_artifact


def test_validate_artifact_accepts_wellformed(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"status": "ok"}))
    assert validate_artifact(str(p))["status"] == "ok"


def test_validate_artifact_rejects_malformed(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"nope": 1}))
    with pytest.raises(ValueError):
        validate_artifact(str(p))


def test_build_site_emits_valid_leaderboard(tmp_path):
    out = build_site(str(tmp_path / "site"))
    doc = json.loads((out / "leaderboard.json").read_text())
    assert doc["schema_version"] == 1
    assert doc["rows"] == []
