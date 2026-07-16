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

_Original bug statement (line refs describe the PRE-FIX file, kept for history):_ the
workflow scoped concurrency per-PR (`group: league-${{ pr.number || ref }}`) and the
persist step did a single-shot `git push origin HEAD:<default_branch>` with **no fetch /
rebase / retry**. The losing race hit a non-fast-forward rejection, the job aborted, and
that recorded match was silently dropped — skewing ELO.

**Work (original suggestion — see the corrected resolution below):** make the store
push resilient to a non-fast-forward race so a losing publish re-applies instead of
dropping a match. (The original text floated a "global concurrency group or a real lock";
re-review showed a GitHub concurrency group is the *wrong* tool here — see why below.)

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

## 5. ok-artifact not bound to immutable bot identity (ENHANCEMENT) — ✅ RESOLVED

PR #1 closed identity + match_id forgery, but the `MatchSpec` is `(login, anchor,
run_id)` only. The executed bytes are **not** structurally bound to `bot_sha256` / PR
head SHA / fingerprint, so nothing ties a scored result to the specific submitted bytes
on record — a contributor could get one bot scored, then merge different bot/fingerprint
metadata under the same identity. Distinct from item 1 (which is about the *outcome*);
this is about binding the artifact to the *bot identity*.

**Work:** include `bot_sha256` / PR head SHA in the `MatchSpec` and reject on mismatch,
so a result is provably from the submitted bytes.

**Resolved:** `MatchSpec` gained an optional trusted `bot_sha256` (computed by the match
job from the exact staged bytes via `sha256sum submission/main.py`, handed to the trusted
publish side). `bind_ok_to_spec` stamps the trusted hash onto the record and rejects a
disagreeing bot-reported `bot_sha256` to a CRASH forfeit — never trusts the claim, never
drops the match. Optional field = full back-compat. Tripwire:
`tests/test_bot_identity_binding.py`.

## 6. Stale Pages deploy race (BUG — pre-existing, out of scope for #2/#3) — ✅ RESOLVED

Surfaced by santa-loop final-check (Reviewer B, gpt-5.4). The persist retry loop makes the
`league/` STORE push race-safe, but the **Pages deploy** is not. Each publish job builds
`./site` inside the loop from its own snapshot, then `upload-pages-artifact` +
`deploy-pages` run AFTER the loop with no final rebuild from the settled default-branch
head. If an older publisher's deploy finishes last, GitHub Pages regresses to a stale
board even though the store is correct. Not a regression from the #2/#3 fixes — the deploy
path is unchanged from the original PR.

**Work:** after the store push settles, rebuild `./site` from a fresh
`origin/<default_branch>` immediately before `upload-pages-artifact`, OR move Pages
deployment into a separate default-branch workflow (`workflow_run` on the store commit) so
the deployed board always reflects the latest settled store. Add a tripwire asserting the
deployed artifact is rebuilt from the settled state.

**Resolved:** a dedicated step now fetches + `reset --hard origin/<default_branch>` and
rebuilds `./site` from the settled store immediately before `upload-pages-artifact`, so the
deployed board reflects the latest settled state (this match plus any concurrently landed)
rather than a per-attempt snapshot. Now lives in the trusted `league-publish.yml` workflow
(see item 7). Tripwire: `tests/test_pages_deploy_freshness.py`.

## 7. Forked-PR read-only token blocks the documented contributor flow (BUG — pre-existing) — ✅ RESOLVED

Surfaced by santa-loop final-check (Reviewer B). `CONTRIBUTING.md` documents "fork → open
PR → maintainer labels `run-match`", but the workflow runs on `pull_request` and the
trusted publish job needs `contents: write` + `pages: write` + `id-token: write`. GitHub
gives `pull_request` runs from FORKED repos a **read-only** `GITHUB_TOKEN`, so the normal
external-contributor path can score in-workspace but cannot persist `league/` or deploy
Pages. Works only for same-repo branches today. Not a regression from #2/#3.

**Work:** move the privileged persist/deploy phase onto a trusted follow-up trigger
(`workflow_run` / `pull_request_target` with strict no-PR-code discipline, or a
maintainer-run default-branch workflow) that can legitimately write on fork submissions
without executing untrusted code.

**Resolved:** the privileged persist/deploy phase moved to a new `league-publish.yml`
workflow triggered on `workflow_run` (the `league` match workflow completing). A
workflow_run workflow runs in the TRUSTED base-repo context with a full write token even
for fork PRs, and this one never checks out or executes PR code — it consumes only (a) the
bot's result artifact and (b) a *trusted* `match-meta` artifact (submitter/opponent/
match_id/bot_sha256, authored by the match job from GitHub context, never bot stdout),
downloaded cross-run via `workflow_run.id`. It gates on `workflow_run.conclusion ==
'success'`, ingests with `--require-spec`, runs the same optimistic-concurrency persist
loop (#3) and settled-store rebuild (#6). The untrusted match job in `league.yml` now
holds no write scope at all. Tripwire: `tests/test_fork_safe_publish.py` (plus the
publish-side assertions in `test_action_isolation.py` / `test_publish_race.py` /
`test_pages_deploy_freshness.py` retargeted to the new workflow).

## 8. Integration-test flag parity drift (TEST — pre-existing) — ✅ RESOLVED

Surfaced by santa-loop final-check (Reviewer B). `tests/test_action_malicious_bot.py` uses
`--memory 256m` and `python:3.12-alpine`, which no longer match the workflow's `512m` and
the real arena image (`arena/Dockerfile`). The gated integration test's "exact sandbox
flags" parity claim is weakened. Not a scoring/security defect.

**Work:** sync the gated test's docker flags + image with the actual `league.yml` match
step (ideally derive both from one source) so the parity claim stays true.

**Resolved:** the integration test now uses `--memory 512m` and builds+runs the in-repo
arena image (`arena/Dockerfile`), matching the workflow. A new `test_sandbox_flag_parity.py`
tripwire parses the real match step from `league.yml` and fails on every push if the
memory cap, core isolation flags, or image drift from the integration test again.

---

## Santa-loop convergence note (PR #1 re-review)

The re-review ran to the 3-iteration fix limit + a confirmation round. Rounds 1–3 fixed
every in-scope defect in the #2/#3 work (untracked-first-match drop, job-level and
workflow-level concurrency footguns, vacuous/stale tests, deadline-bounded retry, board
timestamp) — Reviewer A (Opus) returned PASS on rounds 2, 3, and the confirmation round,
and all fixes are mutation-verified. The confirmation round's remaining FAIL (Reviewer B)
raised items 6–8 above: genuinely real but **broader, pre-existing** issues in the deploy
path and contributor flow, NOT regressions from the #2/#3 fixes. Per the escalation rule
(new problems in new places after 3 rounds = stop looping, don't expand scope mid-loop),
these are tracked here for a dedicated follow-up rather than fixed in this pass.
