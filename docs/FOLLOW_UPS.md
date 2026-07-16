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

What remains bot-asserted is the **win/loss/draw outcome**. The opponent anchor is pinned
at 1500 and excluded from ELO updates (`elo.compute_leaderboard(anchors=[...])`), so a
dishonestly-claimed win inflates only the forger's own row vs the fixed anchor and cannot
move the anchor or bleed into other entrants' ratings. See the "Match-result trust
boundary" section in `docs/COMMUNITY_LEAGUE.md`.

**Work:** have the CodeClash **arena** (not the submitted bot) emit the adjudicated
result inside the sandbox, so the outcome is derived from real gameplay rather than bot
stdout — the "live trusted match-orchestration" layer end-to-end.

## 2. Live `gh` PR submission automation

`atv-bench submit` builds the submission record; the contributor opens the PR manually
today (documented in `docs/COMMUNITY_LEAGUE.md` and the submit status trail). Wire the
live `gh pr create` path (fork, branch, push, open PR) behind the existing 7-check
preflight.

---

# Santa-loop re-review of PR #1 (net-new findings)

A fresh dual-reviewer santa-loop pass (Reviewer A: Claude Opus; Reviewer B: gpt-5.4
via Codex, read-only sandbox — no shared context) returned **NAUGHTY (both FAIL)**.
Item 1 above (adjudicated outcome) was re-confirmed and remains deferred/blast-radius-
bounded. The three items below are **net-new** and were verified against the tree.

## 3. `atv-bench/arena:latest` image missing — scoring path is dead (BUG)

`.github/workflows/league.yml:126` runs `docker run ... atv-bench/arena:latest`, but no
Dockerfile / arena image definition exists anywhere in the repo. Every match therefore
fails to pull the image and falls through to the `CRASH` forfeit fallback: in its shipped
state the ok/scoring pipeline can **never** produce a real "ok" match — only forfeits.
The ELO scoring path is dead in practice until the arena image ships.

**Work:** add the arena Dockerfile to the repo and pin the image **by digest** (not
`:latest`) so the sandbox is reproducible and cannot be swapped. Note: this couples to
item 1 — the arena is also what should author the adjudicated outcome.

## 4. Concurrent-publish store race drops matches (BUG)

`league.yml:24` scopes concurrency per-PR (`group: league-${{ pr.number || ref }}`), so
two different PRs' publish jobs run concurrently. `league.yml:254` does a single-shot
`git push origin HEAD:<default_branch>` with **no fetch / pull --rebase / retry**. The
losing race hits a non-fast-forward rejection, the job aborts, and that recorded match is
silently dropped — skewing ELO.

**Work:** use a **global** concurrency group (or a real lock) for the publish job, and
make the store push a fetch + rebase + retry loop so a losing race re-applies instead of
dropping a match.

## 5. ok-artifact not bound to immutable bot identity (ENHANCEMENT)

PR #1 closed identity + match_id forgery, but the `MatchSpec` is `(login, anchor,
run_id)` only. The executed bytes are **not** structurally bound to `bot_sha256` / PR
head SHA / fingerprint, so nothing ties a scored result to the specific submitted bytes
on record — a contributor could get one bot scored, then merge different bot/fingerprint
metadata under the same identity. Distinct from item 1 (which is about the *outcome*);
this is about binding the artifact to the *bot identity*.

**Work:** include `bot_sha256` / PR head SHA in the `MatchSpec` and reject on mismatch,
so a result is provably from the submitted bytes.
