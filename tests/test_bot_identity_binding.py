"""Bot-identity binding (santa re-review #5) — ok result bound to immutable bot bytes.

PR #1 closed identity + match_id forgery, but the MatchSpec was (submitter, opponent,
match_id) only. Nothing tied a scored `ok` result to the SPECIFIC submitted bytes on
record (`bot_sha256` / PR head bytes), so a contributor could get one bot scored, then
change the bot/fingerprint under the same identity — the leaderboard row and the scored
match would no longer provably describe the same bytes.

THE FIX: the trusted match job computes the sha256 of the exact bot bytes it mounted and
issues it as part of the MatchSpec. On bind, the stored record is stamped with
`spec.bot_sha256` (from the trusted spec, never bot-asserted). If the bot ALSO reports a
`bot_sha256` that disagrees with the trusted one, that is a forgery signal and binds to a
CRASH forfeit — never trusts the bot's claim, never drops the match.

Distinct from item 1 (outcome adjudication): this binds the artifact to the bot IDENTITY.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from atv_bench.publish import (
    MatchSpec,
    SpecMismatch,
    bind_ok_to_spec,
    ingest_result,
)
from atv_bench.store import LeagueStore

_SHA = "b" * 64
_OTHER_SHA = "c" * 64

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "league.yml"


def _ok(pa="alice", pb="byok-anchor", outcome="a_wins", mid="run-1", **extra):
    return {"status": "ok", "player_a": pa, "player_b": pb, "outcome": outcome,
            "match_id": mid, "game": "battlesnake", **extra}


def _spec(submitter="alice", opponent="byok-anchor", match_id="run-1", bot_sha256=_SHA):
    return MatchSpec(submitter=submitter, opponent=opponent, match_id=match_id,
                     bot_sha256=bot_sha256)


def _sub(identity, bot_sha256=_SHA):
    return {
        "identity": identity, "game": "battlesnake",
        "bot_sha256": bot_sha256, "bot_filename": "main.py",
        "pr_url": "https://github.com/All-The-Vibes/ATV-bench/pull/1",
        "logs_url": "https://all-the-vibes.github.io/ATV-bench/logs/1",
        "fingerprint": {"harness": "claude-code", "model": "claude-opus-4-8", "gstack": True,
                        "skills": ["gstack"], "mcps": [], "plugins": [],
                        "custom_agents_count": 0, "unknown": [], "probe_version": "1.0.0"},
    }


# --- MatchSpec carries the trusted bot_sha256 ---

def test_matchspec_bot_sha256_is_optional_for_backcompat():
    """Existing callers build MatchSpec(submitter, opponent, match_id) with no sha; that
    must still work (bot_sha256 defaults to None = binding not enforced)."""
    spec = MatchSpec(submitter="alice", opponent="byok-anchor", match_id="run-1")
    assert spec.bot_sha256 is None


def test_matchspec_from_env_reads_bot_sha256(monkeypatch):
    monkeypatch.setenv("ATV_SUBMITTER", "alice")
    monkeypatch.setenv("ATV_OPPONENT", "byok-anchor")
    monkeypatch.setenv("ATV_MATCH_ID", "run-1")
    monkeypatch.setenv("ATV_BOT_SHA256", _SHA)
    spec = MatchSpec.from_env()
    assert spec.bot_sha256 == _SHA


def test_matchspec_from_env_bot_sha256_absent_is_none(monkeypatch):
    """The sha is an ENHANCEMENT: if the workflow doesn't export it, from_env still builds
    a spec (bot_sha256=None) rather than failing closed — identity+match_id binding (the
    load-bearing v1 guarantee) is unaffected."""
    monkeypatch.setenv("ATV_SUBMITTER", "alice")
    monkeypatch.setenv("ATV_OPPONENT", "byok-anchor")
    monkeypatch.setenv("ATV_MATCH_ID", "run-1")
    monkeypatch.delenv("ATV_BOT_SHA256", raising=False)
    assert MatchSpec.from_env().bot_sha256 is None


# --- bind stamps the trusted sha onto the record ---

def test_bind_stamps_trusted_bot_sha256_onto_record():
    """The stored record's bot_sha256 comes from the trusted spec, so a scored match is
    provably tied to the submitted bytes — never a bot-chosen string."""
    rec = bind_ok_to_spec(_ok(), _spec())
    assert rec["bot_sha256"] == _SHA


def test_bind_ignores_bot_reported_sha_uses_spec():
    """Even if the bot reports its own (matching or not) bot_sha256, the stored value is
    the trusted spec's — the bot never controls the identity field."""
    rec = bind_ok_to_spec(_ok(bot_sha256=_SHA), _spec())
    assert rec["bot_sha256"] == _SHA


def test_bind_rejects_bot_reported_sha_mismatch():
    """If the bot reports a bot_sha256 that disagrees with the trusted one, that is a
    forgery signal: reject to a forfeit rather than score a result for bytes on record
    that are not the ones that ran."""
    with pytest.raises(SpecMismatch):
        bind_ok_to_spec(_ok(bot_sha256=_OTHER_SHA), _spec())


def test_bind_without_spec_sha_does_not_stamp():
    """Back-compat: a spec with bot_sha256=None (local/hermetic) binds as before and does
    not add a bot_sha256 field."""
    rec = bind_ok_to_spec(_ok(), _spec(bot_sha256=None))
    assert "bot_sha256" not in rec


# --- end-to-end ingest ---

def test_ingest_ok_records_trusted_bot_sha256(tmp_path):
    store_dir = str(tmp_path / "league")
    store = LeagueStore(store_dir)
    store.add_submission(_sub("alice"))
    store.add_submission(_sub("byok-anchor"))
    art = tmp_path / "ok.json"
    art.write_text(json.dumps(_ok()))
    assert ingest_result(str(art), store_dir=store_dir, spec=_spec()) is True
    m = store.load_matches()[0]
    assert m["bot_sha256"] == _SHA


def test_ingest_sha_mismatch_scores_submitter_forfeit(tmp_path):
    """A bot reporting bytes different from the ones the trusted job mounted is rebound to
    a CRASH forfeit against the submitter — never dropped, never trusts the claim."""
    store_dir = str(tmp_path / "league")
    store = LeagueStore(store_dir)
    store.add_submission(_sub("alice"))
    store.add_submission(_sub("byok-anchor"))
    art = tmp_path / "forge.json"
    art.write_text(json.dumps(_ok(outcome="a_wins", bot_sha256=_OTHER_SHA)))
    assert ingest_result(str(art), store_dir=store_dir, spec=_spec()) is True
    m = store.load_matches()[0]
    assert m["forfeit_reason"] == "CRASH"
    assert {m["player_a"], m["player_b"]} == {"alice", "byok-anchor"}


def test_real_sha256_of_bytes_binds(tmp_path):
    """Integration-flavoured: the sha computed from real bot bytes (as the match job would
    compute it) is what the spec carries and what the record is stamped with."""
    bot_bytes = b"def move(state):\n    return 'up'\n"
    real_sha = hashlib.sha256(bot_bytes).hexdigest()
    store_dir = str(tmp_path / "league")
    store = LeagueStore(store_dir)
    store.add_submission(_sub("alice", bot_sha256=real_sha))
    store.add_submission(_sub("byok-anchor"))
    art = tmp_path / "ok.json"
    art.write_text(json.dumps(_ok()))
    ingest_result(str(art), store_dir=store_dir,
                  spec=_spec(bot_sha256=real_sha))
    m = store.load_matches()[0]
    assert m["bot_sha256"] == real_sha


# --- workflow wiring tripwire (runs on every push) ---

@pytest.fixture(scope="module")
def wf():
    assert WORKFLOW.exists(), "league.yml must exist"
    return yaml.safe_load(WORKFLOW.read_text())


def test_match_job_exports_trusted_bot_sha256_output(wf):
    """The match job must compute the sha of the bytes it mounted and hand it to the
    trusted publish side. Post santa #7 this travels in the match-meta artifact (the
    publish workflow_run event carries no PR context), authored from the extract step's
    trusted output."""
    match = wf["jobs"]["match"]
    steps = match["steps"]
    meta = next((s for s in steps if "match-meta.json" in str(s.get("run", ""))), None)
    assert meta is not None, "match job must write the trusted match-meta.json"
    assert "BOT_SHA256" in meta.get("env", {}), "match-meta must carry the bot_sha256"
    assert "steps.extract.outputs.bot_sha256" in str(meta["env"]["BOT_SHA256"]), \
        "bot_sha256 must come from the extract step that stages the bot bytes"


def test_extract_step_computes_sha_of_staged_bytes(wf):
    """The sha must be computed from the STAGED bot file (trusted bytes), not bot stdout."""
    steps = wf["jobs"]["match"]["steps"]
    extract = next(s for s in steps if s.get("id") == "extract")
    run = extract["run"]
    assert "sha256sum submission/main.py" in run, (
        "extract step must sha256 the staged submission/main.py (the exact mounted bytes)"
    )
    assert "bot_sha256=" in run and "GITHUB_OUTPUT" in run, (
        "extract step must write bot_sha256 to GITHUB_OUTPUT"
    )


def test_publish_ingest_receives_bot_sha256_from_meta():
    """The trusted publish workflow must export ATV_BOT_SHA256 sourced from the match-meta
    (loaded into steps.meta.outputs), so the scored record is bound to the submitted bytes."""
    publish_wf = Path(__file__).parent.parent / ".github" / "workflows" / "league-publish.yml"
    raw = publish_wf.read_text()
    assert "ATV_BOT_SHA256:" in raw, "publish workflow must export ATV_BOT_SHA256"
    assert "steps.meta.outputs.bot_sha256" in raw, (
        "ATV_BOT_SHA256 must be sourced from the trusted match-meta artifact"
    )
