"""Config->outcome recommender for the rating engine (plan Section 5).

The recommender turns the rated corpus into an actionable answer: given a task/profile P,
which harness-component config is expected to win? It must be CALIBRATED (beat a marginal
baseline on held-out data by Brier & log-loss, with CIs that cover the empirical rates) and
must recover a planted dominant config from synthetic data.

RED before src/atv_bench/recommender.py exists.
"""
from __future__ import annotations

import numpy as np
import pytest

from atv_bench.recommender import Recommender


def _synth_corpus(*, n, seed, config_strength, profile_of):
    """Generate labelled (config, profile, outcome) rows under a logistic model whose only
    signal is config_strength[config] modulated by profile. Returns a list of dict rows."""
    rng = np.random.default_rng(seed)
    configs = list(config_strength)
    rows = []
    for _ in range(n):
        ca, cb = rng.choice(len(configs), size=2, replace=False)
        ca, cb = configs[ca], configs[cb]
        prof = profile_of(rng)
        logit = config_strength[ca][prof] - config_strength[cb][prof]
        p_a = 1.0 / (1.0 + np.exp(-logit))
        rows.append({
            "config_a": ca, "config_b": cb, "profile": prof,
            "score_a": 1.0 if rng.random() < p_a else 0.0,
        })
    return rows


def test_config_to_outcome_recommendation():
    """Shape contract: given a profile the recommender returns a ranked list of
    (config, predicted_win_prob, ci) entries, probs in [0,1], sorted descending."""
    strength = {
        "X": {"P": 1.0}, "Y": {"P": 0.0}, "Z": {"P": -0.5},
    }
    rows = _synth_corpus(n=1500, seed=1, config_strength=strength,
                         profile_of=lambda r: "P")
    rec = Recommender().fit(rows)
    ranked = rec.recommend(profile="P")
    assert isinstance(ranked, list) and ranked
    probs = [e["win_prob"] for e in ranked]
    assert all(0.0 <= p <= 1.0 for p in probs)
    assert probs == sorted(probs, reverse=True)
    for e in ranked:
        assert e["ci"]["lo"] <= e["win_prob"] <= e["ci"]["hi"]


def test_recommender_holdout_calibration():
    """Train/test split: on HELD-OUT matches the recommender's Brier score and log-loss must
    beat the marginal baseline (always predict the corpus base win-rate), and its CIs must
    cover the empirical held-out win rates.

    Theory: a model that has learned real config signal has lower expected Brier/log-loss than
    the marginal predictor whenever the true probabilities differ from the base rate (strict
    proper-scoring-rule improvement). We assert strict improvement on the held-out fold and
    require nominal CI coverage of the binned empirical rates."""
    strength = {
        "X": {"P": 1.2}, "Y": {"P": 0.3}, "Z": {"P": -0.9}, "W": {"P": -0.4},
    }
    rows = _synth_corpus(n=4000, seed=2, config_strength=strength,
                         profile_of=lambda r: "P")
    split = 3000
    train, test = rows[:split], rows[split:]
    rec = Recommender().fit(train)
    metrics = rec.evaluate(test)
    assert metrics["brier"] < metrics["baseline_brier"], "must beat marginal Brier"
    assert metrics["log_loss"] < metrics["baseline_log_loss"], "must beat marginal log-loss"
    assert metrics["ci_coverage"] >= 0.9, (
        f"held-out CI coverage {metrics['ci_coverage']:.3f} below nominal 0.9")


def test_recommender_finds_planted_dominant_config():
    """Plant config X as strictly dominant for profile P; the recommender must return X first,
    with a CI that EXCLUDES the runner-up's win prob (a real, significant preference — not a
    coin flip). Effect size is large (>=1.5 logits over the field) so at N=4000 the top-vs-
    runner-up gap is many SE wide and separation has power ~1."""
    strength = {
        "X": {"P": 2.0},   # dominant
        "Y": {"P": 0.2},
        "Z": {"P": -0.3},
        "W": {"P": -0.8},
    }
    rows = _synth_corpus(n=4000, seed=3, config_strength=strength,
                         profile_of=lambda r: "P")
    rec = Recommender().fit(rows)
    ranked = rec.recommend(profile="P")
    assert ranked[0]["config"] == "X", f"planted dominant config not first: {ranked[0]}"
    runner_up_prob = ranked[1]["win_prob"]
    assert ranked[0]["ci"]["lo"] > runner_up_prob, (
        f"top config CI lo {ranked[0]['ci']['lo']:.3f} does not exclude runner-up "
        f"{runner_up_prob:.3f}")
