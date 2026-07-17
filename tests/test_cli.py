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
