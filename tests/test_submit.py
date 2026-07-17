"""Tests for submit preflight + CLI (devex T3, T7, eng T4).

`submit` never reports a self-scored result; it opens a PR carrying bot + fingerprint.
Before doing anything it runs a 7-check preflight so failures are diagnosable up front
rather than mid-push. --dry-run runs preflight + shows the plan without opening a PR.
"""
from __future__ import annotations

import json

import pytest

from atv_bench.submit import (
    PREFLIGHT_CHECKS,
    PreflightCheck,
    build_submission,
    run_preflight,
)
from atv_bench.errors import AtvError


def test_seven_preflight_checks_defined():
    # gh installed / gh authed / repo exists / fork exists / branch clean /
    # leak-scan clean / bot-shape valid
    assert len(PREFLIGHT_CHECKS) == 7
    ids = {c.id for c in PREFLIGHT_CHECKS}
    assert ids == {
        "gh_installed", "gh_authed", "repo_exists", "fork_exists",
        "branch_clean", "leak_scan", "bot_shape",
    }


def test_preflight_runs_all_checks_and_reports():
    # a fake runner that passes everything
    def always_ok(check: PreflightCheck) -> tuple[bool, str]:
        return True, "ok"

    report = run_preflight(runner=always_ok)
    assert report["passed"] is True
    assert len(report["results"]) == 7
    assert all(r["ok"] for r in report["results"])


def test_preflight_surfaces_first_failure_with_actionable_error():
    def fail_auth(check: PreflightCheck) -> tuple[bool, str]:
        if check.id == "gh_authed":
            return False, "not logged in"
        return True, "ok"

    report = run_preflight(runner=fail_auth)
    assert report["passed"] is False
    failing = [r for r in report["results"] if not r["ok"]]
    assert len(failing) == 1
    assert failing[0]["id"] == "gh_authed"
    # the failing check carries an actionable fix + docs link
    assert failing[0]["fix"]
    assert failing[0]["docs_url"].startswith("https://")


def test_build_submission_shape():
    fingerprint = {
        "harness": "claude-code", "model": "claude-opus-4-8", "gstack": True,
        "skills": ["gstack"], "mcps": [], "plugins": [], "custom_agents_count": 0,
        "unknown": [], "probe_version": "1.0.0",
    }
    sub = build_submission(
        bot_path=_write_bot(),
        fingerprint=fingerprint,
        identity="octocat",
        game="battlesnake",
    )
    assert sub["identity"] == "octocat"
    assert sub["game"] == "battlesnake"
    assert len(sub["bot_sha256"]) == 64
    assert sub["fingerprint"] == fingerprint
    # store-compatible: carries pr_url + logs_url (the store requires them)
    assert sub["pr_url"].startswith("https://")
    assert sub["logs_url"].startswith("https://")
    # never carries a self-reported result
    assert "result" not in sub and "elo" not in sub and "win" not in sub


def test_build_submission_is_store_ingestable(tmp_path):
    """R2: build_submission output must be directly ingestable by LeagueStore
    (the two contracts previously disagreed on required keys)."""
    from atv_bench.store import LeagueStore
    fingerprint = {
        "harness": "claude-code", "model": "claude-opus-4-8", "gstack": True,
        "skills": ["gstack"], "mcps": [], "plugins": [], "custom_agents_count": 0,
        "unknown": [], "probe_version": "1.0.0",
    }
    sub = build_submission(bot_path=_write_bot(), fingerprint=fingerprint,
                           identity="octocat", game="battlesnake")
    store = LeagueStore(str(tmp_path / "league"))
    store.add_submission(sub)  # must not raise
    assert "octocat" in store.load_submissions()


def test_build_submission_rejects_leaky_fingerprint():
    # a fingerprint that somehow still contains a secret-shaped value is refused
    bad = {
        "harness": "claude-code", "model": "claude-opus-4-8", "gstack": True,
        "skills": ["ghp_1234567890abcdefghijklmnopqrstuvwxyzAB"], "mcps": [],
        "plugins": [], "custom_agents_count": 0, "unknown": [], "probe_version": "1.0.0",
    }
    with pytest.raises(AtvError):
        build_submission(bot_path=_write_bot(), fingerprint=bad,
                         identity="octocat", game="battlesnake")


# --- UC1 provenance binding integration ---

_FP = {
    "harness": "claude-code", "model": "claude-opus-4-8", "gstack": True,
    "skills": ["gstack"], "mcps": [], "plugins": [], "custom_agents_count": 0,
    "unknown": [], "probe_version": "1.0.0",
}


def test_build_submission_embeds_provenance_bound_to_bot_and_fingerprint():
    from atv_bench.fingerprint.provenance import verify_provenance
    bot = _write_bot()
    sub = build_submission(bot_path=bot, fingerprint=_FP, identity="octocat",
                           game="battlesnake", captured_at="2026-07-17T00:00:00Z")
    prov = sub["provenance"]
    assert prov["harness"] == "claude-code"
    assert prov["bot_sha256"] == sub["bot_sha256"]
    res = verify_provenance(provenance=prov, harness="claude-code",
                            bot_sha256=sub["bot_sha256"], fingerprint=sub["fingerprint"])
    assert res.ok is True


def test_verify_submission_provenance_detects_fingerprint_post_edit():
    from atv_bench.submit import verify_submission_provenance
    sub = build_submission(bot_path=_write_bot(), fingerprint=_FP, identity="octocat",
                           game="battlesnake", captured_at="2026-07-17T00:00:00Z")
    sub["fingerprint"] = {**_FP, "skills": []}  # leaner manifest than captured
    res = verify_submission_provenance(sub)
    assert res.ok is False
    assert "fingerprint" in " ".join(res.reasons).lower()


def test_verify_submission_provenance_detects_bot_swap():
    from atv_bench.submit import verify_submission_provenance
    sub = build_submission(bot_path=_write_bot(), fingerprint=_FP, identity="octocat",
                           game="battlesnake", captured_at="2026-07-17T00:00:00Z")
    sub["bot_sha256"] = "f" * 64
    res = verify_submission_provenance(sub)
    assert res.ok is False
    assert "bot" in " ".join(res.reasons).lower()


def test_verify_submission_provenance_accepts_untampered_record():
    from atv_bench.submit import verify_submission_provenance
    sub = build_submission(bot_path=_write_bot(), fingerprint=_FP, identity="octocat",
                           game="battlesnake", captured_at="2026-07-17T00:00:00Z")
    res = verify_submission_provenance(sub)
    assert res.ok is True


def test_keyed_build_marks_provenance_signed(monkeypatch):
    monkeypatch.setenv("ATV_PROVENANCE_KEY", "server-side-key")
    sub = build_submission(bot_path=_write_bot(), fingerprint=_FP, identity="octocat",
                           game="battlesnake", captured_at="2026-07-17T00:00:00Z")
    assert sub["provenance"]["signed"] is True


def test_verify_submission_provenance_rehashes_actual_bot_bytes(tmp_path):
    """F-bot-bytes: when given the real artifact, verification must re-hash it and reject a
    swapped bot even if record['bot_sha256'] was left untouched."""
    from atv_bench.submit import verify_submission_provenance
    bot = _write_bot()
    sub = build_submission(bot_path=bot, fingerprint=_FP, identity="octocat",
                           game="battlesnake", captured_at="2026-07-17T00:00:00Z")
    # attacker swaps the shipped bot bytes but leaves the record's hash + token untouched
    swapped = tmp_path / "main.py"
    swapped.write_text("def move(state):\n    return 'down'  # different bot\n")
    res = verify_submission_provenance(sub, bot_path=str(swapped))
    assert res.ok is False
    assert "bot" in " ".join(res.reasons).lower()


def test_verify_submission_provenance_accepts_matching_bot_bytes():
    from atv_bench.submit import verify_submission_provenance
    bot = _write_bot()
    sub = build_submission(bot_path=bot, fingerprint=_FP, identity="octocat",
                           game="battlesnake", captured_at="2026-07-17T00:00:00Z")
    res = verify_submission_provenance(sub, bot_path=bot)  # same bytes it was built from
    assert res.ok is True


# --- helpers ---

_BOT_SRC = "def move(state):\n    return 'up'\n"


def _write_bot(tmp=[None]):
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    p = d / "main.py"
    p.write_text(_BOT_SRC)
    return str(p)


def test_status_trail_is_honest_about_wiring():
    """R4 (Reviewer B): status trail must be honest about how the PR is opened and that
    the board updates via the label-triggered match + publish workflow (not 'on merge')."""
    from atv_bench.submit import submission_status_trail
    trail = " ".join(submission_status_trail(is_first_time=True)).lower()
    assert "run-match" in trail or "label" in trail  # label-triggered, stated
    # honest about how the PR is opened: either the live gh path or the manual fallback
    assert "--live" in trail or "manually" in trail or "you open it" in trail


def test_submit_cli_dry_run_shows_provenance_line(tmp_path):
    """The dry-run submit surface must report the provenance trust level so a contributor
    knows the row is self-attested (unkeyed) until a trusted sandbox re-signs it."""
    from typer.testing import CliRunner
    from atv_bench.cli import app
    home = tmp_path / ".codex"
    (home / "skills" / "gstack").mkdir(parents=True)
    (home / "config.toml").write_text('model = "gpt-5.5"\n')
    bot = tmp_path / "main.py"
    bot.write_text(_BOT_SRC)
    result = CliRunner().invoke(app, [
        "submit", str(bot), "--game", "lightcycles", "--dry-run",
        "--harness", "codex", "--home", str(home),
        "--identity", "octocat", "--out", str(tmp_path / "submission.json"),
    ])
    assert result.exit_code == 0, result.output
    assert "Provenance:" in result.output
    assert "self-attested" in result.output  # no key set in this env


@pytest.mark.parametrize("bad_fp", ["not-a-dict", 42, ["a"], None])
def test_verify_submission_provenance_malformed_fingerprint_fails_closed(bad_fp):
    """Santa PR#10 (reviewer B): a malformed (non-dict) fingerprint in the record must
    FAIL CLOSED with ok=False, not crash the verifier with an AttributeError — an
    untrusted merged record must never be able to DoS the merge-time provenance gate."""
    from atv_bench.submit import verify_submission_provenance
    rec = {"identity": "octocat", "game": "battlesnake", "bot_sha256": "a" * 64,
           "fingerprint": bad_fp, "provenance": {"version": "1.0.0"}}
    res = verify_submission_provenance(rec)  # must not raise
    assert res.ok is False
