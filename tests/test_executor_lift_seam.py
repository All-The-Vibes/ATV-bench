"""Follow-up 1 (PR #19): executor↔lift seam.

A finished live match produces a ``MatchRecord`` whose ``outcome['winner']`` is a harness
key, but nothing converted it into a ``matches.jsonl`` store row or a ``RatingMatch``, so
``compute_lift`` could only run on synthetic rows. These tests pin the end-to-end seam:
MatchRecord -> rating-row dict -> matches.jsonl -> RatingMatch -> compute_lift.
"""
from __future__ import annotations

import pytest

from atv_bench.lift import compute_lift
from atv_bench.match_record import BudgetVector, MatchRecord, PlayerRecord
from atv_bench.rating import matches_from_records
from atv_bench.runner import (
    append_rating_row,
    load_rating_rows,
    match_record_to_rating_row,
)


def _player(harness: str, model: str) -> PlayerRecord:
    return PlayerRecord(
        harness=harness, model=model, model_source="parsed", verified=True,
        tools=[], nested_skills=[], fingerprint_sha256="0" * 64,
        adapter_version="test@1", budget=BudgetVector(),
    )


def _record(winner: str, a: str, b: str, model: str = "sonnet") -> MatchRecord:
    return MatchRecord(
        game="lightcycles", game_version="1", prompt_version="edit@1",
        codeclash_version="test", rounds=1,
        outcome={"winner": winner, "round_winners": [winner], "round_stats": {}},
        replay_path="", players=[_player(a, model), _player(b, model)], verified=True,
    )


def test_winner_a_scores_one():
    """winner == players[0].harness => score_a == 1.0 (AC1.1)."""
    row = match_record_to_rating_row(_record("claude-code", "claude-code", "bare:claude-code"))
    assert row["harness_a"] == "claude-code"
    assert row["harness_b"] == "bare:claude-code"
    assert row["score_a"] == 1.0


def test_winner_b_scores_zero():
    """winner == players[1].harness => score_a == 0.0 (AC1.1)."""
    row = match_record_to_rating_row(_record("bare:claude-code", "claude-code", "bare:claude-code"))
    assert row["score_a"] == 0.0


def test_tie_scores_half():
    """A tie => score_a == 0.5 (AC1.1)."""
    row = match_record_to_rating_row(_record("tie", "claude-code", "bare:claude-code"))
    assert row["score_a"] == 0.5


def test_row_has_all_rating_keys():
    """The row carries every key matches_from_records needs (AC1.2)."""
    row = match_record_to_rating_row(_record("claude-code", "claude-code", "bare:claude-code"))
    for k in ("harness_a", "harness_b", "model_a", "model_b", "score_a"):
        assert k in row


def test_round_trip_record_to_lift(tmp_path):
    """End-to-end: records -> matches.jsonl -> RatingMatch -> compute_lift (AC1.3).

    The harness beats its bare baseline in most matches, so its lift over 'bare:M' is a
    finite, positive point estimate derived from the persisted rows — no synthetic thetas.
    """
    corpus = tmp_path / "rating_matches.jsonl"
    # 6 matches: harnessed 'claude-code' wins 5/6 vs its bare control on the same model.
    outcomes = ["claude-code"] * 5 + ["bare:sonnet"]
    for i, w in enumerate(outcomes):
        rec = _record(w, "claude-code", "bare:sonnet", model="sonnet")
        row = match_record_to_rating_row(rec)
        append_rating_row(corpus, row)

    records = load_rating_rows(corpus)
    rating_rows = matches_from_records(records)
    assert len(rating_rows) == 6

    lifts = compute_lift(rating_rows, baselines={"claude-code": "bare:sonnet"}, n_boot=200)
    assert "claude-code" in lifts
    res = lifts["claude-code"]
    # finite point estimate, and the harness that won 5/6 has positive lift.
    assert res.lift == res.lift  # not NaN
    assert res.lift > 0


def test_persist_record_appends_row(tmp_path):
    """persist_rating_row_from_record writes a loadable rating row (AC1.4).

    This is the testable seam the CLI `run --persist <path>` calls after a live match, kept
    Docker-free so the wiring is covered without a live tournament.
    """
    from atv_bench.runner import persist_rating_row_from_record

    corpus = tmp_path / "sub" / "rating_matches.jsonl"
    rec = _record("claude-code", "claude-code", "bare:sonnet", model="sonnet")
    persist_rating_row_from_record(rec, corpus)

    rows = load_rating_rows(corpus)
    assert len(rows) == 1
    assert rows[0]["harness_a"] == "claude-code"
    assert rows[0]["score_a"] == 1.0



def test_missing_winner_key_fails_closed():
    """A record with NO winner key is malformed — must raise, not score a silent draw."""
    from atv_bench.runner import match_record_to_rating_row

    rec = _record("claude-code", "claude-code", "bare:sonnet")
    rec.outcome.pop("winner", None)  # simulate a malformed/absent outcome
    with pytest.raises(ValueError, match="winner"):
        match_record_to_rating_row(rec)


def test_blank_winner_fails_closed():
    """A blank winner string is not a legitimate tie — must raise."""
    from atv_bench.runner import match_record_to_rating_row

    rec = _record("", "claude-code", "bare:sonnet")  # winner=""
    with pytest.raises(ValueError, match="winner"):
        match_record_to_rating_row(rec)


def test_explicit_draw_scores_half():
    """An explicit 'draw' token is a real outcome and still scores 0.5."""
    from atv_bench.runner import match_record_to_rating_row

    row = match_record_to_rating_row(_record("draw", "claude-code", "bare:sonnet"))
    assert row["score_a"] == 0.5


def test_identical_harness_selfplay_rejected():
    """Two players sharing a harness key make winner attribution ambiguous — reject it."""
    from atv_bench.runner import match_record_to_rating_row

    rec = _record("claude-code", "claude-code", "claude-code")  # both seats same harness
    with pytest.raises(ValueError, match="same harness|ambiguous|identical"):
        match_record_to_rating_row(rec)


def test_none_winner_not_matched_to_literal_none_harness():
    """A None winner must be rejected BEFORE stringification — otherwise a player whose
    harness key is the literal string 'None' would be mis-scored as the winner."""
    from atv_bench.runner import match_record_to_rating_row

    rec = _record("None", "None", "b")  # player_a.harness == 'None', winner set to 'None'
    rec.outcome["winner"] = None        # but the REAL winner is None (unscored)
    with pytest.raises(ValueError, match="None|winner"):
        match_record_to_rating_row(rec)
