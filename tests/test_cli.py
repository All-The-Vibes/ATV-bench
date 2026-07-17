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


# --- T5: detect-guard (M10) + model in consent (M13) + codex missing-config msg (M9) ---

def test_fingerprint_consent_includes_model(tmp_path):
    """M13: the --dry-run consent view must show the model that would be published."""
    home = _fixture_home(tmp_path)
    result = runner.invoke(app, ["fingerprint", "--dry-run", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert "claude-opus-4-8" in result.output
    # and it's labelled, not just floating
    assert "model" in result.output.lower()


def test_detect_guard_requires_explicit_harness_when_multiple(tmp_path, monkeypatch):
    """M10: >1 live harness config present + no --harness → error listing detected
    harnesses and requiring an explicit --harness (no silent first-live pick)."""
    base = tmp_path / "home"
    (base / ".claude" / "skills" / "gstack").mkdir(parents=True)
    (base / ".claude" / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    (base / ".codex" / "skills" / "gstack").mkdir(parents=True)
    (base / ".codex" / "config.toml").write_text('model = "gpt-5.5"\n')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: base))
    result = runner.invoke(app, ["fingerprint"])  # no --harness, no --home
    assert result.exit_code != 0, result.output
    out = result.output.lower()
    assert "--harness" in out
    assert "claude-code" in out and "codex" in out


def test_detect_guard_single_harness_ok(tmp_path, monkeypatch):
    """Only one live config present → auto-detect proceeds without the guard."""
    base = tmp_path / "home"
    (base / ".codex" / "skills" / "gstack").mkdir(parents=True)
    (base / ".codex" / "config.toml").write_text('model = "gpt-5.5"\n')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: base))
    result = runner.invoke(app, ["fingerprint"])
    assert result.exit_code == 0, result.output
    assert "gpt-5.5" in result.output


def test_codex_missing_config_actionable_message(tmp_path):
    """M9: an empty ~/.codex (no config.toml) probed explicitly → actionable message,
    never a green empty manifest passing silently."""
    home = tmp_path / ".codex"
    home.mkdir()
    result = runner.invoke(app, ["fingerprint", "--harness", "codex", "--home", str(home)])
    # empty codex home should not present as a confident published fingerprint
    out = result.output.lower()
    assert "config.toml" in out
    # actionable: names the fix / where to look
    assert result.exit_code != 0 or "no " in out or "not found" in out


# --- T7: validate-harness failure copy names harness + prose + fix (M11) ---

def test_validate_harness_success_names_harness(tmp_path):
    home = tmp_path / ".claude"
    (home / "skills" / "gstack").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    result = runner.invoke(app, ["validate-harness", "--harness", "claude-code", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert "claude-code" in result.output.lower()


def test_validate_harness_failure_copy_is_actionable(tmp_path, monkeypatch):
    """M11: when validate-harness fails, the output names WHICH harness was validated
    and gives a fix hint — not just bare reason codes a contributor can't act on."""
    import atv_bench.validate as v
    home = tmp_path / ".claude"
    (home / "skills" / "gstack").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    monkeypatch.setattr(
        v, "validate_harness_fingerprint",
        lambda m: {"ok": False, "errors": ["leak risk: skills entry failed safety scan"]},
    )
    result = runner.invoke(app, ["validate-harness", "--harness", "claude-code", "--home", str(home)])
    assert result.exit_code == 1
    out = result.output.lower()
    assert "claude-code" in out            # names the harness validated
    assert "leak risk" in out              # the prose reason
    assert "contributing" in out or "fix" in out  # a fix hint / where to look


def test_submit_dry_run_emits_submission_json(tmp_path):
    """R3 (both reviewers): CONTRIBUTING promises submit --dry-run emits the submission
    JSON, but it only printed preflight text. --dry-run must write a store-ingestable
    record so the manual-PR fallback is real."""
    import json as _json
    home = tmp_path / ".claude"
    (home / "skills" / "gstack").mkdir(parents=True)
    (home / "settings.json").write_text(_json.dumps({"model": "claude-opus-4-8"}))
    bot = tmp_path / "main.py"
    bot.write_text("def move(s):\n    return 'up'\n")
    out_json = tmp_path / "submission.json"
    result = runner.invoke(app, [
        "submit", str(bot), "--game", "lightcycles", "--dry-run",
        "--home", str(home), "--identity", "octocat", "--out", str(out_json),
    ])
    assert result.exit_code == 0, result.output
    assert out_json.exists(), "dry-run must write the submission JSON"
    rec = _json.loads(out_json.read_text())
    # store-ingestable shape
    from atv_bench.store import LeagueStore
    store = LeagueStore(str(tmp_path / "league"))
    store.add_submission(rec)  # must not raise
    assert rec["identity"] == "octocat"
    assert rec["bot_filename"] == "main.py"


def test_validate_pr_paths_accepts_own_files(tmp_path):
    pf = tmp_path / "changed.txt"
    pf.write_text("league/submissions/octocat/main.py\n"
                  "league/submissions/octocat/submission.json\n")
    result = runner.invoke(app, ["validate-pr-paths", "--author", "octocat",
                                 "--paths-file", str(pf)])
    assert result.exit_code == 0


def test_validate_pr_paths_rejects_matches_edit(tmp_path):
    pf = tmp_path / "changed.txt"
    pf.write_text("league/matches.jsonl\n")
    result = runner.invoke(app, ["validate-pr-paths", "--author", "octocat",
                                 "--paths-file", str(pf)])
    assert result.exit_code == 1
    assert "outside" in result.stdout


def test_validate_pr_paths_name_status_rejects_workflow_edit(tmp_path):
    pf = tmp_path / "changes.txt"
    pf.write_text("M\tleague/submissions/octocat/main.py\n"
                  "M\t.github/workflows/league.yml\n")
    result = runner.invoke(app, ["validate-pr-paths", "--author", "octocat",
                                 "--name-status", "--paths-file", str(pf)])
    assert result.exit_code == 1


def test_validate_pr_paths_name_status_allows_plumbing_pr(tmp_path):
    pf = tmp_path / "changes.txt"
    pf.write_text("M\tsrc/atv_bench/store.py\n")
    result = runner.invoke(app, ["validate-pr-paths", "--author", "maintainer",
                                 "--name-status", "--paths-file", str(pf)])
    assert result.exit_code == 0
