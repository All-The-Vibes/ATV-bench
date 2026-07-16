# Santa-loop Round 1 — consolidated findings (PR #1 + PR #3)

Two independent reviewers (Reviewer A = Claude Opus; Reviewer B = Codex gpt-5.4, no shared
context) BOTH returned FAIL. Deduplicated, code-verified findings below. Ordered by severity.

## F1 — Submission-record path mismatch → board-invisible live entrant (BOTH, CRITICAL)
- `submit.py::open_submission_pr` writes the record to `league/submissions/<identity>/submission.json`
  (nested dir), submit.py:237-240; `tests/test_submit_live.py:98-104` asserts this nested layout.
- `store.py::load_submissions` reads ONLY flat `league/submissions/*.json` via non-recursive glob,
  and requires `identity == f.stem` (store.py:51,59). The nested record is never matched.
- `leaderboard` rows are built solely from the `submissions` dict → a `--live` entrant, even after
  scored matches, produces NO leaderboard row after merge.
- `CONTRIBUTING.md:76-80` documents a THIRD layout (bot at `submissions/<id>/main.py`, record at
  flat `submissions/<id>.json`). Three sources disagree.
- FIX: pick ONE layout. Recommend: bot at `league/submissions/<id>/main.py` (match job already
  reads `submissions/<submitter>/main.py`, league.yml:75-86) AND record at flat
  `league/submissions/<id>.json` (what the store reads). Align submit.py, store.py,
  test_submit_live.py, CONTRIBUTING.md. Add a test that `load_submissions()` ingests a
  live-submitted PR tree end-to-end.

## F2 — Trusted publish path never re-validates merged fingerprints (Reviewer B, SECURITY)
- `build_leaderboard_from_store` → `load_submissions` only checks filename/identity (store.py:47-67),
  then feeds records straight into `build_leaderboard_doc`, which copies `harness`/`skills`/`mcps`/
  `plugins` into published rows; schema only requires `type: string` (leaderboard.py:77-109).
- The leak-safe validator (`validate.py::validate_harness_fingerprint`, uses `scan.is_safe_name`)
  is NOT invoked on the publish/build path — verified: `scan` is not imported in store/leaderboard/
  publish. A hand-edited `league/submissions/<id>.json` merged via PR can publish secret-shaped
  `skills`/`mcps`/`plugins`/`harness` strings onto the static board.
- FIX: run leak-safe fingerprint validation on the publish/build path before a record's details
  enter a published row. Reject/scrub secret-shaped values (reuse `scan.is_safe_name`/`is_secret`,
  cover `harness` too). Fail-closed. Tripwire test: a planted secret-shaped skill never reaches
  the board doc.

## F3 — `submit --live` flow incomplete for first-time users (Reviewer B)
- `gh_preflight_runner` makes `fork_exists` a FAILING check when the user has no fork
  (submit.py:174-178); `cli.submit` refuses `open_submission_pr` unless ALL preflight pass
  (cli.py:163-176) → the advertised `gh repo fork` bootstrap never runs for first-timers.
- `open_submission_pr` does `gh repo fork --clone=false` then `git checkout -b` in `workdir` with no
  clone/init step (submit.py:232-245) — assumes an existing checkout.
- `pr_url`/`logs_url` claimed backfilled (submit.py:112-126) but no code rewrites the staged record
  after `gh pr create`.
- FIX: treat missing fork as non-fatal (bootstrap it), ensure a working tree exists before checkout,
  and backfill the committed record with real PR/log URLs after `gh pr create` (or drop the backfill
  claim from docs/comments and file follow-up). Keep fail-closed on real gh/git errors.

## F4 — Pages-freshness fix still TOCTOU (Reviewer B)
- `league-publish.yml:177-181` fetches/resets + rebuilds ONCE, then uploads/deploys that snapshot
  with no head-SHA fence (:183-189). Run A rebuilds `{A}`, run B pushes `{A,B}`, A deploys last →
  stale `{A}` board.
- FIX: gate deploy on the exact fetched head SHA (re-check origin head immediately before
  upload; if it moved, re-fetch+rebuild or abort-and-let-newer-win), OR move deploy to a separate
  default-branch `workflow_run`-on-store-commit. Tripwire must assert the fence, not just presence
  of a rebuild step.

## F5 — Docs/wiring inconsistency (Reviewer B)
- README.md:113-115 + FOLLOW_UPS.md:53-67 say `submit --live` resolved; CONTRIBUTING.md:21-23 +
  COMMUNITY_LEAGUE.md:11-14 still say "live automation not wired". Plus the F1 path disagreement.
- FIX: make all four docs consistent with the shipped behavior + chosen layout.

## F6 — Test quality gaps (BOTH)
- `test_submit_live.py:92-104` hardcodes the wrong nested layout, never asserts store ingestion.
- Freshness tripwire only checks a rebuild step exists, not the TOCTOU fence.
- Forfeit reason collapses TIMEOUT→CRASH though the source knows it hung (referee.py:165-169,233-236)
  — diagnostic-only, LOW.
- FIX: land the missing end-to-end assertions alongside F1/F2/F4 (they are the regression tests).

## Non-findings (both reviewers PASS, do NOT touch)
- Referee/spec-binding trust boundary, secret scanner core, fork-safe workflow_run split,
  baked-pkg byte-identity, order-independent ELO w/ pinned anchor, fail-closed artifact ingest.
