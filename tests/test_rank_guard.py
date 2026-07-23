"""Typed-rank guard + honesty/framing (plan Section 6).

The product's integrity depends on never publishing a ranked number it cannot support.
These tests PIN a single choke point: rankings / ratings / lifts may reach a user ONLY
through one typed renderer (``src/atv_bench/render.py::render_ranking``) that checks the
``verified`` flag CENTRALLY. When the corpus is unverified the renderer REFUSES to emit a
rank number and instead returns an "unranked (integrity gate)" object carrying the reframe
copy — the verified=false state is an integrity FEATURE, not a broken board.

Honesty guards (from the review):
  * "airtight" over-claims the fingerprint — it must not appear in shipped copy.
  * the unknown[] ledger (unparseable models / unsupported surfaces) must be SURFACED,
    not hidden.
  * codex is fingerprint-only (no builder adapter yet) and must NOT be framed as a
    competitor until a CodexAdapter builder exists (CEO-5).

RED until src/atv_bench/render.py lands and the framing copy is corrected.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent

# The API under test (RED until src/atv_bench/render.py exists).
from atv_bench.render import (  # noqa: E402
    RankingView,
    UnrankedView,
    harness_role,
    render_ranking,
)


# ---------------------------------------------------------------------------
# Fixtures: a ratings doc + lift doc payload shaped exactly like the real
# build_ratings_doc / lift CLI outputs (model-locked roster -> bundle_unit).
# ---------------------------------------------------------------------------


def _ratings_doc(*, verified: bool) -> dict:
    return {
        "harnesses": [
            {"harness": "claude-code", "model": "claude-opus-4.8", "theta": 0.42,
             "bundle_unit": True, "theta_model_adjusted": None, "publishable": True},
            {"harness": "copilot-cli", "model": "claude-opus-4.8", "theta": -0.42,
             "bundle_unit": True, "theta_model_adjusted": None, "publishable": True},
        ],
        "pairwise": [
            {"harness": "copilot-cli", "ref": "claude-code", "diff": -0.84,
             "se": 0.2, "ci": {"lo": -1.2, "hi": -0.44}, "fdr_p": 0.01},
        ],
        "attributed": False,
        "model_locked": True,
        "factor_out": False,
        "verified": verified,
        "unknown": ["auto", "unknown-model-x"],
        "data_sufficiency": {"n_matches": 40, "n_harnesses": 2},
    }


def _lift_doc() -> dict:
    return {
        "seed": 0,
        "n_boot": 1000,
        "lifts": [
            {"harness": "claude-code", "bare_harness": "claude-bare",
             "base_model": "claude-opus-4.8", "lift": 1.23, "ci": {"lo": 0.90, "hi": 1.55}},
            {"harness": "copilot-cli", "bare_harness": "copilot-bare",
             "base_model": "claude-opus-4.8", "lift": 0.31, "ci": {"lo": 0.05, "hi": 0.58}},
        ],
    }


def _payload(*, verified: bool) -> dict:
    return {"ratings": _ratings_doc(verified=verified), "lifts": _lift_doc()}


# Adversarial rank-shaped phrasings the renderer must NEVER emit when unverified.
# Non-regex, human-authored variants — the guard must catch the meaning, not one regex.
_RANK_PHRASES = [
    "#1", "#2", "rank 1", "ranked #", "edged out", "leads", "beats", "wins over",
    "top harness", "1st", "2nd", "🥇", "🥈", "🥉", "🏆",
]


def _assert_no_rank_language(text: str) -> None:
    low = text.lower()
    for phrase in _RANK_PHRASES:
        assert phrase.lower() not in low, f"rank-shaped phrase leaked when unverified: {phrase!r}"
    # No bare "#<int>" rank token either.
    assert not re.search(r"#\s*\d+", text), f"numeric rank token leaked: {text!r}"


# ---------------------------------------------------------------------------
# 1. Rank output ONLY through the typed renderer; it refuses when unverified.
# ---------------------------------------------------------------------------


def test_rank_only_through_typed_renderer():
    """Unverified -> render_ranking returns an UnrankedView, never a rank number."""
    view = render_ranking(_payload(verified=False), verified=False)
    assert isinstance(view, UnrankedView)
    assert view.is_ranked is False
    # The typed object stringifies to the integrity-gate marker, never a rank.
    rendered = str(view)
    assert "unranked (integrity gate)" in rendered.lower()
    _assert_no_rank_language(rendered)


def test_renderer_blocks_when_unverified():
    """Unverified corpus -> the reframe line, not a rank."""
    view = render_ranking(_payload(verified=False), verified=False)
    rendered = str(view).lower()
    assert "not a ranked number yet" in rendered
    assert "gated for integrity" in rendered
    _assert_no_rank_language(str(view))


# ---------------------------------------------------------------------------
# 2. Verified -> LIFT is the headline; bundle theta secondary.
# ---------------------------------------------------------------------------


def test_lift_is_headline():
    """Verified board leads with LIFT (harness lift over bare model)."""
    view = render_ranking(_payload(verified=True), verified=True)
    assert isinstance(view, RankingView)
    assert view.is_ranked is True
    assert view.headline_metric == "lift"
    rendered = str(view).lower()
    # Lift present and labelled as the headline metric.
    assert "lift" in rendered
    assert "harness lift over bare model" in rendered
    # The concrete headline lift value is surfaced.
    assert "1.23" in str(view)
    # Bundle theta is present but SECONDARY (appears after the lift headline).
    assert "theta" in rendered
    assert rendered.index("lift") < rendered.index("theta"), "lift must lead theta"


def test_unknown_ledger_exposed():
    """The unknown[] ledger (non-publishable models) is surfaced, not hidden."""
    view = render_ranking(_payload(verified=True), verified=True)
    rendered = str(view)
    assert "unknown-model-x" in rendered
    assert "unknown" in rendered.lower()


def test_verified_false_reframed():
    """The first verified=false a user sees carries the DX-2 one-liner."""
    view = render_ranking(_payload(verified=False), verified=False)
    assert "No — numbers are gated because" in str(view)


# ---------------------------------------------------------------------------
# 3. "airtight" must not appear in shipped copy (over-claim).
# ---------------------------------------------------------------------------


def test_airtight_word_absent():
    """`grep -ri airtight docs/ src/ README.md` must return nothing."""
    targets = [REPO / "docs", REPO / "src", REPO / "README.md"]
    hits: list[str] = []
    for t in targets:
        if not t.exists():
            continue
        files = t.rglob("*") if t.is_dir() else [t]
        for f in files:
            if not f.is_file() or "__pycache__" in f.parts:
                continue
            try:
                text = f.read_text(errors="ignore")
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if "airtight" in line.lower():
                    hits.append(f"{f}:{i}: {line.strip()}")
    assert not hits, "over-claiming 'airtight' still present:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# 4. codex is now a BUILDER: CodexCliAdapter ships an execution adapter (quickstart).
# ---------------------------------------------------------------------------


def test_codex_is_now_a_builder():
    """codex gained an execution adapter (CodexCliAdapter), so it is a competitor, not
    fingerprint-only — harness_role reflects the live ADAPTERS registry."""
    assert harness_role("codex") == "builder"
    assert harness_role("claude-code") == "builder"
    assert harness_role("copilot-cli") == "builder"
    # a genuinely-unregistered harness stays fingerprint-only.
    assert harness_role("some-unregistered-harness") == "fingerprint-only"
    # the role helper agrees with reality: the codex adapter is registered.
    from atv_bench.adapters.contract import ADAPTERS
    assert "codex" in ADAPTERS
