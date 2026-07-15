# Deferred follow-ups (post-PR #1)

Tracked here because issue creation is unavailable on this account (Enterprise
Managed User restriction). These are genuinely deferred v1-adjacent scope, not bugs.

## 1. Adjudicated match outcome (live match-orchestration)

PR #1 closes **identity + match_id forgery**: the trusted publish job binds an `ok`
artifact's `player_a`/`player_b`/`match_id` to a workflow-issued `MatchSpec` (submitter
= PR author, opponent = anchor, match_id = run-scoped). A forged third-party identity,
fabricated match_id, or self-match is rebound to a `CRASH` forfeit against the submitter
— never trusted, never dropped. See `tests/test_match_binding.py` and the `league.yml`
tripwire in `tests/test_action_isolation.py`.

What remains bot-asserted is the **win/loss/draw outcome**. Because the opponent is a
fixed baseline anchor (not another real entrant), a dishonestly-claimed win can only
inflate the forger's own row vs the anchor and cannot damage a third party's rating
(blast-radius-bounded). See the "Match-result trust boundary" section in
`docs/COMMUNITY_LEAGUE.md`.

**Work:** have the CodeClash **arena** (not the submitted bot) emit the adjudicated
result inside the sandbox, so the outcome is derived from real gameplay rather than bot
stdout — the "live trusted match-orchestration" layer end-to-end.

## 2. Live `gh` PR submission automation

`atv-bench submit` builds the submission record; the contributor opens the PR manually
today (documented in `docs/COMMUNITY_LEAGUE.md` and the submit status trail). Wire the
live `gh pr create` path (fork, branch, push, open PR) behind the existing 7-check
preflight.
