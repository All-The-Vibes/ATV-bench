"""Publish-side entrypoint (trusted job): validate artifact, ingest, build board.

Deliberately imports NOTHING that executes a bot or the arena. It:
  - `validate`  : validate a match-result artifact against the result contract.
  - `ingest`    : append a validated result to the committed match history.
  - `build`     : recompute the leaderboard from the store and render the static site.

Invoked by the publish job in .github/workflows/league.yml. The board is built from
the committed `league/` store (submissions + match history), never hardcoded — a
regression to an empty/1970 board is caught by tests/test_publish.py.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atv_bench.elo import ForfeitReason, Outcome
from atv_bench.leaderboard import validate_leaderboard
from atv_bench.store import LeagueStore, build_leaderboard_from_store, _read_text_bounded

# A match-result artifact is small JSON (a handful of fields). Bound the trusted read so
# an oversized artifact fails closed instead of OOM-ing the trusted publish job.
_MAX_ARTIFACT_BYTES = 256 * 1024

_VALID_STATUSES = {"ok", "crash", "invalid_output"}
_FORFEIT_OUTCOMES = {Outcome.FORFEIT_A.value, Outcome.FORFEIT_B.value}
_OK_REQUIRED_KEYS = {"player_a", "player_b", "outcome", "match_id"}
_CRASH_REQUIRED_KEYS = {"loser", "opponent", "match_id"}
_EPOCH = "1970-01-01T00:00:00Z"
_DEFAULT_STORE = "league"


class SpecMismatch(Exception):
    """An ok artifact's identities/match_id do not bind to the workflow-issued spec.

    NOT a validation error (the artifact is well-formed) — it means the untrusted bot
    asserted participants or a match_id it was never issued. The caller fails closed:
    the match is recorded as a CRASH forfeit against the submitter, never with the
    forged data and never dropped.
    """


@dataclass(frozen=True)
class MatchSpec:
    """The TRUSTED match identity, issued by the workflow from GitHub context.

    An untrusted bot controls only its stdout; it does NOT control this. `submitter` is
    the PR author (`github.event.pull_request.user.login`), `opponent` the roster anchor
    the match job pitted it against, `match_id` the STABLE run id (`github.run_id`, with
    no run_attempt so a publish re-run reuses the same id and never rebinds an honest
    earlier-attempt artifact into a forfeit). The publish job binds the bot's `ok` claim
    to these before anything enters permanent ELO history.
    """
    submitter: str
    opponent: str
    match_id: str
    # Trusted sha256 of the EXACT bot bytes the match job mounted (santa re-review #5).
    # Optional: None means bot-identity binding is not enforced (local/hermetic use, or a
    # workflow that does not export it). When set, the stored record is stamped with THIS
    # value and a disagreeing bot-reported bot_sha256 is rejected — so a SCORED RESULT is
    # provably tied to the bytes that produced it, never a later-swapped bot under the same
    # login for THAT match. Scope note: the leaderboard ROW is keyed by login and its ELO is
    # recompute-from-full-history BY DESIGN — a per-contributor/harness league where an
    # entrant iterates their bot over time. The row's published bot_sha256 is separately
    # re-derived from the committed main.py at load, so the displayed hash always matches the
    # current on-disk bot; this is not a claim that ELO resets per bot edit.
    bot_sha256: "str | None" = None

    @classmethod
    def from_env(cls) -> "MatchSpec":
        """Build the spec from the workflow-exported env (ATV_SUBMITTER/OPPONENT/MATCH_ID).

        Fails closed: a missing/blank value means the trusted context is absent, and we
        must NOT fall back to trusting the bot's self-reported identities. ATV_BOT_SHA256
        is OPTIONAL (an enhancement over the load-bearing identity+match_id binding): if
        absent/blank the spec's bot_sha256 is None and byte-binding is simply not enforced,
        rather than failing the whole publish."""
        vals = {}
        for field_name, env in (("submitter", "ATV_SUBMITTER"),
                                ("opponent", "ATV_OPPONENT"),
                                ("match_id", "ATV_MATCH_ID")):
            v = os.environ.get(env, "")
            if not v.strip():
                raise ValueError(f"{env} must be a non-empty string (trusted match spec missing)")
            vals[field_name] = v.strip()
        if vals["submitter"] == vals["opponent"]:
            # If the two identities collapse, the participant-set check degenerates and a
            # self-match could bind. Enforce the invariant locally at spec construction.
            raise ValueError(
                f"submitter and opponent must differ (both {vals['submitter']!r})")
        sha = os.environ.get("ATV_BOT_SHA256", "").strip()
        vals["bot_sha256"] = sha or None
        return cls(**vals)


def bind_ok_to_spec(data: dict[str, Any], spec: MatchSpec) -> dict[str, Any]:
    """Bind a validated `ok` artifact to the trusted spec, or raise SpecMismatch.

    The bot may assert the OUTCOME (bot-asserted in v1; honest trust boundary). It may
    NOT assert WHO played or WHICH match this is. The two reported identities must be
    exactly {submitter, opponent} and the match_id must equal the issued one. On success
    the returned record's identities + match_id are taken from the SPEC (canonicalized to
    a fixed orientation: player_a=opponent, player_b=submitter) — so even an accepted
    record never carries a bot-chosen string in an identity/id field. The bot-asserted
    outcome is TRANSLATED to preserve the winner under that canonical orientation.
    """
    pa, pb = data.get("player_a"), data.get("player_b")
    reported = {pa, pb}
    expected = {spec.submitter, spec.opponent}
    # Reject a collapsed self-match (pa == pb) explicitly: the set comparison alone would
    # miss it if the spec identities ever collapsed. spec.submitter != spec.opponent is
    # enforced at construction, so a genuine two-identity match always has pa != pb.
    if pa == pb or reported != expected:
        raise SpecMismatch(
            f"reported participants {sorted(str(x) for x in reported)} != "
            f"issued {sorted(expected)}")
    if data.get("match_id") != spec.match_id:
        raise SpecMismatch(
            f"reported match_id {data.get('match_id')!r} != issued {spec.match_id!r}")
    # Bot-identity binding (item 5): if the trusted spec carries a bot_sha256 AND the bot
    # ALSO reports one, they must agree — a disagreement means the artifact claims bytes
    # other than the ones the trusted job mounted. Reject to a forfeit. (A bot that reports
    # NO sha is fine: the trusted value is stamped regardless below, so the record is bound
    # to the real bytes either way.)
    if spec.bot_sha256 is not None:
        reported_sha = data.get("bot_sha256")
        if reported_sha is not None and reported_sha != spec.bot_sha256:
            raise SpecMismatch(
                f"reported bot_sha256 {reported_sha!r} != issued {spec.bot_sha256!r}")
    # Canonicalize identities from the trusted spec (fixed orientation), and translate
    # the bot-asserted outcome so the WINNER is preserved rather than silently flipped.
    rec = dict(data)
    rec["player_a"] = spec.opponent
    rec["player_b"] = spec.submitter
    rec["match_id"] = spec.match_id
    # Stamp the TRUSTED bot bytes hash (never the bot-reported one) so a scored match is
    # provably tied to the submitted bytes. Omitted when the spec carries no sha.
    if spec.bot_sha256 is not None:
        rec["bot_sha256"] = spec.bot_sha256
    else:
        rec.pop("bot_sha256", None)
    rec["outcome"] = _translate_outcome(
        data["outcome"], bot_a=pa, canonical_a=spec.opponent)
    return rec


# outcome under a flipped A/B orientation: swap winner side, leave symmetric ones.
_OUTCOME_FLIP = {
    Outcome.A_WINS.value: Outcome.B_WINS.value,
    Outcome.B_WINS.value: Outcome.A_WINS.value,
    Outcome.FORFEIT_A.value: Outcome.FORFEIT_B.value,
    Outcome.FORFEIT_B.value: Outcome.FORFEIT_A.value,
    Outcome.DRAW.value: Outcome.DRAW.value,
}


def _translate_outcome(outcome: str, *, bot_a: Any, canonical_a: str) -> str:
    """Re-express a bot-asserted outcome for the canonical A/B orientation.

    The bot labeled the outcome relative to ITS player_a (`bot_a`). If canonical player_a
    is the same identity, the outcome is unchanged; if it's the other identity, A and B
    swapped so the outcome must flip (a_wins<->b_wins, forfeit_a<->forfeit_b, draw fixed).
    """
    if bot_a == canonical_a:
        return outcome
    return _OUTCOME_FLIP[outcome]


def _require_nonempty_str(data: dict[str, Any], key: str) -> None:
    v = data.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{key} must be a non-empty string, got {v!r}")


def _check_optional_common(data: dict[str, Any]) -> None:
    """Type-check optional fields present on ANY status. Untyped optionals are a
    poison vector: a bad `seed` crashes int() in ingest, a dict `game` crashes the
    ELO sort — both inside the trusted job. Reject at the boundary."""
    if "seed" in data and (not isinstance(data["seed"], int) or isinstance(data["seed"], bool)):
        raise ValueError(f"seed must be an integer, got {data['seed']!r}")
    if "game" in data and (not isinstance(data["game"], str) or not data["game"].strip()):
        raise ValueError(f"game must be a non-empty string, got {data.get('game')!r}")
    if "forfeit_reason" in data and data["forfeit_reason"] is not None:
        if data["forfeit_reason"] not in {r.value for r in ForfeitReason}:
            raise ValueError(f"invalid forfeit_reason {data['forfeit_reason']!r}")


def validate_artifact(path: str) -> dict[str, Any]:
    """Validate a match-result artifact — FAIL-CLOSED at the trust boundary.

    An untrusted bot's run produced this. We reject anything that isn't one of the
    known result shapes with correctly-TYPED fields (required AND optional) BEFORE it
    can reach the store or the ELO engine. Guarantee: anything this ACCEPTS ingests and
    builds without a trusted-job crash.
    """
    try:
        data = json.loads(_read_text_bounded(Path(path), _MAX_ARTIFACT_BYTES, "artifact"))
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"artifact is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise ValueError("artifact must be a JSON object")
    status = data.get("status")
    if status not in _VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(_VALID_STATUSES)}, got {status!r}")
    _check_optional_common(data)
    if status == "ok":
        missing = _OK_REQUIRED_KEYS - set(data)
        if missing:
            raise ValueError(f"ok result missing keys {sorted(missing)}")
        _require_nonempty_str(data, "player_a")
        _require_nonempty_str(data, "player_b")
        _require_nonempty_str(data, "match_id")
        outcome = data["outcome"]
        if outcome not in {o.value for o in Outcome}:
            raise ValueError(f"invalid outcome {outcome!r}")
        if outcome in _FORFEIT_OUTCOMES and data.get("forfeit_reason") not in {r.value for r in ForfeitReason}:
            raise ValueError(f"forfeit outcome requires a valid forfeit_reason, got {data.get('forfeit_reason')!r}")
        # cross-field invariant (mirrors MatchResult.__post_init__): a non-forfeit
        # outcome must NOT carry a forfeit_reason, else the trusted build raises.
        if outcome not in _FORFEIT_OUTCOMES and data.get("forfeit_reason") is not None:
            raise ValueError(f"forfeit_reason set on non-forfeit outcome {outcome!r}")
    elif status in ("crash", "invalid_output"):
        missing = _CRASH_REQUIRED_KEYS - set(data)
        if missing:
            raise ValueError(f"{status} record missing keys {sorted(missing)} "
                             "(loser+opponent needed to score the forfeit)")
        _require_nonempty_str(data, "loser")
        _require_nonempty_str(data, "opponent")
        _require_nonempty_str(data, "match_id")
    return data


def _submitter_forfeit(spec: MatchSpec, *, game: str, seed: int) -> dict[str, Any]:
    """Score the submitter as forfeiting against the trusted opponent (reason CRASH).

    Used both for real crashes and for a rebound spec-mismatch, so a forged `ok` costs
    the forger a loss instead of crediting a fabricated win — and never enters the store
    with the forged identities."""
    return {
        "player_a": spec.opponent,
        "player_b": spec.submitter,
        "outcome": Outcome.FORFEIT_B.value,   # player_b (submitter) forfeited
        "forfeit_reason": ForfeitReason.CRASH.value,
        "match_id": spec.match_id,
        "game": game,
        "seed": seed,
    }


def ingest_result(path: str, *, store_dir: str = _DEFAULT_STORE,
                   spec: "MatchSpec | None" = None) -> bool:
    """Append a validated result to the store's history.

    When a trusted `spec` is supplied (the workflow always supplies one), an `ok`
    result's identities + match_id are BOUND to the spec: a mismatch means the untrusted
    bot forged participants or a match_id, so we rebind to a CRASH forfeit against the
    submitter — never trusting the forgery, never dropping the match. A `crash`/
    `invalid_output` record is scored as a FORFEIT LOSS for the crashing player (reason
    CRASH) — never dropped, because a dropped forfeit skews everyone's ELO. With a spec,
    the crash identities are taken from the trusted spec too. Returns True when a match
    was appended.

    Without a spec (local/hermetic use) the prior verbatim behavior is preserved.
    """
    data = validate_artifact(path)
    store = LeagueStore(store_dir)
    game = data.get("game", "battlesnake")
    seed = int(data.get("seed", 0))
    if data["status"] == "ok":
        if spec is not None:
            try:
                data = bind_ok_to_spec(data, spec)
            except SpecMismatch:
                # forged identities/match_id -> submitter forfeits, forgery FULLY
                # discarded: use canonical game/seed defaults so no bot-supplied value
                # (even a type-valid one) rides along on a rejected forgery.
                store.append_match(_submitter_forfeit(spec, game="battlesnake", seed=0))
                return True
        match = {
            "player_a": data["player_a"],
            "player_b": data["player_b"],
            "outcome": data["outcome"],
            "match_id": data["match_id"],
            "game": game,
            "seed": seed,
        }
        if data.get("forfeit_reason"):
            match["forfeit_reason"] = data["forfeit_reason"]
        if data.get("bot_sha256"):
            # Present only when a trusted spec stamped it (bind_ok_to_spec). Carries the
            # bot-identity binding through into the durable match record.
            match["bot_sha256"] = data["bot_sha256"]
        store.append_match(match)
        return True
    # crash / invalid_output -> forfeit loss for the crasher
    if spec is not None:
        # crash identities come from the trusted spec, not the (workflow-built but
        # still normalized-here) record, so match_id + participants stay canonical.
        store.append_match(_submitter_forfeit(spec, game=game, seed=seed))
        return True
    loser, opponent = data["loser"], data["opponent"]
    store.append_match({
        "player_a": opponent,
        "player_b": loser,
        "outcome": Outcome.FORFEIT_B.value,   # player_b (loser) forfeited
        "forfeit_reason": ForfeitReason.CRASH.value,
        "match_id": data["match_id"],
        "game": game,
        "seed": seed,
    })
    return True


def ingest_result_or_forfeit(path: str, *, store_dir: str = _DEFAULT_STORE,
                             spec: "MatchSpec | None" = None) -> bool:
    """Single fail-closed scoring gate for the trusted publish job.

    A bot can emit a KNOWN status with an INVALID schema (e.g. `{"status":"ok"}` with no
    players, or non-JSON). The match-job sanitizer only checks dict+status, so such an
    artifact is uploaded. If the publish job ran a separate hard-failing `validate` step,
    that would abort the WHOLE job and record NO score — a bot-controlled no-score DoS.

    Instead, ingest here: any validation failure (malformed/invalid/mistyped) is
    converted into a spec-bound submitter CRASH forfeit — scored as a loss, never an
    aborted job, never a dropped match. A spec is REQUIRED: without a trusted identity
    there is nobody to attribute the forfeit to, so we refuse rather than guess.
    """
    if spec is None:
        raise ValueError("ingest_result_or_forfeit requires a trusted MatchSpec")
    try:
        return ingest_result(path, store_dir=store_dir, spec=spec)
    except (ValueError, OSError):
        # artifact failed the schema contract, was unreadable, or absent -> the submitter
        # forfeits (CRASH), scored against the trusted opponent with the issued match_id.
        # No bot data persisted. SpecMismatch is NOT caught here (it subclasses Exception,
        # not ValueError) — a forged-but-valid ok is already rebound inside ingest_result,
        # so this except only ever fires on a genuinely malformed/missing artifact, never
        # masking a binding bug.
        LeagueStore(store_dir).append_match(_submitter_forfeit(spec, game="battlesnake", seed=0))
        return True


def _normalize_utc_z(ts: str) -> str:
    """Normalize an ISO-8601 timestamp to the schema's required `...Z` UTC form.

    git %cI emits a numeric offset (e.g. 2026-07-15T15:36:06-05:00, or +00:00 on a
    UTC runner). The leaderboard schema requires a `Z` suffix, so build_site must
    convert; otherwise validate_leaderboard raises on every real publish run.
    """
    from datetime import datetime, timezone
    s = ts.strip()
    if s.endswith("Z") and "+" not in s:
        return s
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        # unparseable -> fall back to epoch (schema-valid, obviously-stale)
        return _EPOCH


def build_site(out_dir: str, *, store_dir: str = _DEFAULT_STORE, updated_at: str = _EPOCH) -> Path:
    """Render the static leaderboard site from the committed store.

    The board is computed from the store (submissions + match history). An empty
    store yields a schema-valid empty board (legitimate: no entrants yet). The
    timestamp is normalized to UTC `Z` so a git-commit-time input validates.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    doc = build_leaderboard_from_store(store_dir, updated_at=_normalize_utc_z(updated_at))
    validate_leaderboard(doc)
    (out / "leaderboard.json").write_text(json.dumps(doc, indent=2))
    view = Path(__file__).parent.parent.parent / "leaderboard" / "view" / "index.html"
    if view.exists():
        (out / "index.html").write_text(view.read_text())
    return out


def _arg(argv: list[str], flag: str, default: str) -> str:
    return argv[argv.index(flag) + 1] if flag in argv else default


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m atv_bench.publish {validate <a>|ingest <a>|build --out <d>}",
              file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "validate":
        validate_artifact(argv[1])
        print(f"artifact OK: {argv[1]}")
        return 0
    if cmd == "ingest":
        store = _arg(argv, "--store", _DEFAULT_STORE)
        # --require-spec: the trusted publish job MUST bind to a workflow-issued spec.
        # It fails closed (from_env raises) if the trusted context is missing, rather
        # than silently trusting the untrusted bot's self-reported identities. Ingest is
        # the SINGLE fail-closed gate: an invalid/malformed artifact becomes a spec-bound
        # submitter forfeit instead of aborting the job (no bot-controlled no-score DoS).
        if "--require-spec" in argv:
            appended = ingest_result_or_forfeit(argv[1], store_dir=store,
                                                spec=MatchSpec.from_env())
        else:
            appended = ingest_result(argv[1], store_dir=store)
        print("match appended" if appended else "crash/invalid record — not scored")
        return 0
    if cmd == "build":
        out = _arg(argv, "--out", "site")
        store = _arg(argv, "--store", _DEFAULT_STORE)
        updated = _arg(argv, "--updated-at", _EPOCH)
        p = build_site(out, store_dir=store, updated_at=updated)
        print(f"built site: {p}")
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
