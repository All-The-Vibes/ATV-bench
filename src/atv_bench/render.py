"""The SINGLE typed choke point for emitting rankings / ratings / lifts (plan Section 6).

Every rank, rating, or lift number that reaches a user MUST pass through
:func:`render_ranking`. It checks the ``verified`` flag CENTRALLY:

  * ``verified=False`` -> :class:`UnrankedView`. NEVER a rank. Carries the integrity-gate
    reframe copy — an unverified corpus is an integrity FEATURE, not a broken board.
  * ``verified=True``  -> :class:`RankingView`. LIFT (harness lift over the bare model) is
    the headline metric; bundle theta is secondary; the ``unknown[]`` ledger is surfaced.

Because there is exactly one renderer, a free-text rank leak becomes structurally
impossible: any surface that wants to show a number routes through here.

``harness_role`` reports whether a harness key is a real builder (a builder adapter exists)
or ``fingerprint-only`` (probed but no adapter yet, e.g. codex).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "RankingView",
    "UnrankedView",
    "render_ranking",
    "harness_role",
]


# The DX-2 one-liner: the first verified=false a user sees frames the gate as a feature.
_REFRAME_HEADLINE = "unranked (integrity gate)"
_REFRAME_ONE_LINER = (
    "No — numbers are gated because we won't publish what we can't verify."
)
_REFRAME_BODY = (
    "This corpus is not a ranked number yet: it is gated for integrity until every "
    "surface verifies. The gate is the product working, not a broken board."
)


def _unknown_list(ratings: Mapping[str, Any]) -> list[str]:
    """Normalise the unknown[] ledger to a list of human strings."""
    raw = ratings.get("unknown") or []
    out: list[str] = []
    for item in raw:
        if isinstance(item, Mapping):
            field_name = item.get("field", "?")
            reason = item.get("reason", "")
            out.append(f"{field_name}: {reason}".strip().rstrip(":").strip())
        else:
            out.append(str(item))
    return out


@dataclass(frozen=True)
class UnrankedView:
    """Integrity-gated view: an unverified corpus yields NO rank, only the reframe."""

    unknown: list[str] = field(default_factory=list)
    is_ranked: bool = False
    headline_metric: str = "none"

    def __str__(self) -> str:  # noqa: D105
        lines = [
            _REFRAME_HEADLINE,
            "",
            _REFRAME_ONE_LINER,
            _REFRAME_BODY,
        ]
        if self.unknown:
            lines.append("")
            lines.append("unknown[] ledger (surfaces we could not verify):")
            for u in self.unknown:
                lines.append(f"  - {u}")
        return "\n".join(lines)


@dataclass(frozen=True)
class _LiftLine:
    harness: str
    lift: float
    ci_lo: float
    ci_hi: float
    theta: float | None
    bundle_unit: bool


@dataclass(frozen=True)
class RankingView:
    """Verified view: LIFT headline, bundle theta secondary, unknown[] surfaced."""

    lines: list[_LiftLine] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    data_sufficiency: Mapping[str, Any] = field(default_factory=dict)
    is_ranked: bool = True
    headline_metric: str = "lift"

    def __str__(self) -> str:  # noqa: D105
        out = ["LIFT — harness lift over bare model (headline metric):"]
        for ln in self.lines:
            ci = f"CI [{ln.ci_lo:+.2f}, {ln.ci_hi:+.2f}]"
            out.append(f"  {ln.harness}: lift {ln.lift:+.2f}  {ci}")
        # Secondary: bundle theta (appears AFTER the lift headline, always).
        out.append("")
        out.append("theta (bundle, secondary — not comparable across base models):")
        for ln in self.lines:
            theta_txt = "n/a" if ln.theta is None else f"{ln.theta:+.2f}"
            unit = " [bundle_unit]" if ln.bundle_unit else ""
            out.append(f"  {ln.harness}: theta {theta_txt}{unit}")
        if self.data_sufficiency:
            n_m = self.data_sufficiency.get("n_matches", "?")
            n_h = self.data_sufficiency.get("n_harnesses", "?")
            out.append("")
            out.append(f"data_sufficiency: {n_m} matches across {n_h} harnesses")
        out.append("")
        if self.unknown:
            out.append("unknown[] ledger (surfaces we could not verify):")
            for u in self.unknown:
                out.append(f"  - {u}")
        else:
            out.append("unknown[] ledger: none — all surfaces read cleanly")
        return "\n".join(out)


def render_ranking(data: Mapping[str, Any], *, verified: bool) -> RankingView | UnrankedView:
    """The one typed choke point for rank/rating/lift emission.

    ``data`` = ``{"ratings": <build_ratings_doc>, "lifts": <lift doc>}``.
    ``verified=False`` refuses to emit a rank and returns an :class:`UnrankedView`.
    """
    ratings = dict(data.get("ratings") or {})
    unknown = _unknown_list(ratings)

    if not verified or not ratings.get("verified", False):
        return UnrankedView(unknown=unknown)

    # Verified: LIFT is the headline. Index theta per harness for the secondary block.
    theta_by_harness: dict[str, tuple[float | None, bool]] = {}
    for h in ratings.get("harnesses", []) or []:
        theta_by_harness[h.get("harness")] = (
            h.get("theta"),
            bool(h.get("bundle_unit", False)),
        )

    lift_doc = dict(data.get("lifts") or {})
    lines: list[_LiftLine] = []
    for lf in lift_doc.get("lifts", []) or []:
        harness = lf.get("harness")
        ci = lf.get("ci") or {}
        theta, bundle_unit = theta_by_harness.get(harness, (None, False))
        lines.append(
            _LiftLine(
                harness=harness,
                lift=float(lf.get("lift", 0.0)),
                ci_lo=float(ci.get("lo", 0.0)),
                ci_hi=float(ci.get("hi", 0.0)),
                theta=theta,
                bundle_unit=bundle_unit,
            )
        )
    # Sort by lift descending so the strongest lift leads the headline block.
    lines.sort(key=lambda ln: ln.lift, reverse=True)

    return RankingView(
        lines=lines,
        unknown=unknown,
        data_sufficiency=ratings.get("data_sufficiency", {}) or {},
    )


# ---------------------------------------------------------------------------
# Harness role: builder vs fingerprint-only (CEO-5).
# ---------------------------------------------------------------------------

# A harness key maps to a builder adapter class name if one exists.
_BUILDER_ADAPTERS = {
    "claude-code": "ClaudeCodeAdapter",
    "copilot-cli": "CopilotCliAdapter",
}


def harness_role(key: str) -> str:
    """Return ``"builder"`` if a builder adapter exists for ``key``, else ``"fingerprint-only"``.

    codex is probed (fingerprint-only) but has no ``CodexAdapter`` builder yet, so it must
    NOT be framed as a competitor. This helper reads the adapters module at call time so it
    stays honest the moment a real adapter lands.
    """
    import atv_bench.adapters as adapters

    adapter_name = _BUILDER_ADAPTERS.get(key)
    if adapter_name and hasattr(adapters, adapter_name):
        return "builder"
    return "fingerprint-only"
