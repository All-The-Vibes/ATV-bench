"""Content-addressed immutable result bundle + offline reproduce (plan G7 + G9).

A bundle is a canonical-JSON document that pins a result so it can be verified offline by
anyone, with no network and no hidden state:

  * ``content_id`` — sha256 over the canonical bytes of the payload (every field EXCEPT
    ``content_id`` itself). Any 1-byte change to any field changes the id.
  * a reproduction tuple (``reproduce``) — seed, n_boot, baselines, cluster policy, and
    tool versions: everything needed to re-run the deterministic lift math.
  * the embedded ``matches`` and the ``published`` per-harness lift scalars, so
    ``verify_bundle`` can recompute the numbers offline and assert they match.

Track + trust-tier (G9) fold into the same document and are part of ``content_id``. They
default FAIL-CLOSED: an unspecified bundle is ``track='league'``,
``trust_tier='local-self-attested'`` and ``rankable=False`` — the weakest, unrankable
claim, never something stronger by omission.

Pure and deterministic: stdlib ``hashlib`` + ``json`` for the content address, and the real
``compute_lift`` for the offline reproduce check.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from atv_bench.lift import compute_lift
from atv_bench.rating import RatingMatch

__all__ = ["build_bundle", "verify_bundle", "canonical_bytes", "content_id_of"]

_TRACKS = ("league", "controlled", "systems")
_TRUST_TIERS = ("local-self-attested", "attested", "reproduced")

# Tolerance for the offline reproduce check. compute_lift's point estimate is a
# deterministic MLE refit, so equality is exact up to optimizer float noise.
_REPRO_TOL = 1e-9


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    """Canonical JSON bytes of a payload (sorted keys, tight separators)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_id_of(payload: Mapping[str, Any]) -> str:
    """sha256 hex of the canonical bytes of ``payload`` (excludes any ``content_id``)."""
    body = {k: v for k, v in payload.items() if k != "content_id"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def _match_to_dict(m: RatingMatch) -> dict[str, Any]:
    return {
        "harness_a": m.harness_a,
        "harness_b": m.harness_b,
        "model_a": m.model_a,
        "model_b": m.model_b,
        "score_a": float(m.score_a),
    }


def _match_from_dict(d: Mapping[str, Any]) -> RatingMatch:
    return RatingMatch(
        harness_a=d["harness_a"],
        harness_b=d["harness_b"],
        model_a=d["model_a"],
        model_b=d["model_b"],
        score_a=float(d["score_a"]),
    )


def _published_lifts(
    matches: list[RatingMatch], baselines: Mapping[str, str], *, seed: int, n_boot: int
) -> dict[str, float]:
    """The published per-harness lift point estimates (offline-recomputable)."""
    lifts = compute_lift(matches, dict(baselines), seed=seed, n_boot=n_boot)
    return {h: float(res.lift) for h, res in lifts.items()}


def build_bundle(
    ratings_doc: Mapping[str, Any],
    matches: list[RatingMatch],
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble a content-addressed, offline-reproducible result bundle.

    ``meta`` carries the reproduction tuple and the G9 schema fields:
      * ``seed`` (int, default 0), ``n_boot`` (int, default 1000)
      * ``baselines`` (harness -> bare-harness map) — required to compute lift
      * ``versions`` (dict), ``cluster_policy`` (str, default 'iid')
      * ``track``, ``trust_tier``, ``rankable`` (G9; fail-closed defaults)

    Raises ``ValueError`` on an unknown ``track`` or ``trust_tier``.
    """
    track = meta.get("track", "league")
    trust_tier = meta.get("trust_tier", "local-self-attested")
    if track not in _TRACKS:
        raise ValueError(
            f"invalid track {track!r}; must be one of {_TRACKS}")
    if trust_tier not in _TRUST_TIERS:
        raise ValueError(
            f"invalid trust_tier {trust_tier!r}; must be one of {_TRUST_TIERS}")
    rankable = bool(meta.get("rankable", False))

    seed = int(meta.get("seed", 0))
    n_boot = int(meta.get("n_boot", 1000))
    baselines = dict(meta.get("baselines", {}))
    cluster_policy = meta.get("cluster_policy", "iid")
    versions = dict(meta.get("versions", {}))

    match_dicts = [_match_to_dict(m) for m in matches]
    published = _published_lifts(matches, baselines, seed=seed, n_boot=n_boot)

    payload: dict[str, Any] = {
        "ratings_doc": json.loads(json.dumps(ratings_doc)),  # deep, JSON-safe copy
        "matches": match_dicts,
        "published": published,
        "reproduce": {
            "seed": seed,
            "n_boot": n_boot,
            "baselines": baselines,
            "cluster_policy": cluster_policy,
            "versions": versions,
        },
        "track": track,
        "trust_tier": trust_tier,
        "rankable": rankable,
    }
    payload["content_id"] = content_id_of(payload)
    return payload


def verify_bundle(bundle: Mapping[str, Any]) -> bool:
    """Verify a bundle: content address matches AND the published math reproduces offline.

    Returns ``False`` (never raises) on any tamper, malformed field, or numeric mismatch.
    """
    try:
        stored_id = bundle["content_id"]
        if content_id_of(bundle) != stored_id:
            return False

        # Schema fields must still be valid post-transit.
        if bundle.get("track") not in _TRACKS:
            return False
        if bundle.get("trust_tier") not in _TRUST_TIERS:
            return False

        rep = bundle["reproduce"]
        matches = [_match_from_dict(d) for d in bundle["matches"]]
        recomputed = _published_lifts(
            matches, rep["baselines"], seed=int(rep["seed"]), n_boot=int(rep["n_boot"])
        )

        published = bundle["published"]
        if set(published) != set(recomputed):
            return False
        for h, val in recomputed.items():
            if abs(float(published[h]) - val) > _REPRO_TOL:
                return False
        return True
    except (KeyError, TypeError, ValueError):
        return False
