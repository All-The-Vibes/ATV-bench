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

## 3. `atv-bench/arena:latest` image missing — scoring path is dead (BUG) — ✅ RESOLVED

`.github/workflows/league.yml:126` runs `docker run ... atv-bench/arena:latest`, but no
Dockerfile / arena image definition exists anywhere in the repo. Every match therefore
fails to pull the image and falls through to the `CRASH` forfeit fallback: in its shipped
state the ok/scoring pipeline can **never** produce a real "ok" match — only forfeits.
The ELO scoring path is dead in practice until the arena image ships.

**Work:** add the arena Dockerfile to the repo and pin the image **by digest** (not
`:latest`) so the sandbox is reproducible and cannot be swapped. Note: this couples to
item 1 — the arena is also what should author the adjudicated outcome.

**Resolved:** added `arena/Dockerfile` (base pinned by digest, non-root USER). The match
job now `docker build`s it from the TRUSTED default-branch checkout and runs a run-scoped
local tag `atv-bench/arena:${{ github.run_id }}` — never the mutable/unbuilt `:latest`.
Tripwire: `tests/test_arena_image.py`. Verified the image builds and executes a mounted
bot under the full sandbox flag set. (The adjudicated-outcome layer — item 1 — is still
deferred; this fix makes the scoring path live.)

## 4. Concurrent-publish store race drops matches (BUG) — ✅ RESOLVED

`league.yml:24` scopes concurrency per-PR (`group: league-${{ pr.number || ref }}`), so
two different PRs' publish jobs run concurrently. `league.yml:254` does a single-shot
`git push origin HEAD:<default_branch>` with **no fetch / pull --rebase / retry**. The
losing race hits a non-fast-forward rejection, the job aborts, and that recorded match is
silently dropped — skewing ELO.

**Work:** use a **global** concurrency group (or a real lock) for the publish job, and
make the store push a fetch + rebase + retry loop so a losing race re-applies instead of
dropping a match.

**Resolved (optimistic-concurrency, NO serializing group):** the persist step is a
DEADLINE-bounded retry loop with backoff: fetch origin → `reset --hard origin/<default>`
+ `git clean -fd league/` → **re-ingest THIS match** (idempotent: matches.jsonl dedups on
the stable `github.run_id` match_id) → rebuild → `git add -A league/` → push; a rejected
push backs off and re-applies. This is race-safe **without** a GitHub concurrency group.

Note we deliberately do **NOT** use a job-level `concurrency` group. A constant group
would *reintroduce* the drop: GitHub keeps only one *pending* run per group and cancels an
older pending run when a newer queues (per the docs, "any existing pending job… will be
canceled"; `cancel-in-progress: false` protects only the *in-progress* run), so a burst of
publishes silently loses the cancelled ones. The optimistic-retry loop instead guarantees
every scored match persists within the job's time budget, and fails **closed** (exit 1,
loud + re-runnable) if the deadline is ever hit — it never silently drops a match. Two
subtle bugs were fixed here in re-review: `git diff --quiet -- league/` ignores the
*untracked* first-match file (now stage `git add -A` then check `git diff --cached`), and
the board `--updated-at` used the previous commit's time (now current time). Tripwire:
`tests/test_publish_race.py`.

## 5. ok-artifact not bound to immutable bot identity (ENHANCEMENT)

PR #1 closed identity + match_id forgery, but the `MatchSpec` is `(login, anchor,
run_id)` only. The executed bytes are **not** structurally bound to `bot_sha256` / PR
head SHA / fingerprint, so nothing ties a scored result to the specific submitted bytes
on record — a contributor could get one bot scored, then merge different bot/fingerprint
metadata under the same identity. Distinct from item 1 (which is about the *outcome*);
this is about binding the artifact to the *bot identity*.

**Work:** include `bot_sha256` / PR head SHA in the `MatchSpec` and reject on mismatch,
so a result is provably from the submitted bytes.
