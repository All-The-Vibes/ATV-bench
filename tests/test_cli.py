"""CLI tests (devex T2, eng T4): fingerprint --dry-run consent view + entrypoints.

The consent view has three load-bearing sections: Will publish / Scrubbed / Unknown.
The Scrubbed section must appear even when nothing was scrubbed-count-0, so the
developer sees the scanner ran. --json emits machine output.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from atv_bench.cli import app

runner = CliRunner()


def _fixture_home(tmp_path: Path) -> Path:
    home = tmp_path / ".claude"
    (home / "skills" / "gstack").mkdir(parents=True)
    (home / "skills" / "office-hours").mkdir()
    # a secret-like skill name so the Scrubbed section has something to report
    (home / "skills" / "my-api-token-xyz").mkdir()
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    return home


def test_fingerprint_dry_run_three_sections(tmp_path):
    home = _fixture_home(tmp_path)
    result = runner.invoke(app, ["fingerprint", "--dry-run", "--home", str(home)])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Will publish:" in out
    assert "Scrubbed:" in out       # load-bearing: proves the scanner fired
    assert "Unknown:" in out
    # the secret-like name never appears in the human output
    assert "my-api-token-xyz" not in out
    # clean names do appear
    assert "gstack" in out


def test_fingerprint_json_output(tmp_path):
    home = _fixture_home(tmp_path)
    result = runner.invoke(app, ["fingerprint", "--json", "--home", str(home)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["harness"] == "claude-code"
    assert "gstack" in payload["skills"]
    assert "my-api-token-xyz" not in json.dumps(payload)
    # the scrubbed name is accounted for in unknown[]
    assert any(u["reason"] == "name_failed_safety_scan" for u in payload["unknown"])


def test_cli_has_expected_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("fingerprint", "submit"):
        assert cmd in result.output
