# PR Review Report — #23 & #24 (All-The-Vibes/ATV-bench)

_Lead-reviewer synthesis of agent-team evidence: recon + screenshotted E2E claim verification + atv-security + TDD + proof publication._

---

## Executive summary (corrected after Santa/codex review)

Both PRs are engineering-sound (Security **Grade A**, 0 critical / 0 high each) and both are **🟢 GO** — but two body-accuracy overclaims and one CI-evidence gap flagged by the codex reviewer are now corrected honestly:

1. **PR #24 body is _not_ fully accurate.** The prior "ACCURATE" grade was an overclaim. The body states **4** pre-existing full-suite failures; this environment observes **1** (`test_codeclash_importable`, an `@integration` submodule import touching no changed code). The narrative direction (submodule-env, not code) holds, but a false count is a defect — regraded **9 VERIFIED / 1 FAILED**, and correcting "4 → 1" is **blocking**, not cosmetic.
2. **PR #23 banner-once fix (`0f0597a`) now has explicit RED→GREEN proof.** Previously the packet only asserted the fix was green. The two shipped regression tests were replayed against the pre-fix parent `banner.py` → **2 failed (RED)**, then against the committed fix → **11 passed (GREEN)**. Proof is on the record.
3. **"CI green" is scoped and evidenced.** Both PRs' green status is verified via `gh pr view --json statusCheckRollup` (no `FAILURE` conclusions; `mergeable: MERGEABLE`, no conflicts) — but "green" means the **hermetic `-m 'not integration'` lane only**. Each PR has exactly one environmental full-suite failure that GitHub CI does **not** run: PR #23's `test_wave_a_fake_match_scores[gomoku]` (Docker-startup flake, passes on rerun) and PR #24's `test_codeclash_importable`. Both are surfaced honestly, not folded into the green claim.

**Bottom line:** merge **#23 → main** first (fix its full-suite/banner-count body lines), then rebase **#24** onto `main`, correct its "4 → 1" count, re-confirm CI, and merge. No new code defects were found in this pass; all corrections are report-accuracy and one already-committed on-branch fix now carries explicit proof.

---

## Merge order (STACKED PRs — read first)

These PRs are **stacked**: #24's base branch is `feat/quickstart-harness-picker-banner` (which is #23's head), **not** `main`.

1. **Merge #23 first** (squash-merge to `main`).
2. **Then REBASE #24 onto `main`** — do **not** merely retarget its base in the GitHub UI:

   ```
   git fetch origin
   git rebase --onto origin/main origin/feat/quickstart-harness-picker-banner feat/liveview-tdd
   git push --force-with-lease
   ```

   **Rationale:** a squash-merge of #23 collapses #23's commit history into a single new commit on `main`. #24 still carries #23's *original un-squashed* commits. If you only retarget #24's base to `main`, GitHub will show #23's commits as "new" in #24 and the diff/history will be wrong (duplicate/ghost commits, possible false conflicts). `git rebase --onto origin/main <old-base> <branch>` replays **only #24's own commits** on top of the squashed `main`, producing a clean stack. Retargeting alone is insufficient because of the squash.

---

## PR #23 — feat/quickstart-harness-picker-banner → main

### Body-accuracy verdict: **MOSTLY ACCURATE — body overstates test results; 8 VERIFIED / 1 PARTIAL / 1 FAILED**

- **VERIFIED (8):** CLI + `quickstart` install/help; harness picker lists 3 annotated harnesses with correct readiness; all bypass/fail-closed paths (`--harness`/`--yes`/`--json`/non-TTY/cancel/empty/single/import-fallback); gold `#FFD700` `ATV-BENCH` wordmark + 🥇 banner via rich shown once via `~/.atv-bench/.banner_shown_v1` sentinel; banner fail-silent across non-TTY/`--json`/`ATV_BENCH_SKIP_BANNER`/unwritable-home/render-error; `--json`/piped output banner-clean; `rich>=13` + `questionary>=2.0` in base deps, both lazy-imported; scoped harness suite = 9 pass.
- **PARTIAL (1) — banner test count:** body says **9** banner tests; branch ships **11** (all pass). More coverage than claimed; documentation drift only.
- **FAILED (1) — full-suite headline:** body claims **"1049 passed, 17 skipped, 0 failed (zero regressions)."** Real full run in this environment: **1090 passed, 0 skipped, 1 FAILED** (`382s`). The 17 "skipped" do not appear because this env HAS Docker+CodeClash (Docker-gated tests run instead of skip). The single failure — `tests/test_wave_a_games.py::test_wave_a_fake_match_scores[gomoku]` — is a live-Docker CodeClash match test that **passed on two isolated reruns** (~105s each). It is environmental Docker-startup contention flakiness, **not a deterministic PR #23 regression**. The qualitative "no regression" spirit holds, but the literal "1049/17/0, zero regressions" numbers are **not truthful for this environment** and should be corrected in the body.

### On-branch code fix (report-consistency, item a)
PR #23's branch **already contains a real committed code fix**: commit **`0f0597a` "render gold-medal banner once on first run"** from an earlier review pass. This run's E2E did **not** re-find a banner double-print bug **because that defect was already fixed and landed on the branch** — `render_banner()` is now pure and prints exactly once (verified: first-run `printed=True` + sentinel written, second-run `printed=False`). This is **not** a "no code defects ever existed" situation: a defect existed, was fixed on-branch, and is now verified green. Reported accurately here to avoid contradicting the on-branch fix.

#### Explicit RED → GREEN proof for `0f0597a` (codex-flagged: proof was missing from the packet)
The fix ships two regression tests in `tests/test_banner.py` (`test_maybe_show_prints_banner_exactly_once`, `test_render_banner_does_not_emit_to_stdout`). To satisfy the codex demand for an explicit RED→GREEN demonstration (not merely an assertion that the fix is green), the two new tests were replayed against the **parent** `src/atv_bench/banner.py` (`0f0597a~1`, pre-fix) and then against the committed fix:

- **RED (parent `banner.py`, pre-fix):**
  ```
  $ .venv/bin/python -m pytest \
      tests/test_banner.py::test_maybe_show_prints_banner_exactly_once \
      tests/test_banner.py::test_render_banner_does_not_emit_to_stdout -q
  FAILED tests/test_banner.py::test_maybe_show_prints_banner_exactly_once
  FAILED tests/test_banner.py::test_render_banner_does_not_emit_to_stdout
  2 failed in 0.07s
  ```
  Failure captured the exact defect: `render_banner()` emitted the full ATV-BENCH panel to real stdout as a side effect (`redirect_stdout` capture was non-empty; the "Community league for coding-agent" line appeared **twice** on first run).
- **GREEN (committed fix `0f0597a`):**
  ```
  $ .venv/bin/python -m pytest tests/test_banner.py -q
  11 passed in 0.07s
  ```
The one-line fix (`Console(record=True, width=72, file=io.StringIO())`) makes `render_banner()` record-only; `maybe_show_banner()` becomes the single emitter. RED→GREEN is now on the record; the banner-once claim is no longer an unproven assertion in this packet.

### Screenshot evidence
15 committed PNGs under `docs/proof/pr-review-2324/` (commit `0532ea6`, pushed):
`pr23-help.png`, `pr23-deps.png`, `pr23-lazy-imports.png`, `pr23-banner-render.png`, `pr23-banner-gating.png`, `pr23-failsilent.png`, `pr23-json-clean.png`, `pr23-harness-list.png`, `pr23-bypass.png`, `pr23-sentinel.png`, `pr23-scoped-tests.png`, `pr23-quickstart-cli-tests.png`, `pr23-full-suite.png`, `pr23-hermetic-suite.png`, `pr23-banner-double-print-bug.png`.
**PR comment:** https://github.com/All-The-Vibes/ATV-bench/pull/23#issuecomment-5074851214

> **Caption-consistency flag (item b):** `pr23-full-suite.png` shows **1 FAILED / 1090 passed / 0 skipped** and MUST NOT be captioned as a clean "1049 passed / 17 skipped / 0 failed" run. Any caption asserting a green full suite here is a mismatch — the screenshot documents the FAILED claim, which is the honest state.

### Security: **Grade A — 0 critical / 0 high**
One **low** finding: `src/atv_bench/arena/live_server.py` — `LiveMatchServer._run_and_stream` runs operator-supplied bot files via `subprocess` (list argv, no shell) with a 2s per-turn timeout but **without** the `containment.contained_run` boundary (no `unshare -Urn`/RLIMIT caps) used on the league path. Exposure is low: binds loopback `127.0.0.1:0`, inputs are first-party. Recommend routing live bot subprocesses through `contained_run` for non-first-party inputs. CI/league workflows are SHA-pinned, least-privilege, fork-PR scoring uses trusted-base `workflow_run` + fail-closed identity check, `persist-credentials:false`. No injection/exec/pickle/yaml.load/shell=True.

### TDD: no fixes required
`attempted=false`. No real code defect surfaced by this run. The full-suite discrepancy is environmental (Docker present ⇒ tests run not skip) + the flaky @integration Docker test (out of scope, passes on rerun). The suggested deterministic non-Docker unit test for `runner.summarize_tournament`'s winner name→harness remap **already exists and passes** (`test_summarize_tournament_maps_bare_name_back_to_harness_key`, `test_summarize_tournament_passes_through_tie`). Scoped deterministic suite: 26 passed / 0 failed. Tree left untouched. **No new commits/pushes.**

### CI / conflicts / mergeable
**Evidence source (codex-flagged: prior draft asserted "green" without a checkable source).** Verified via `gh pr view 23 --json mergeable,mergeStateStatus,statusCheckRollup` at report time:
- `mergeable: MERGEABLE`, `mergeStateStatus: BLOCKED`.
- Checks: `hermetic` ×2 = **SUCCESS**, `import-smoke` = **SUCCESS**, `pr-path-guard` = **SUCCESS** (a second `pr-path-guard` and `live-integration` = **SKIPPED**, expected on this path). No check with `conclusion: FAILURE`.

`mergeable_state=BLOCKED` reflects **branch protection (REVIEW_REQUIRED)**, not a merge conflict — `mergeable` is `MERGEABLE`, so there are no conflicts.

**Honest caveat (codex-flagged: PR #23 has a real full-suite failure).** GitHub CI is green because the hermetic lane runs `pytest -m 'not integration'` and does **not** execute the Docker-gated CodeClash tests. The **local full-suite** run in this Docker-enabled review environment produced **1090 passed / 0 skipped / 1 FAILED** — `tests/test_wave_a_games.py::test_wave_a_fake_match_scores[gomoku]`. This failure is **not visible in GitHub CI** (that lane skips it) and passed on two isolated reruns (~105s each), consistent with Docker-startup contention flakiness rather than a deterministic PR #23 regression. It is surfaced here rather than hidden: "CI green" is true **for the hermetic lane only**; the full local suite is not clean, and the screenshot `pr23-full-suite.png` documents the FAILED state honestly.

### **GO / NO-GO: 🟢 GO (conditional)**
Merge-ready. **Condition (non-blocking for merge, required for honesty):** correct the PR body's full-suite line to **1090 passed / 0 skipped / 1 flaky-Docker failure (passes on rerun)** and the banner test count to **11**. No code changes needed. Squash-merge to `main` **first** (bottom of stack).

---

## PR #24 — feat/liveview-tdd → feat/quickstart-harness-picker-banner

### Body-accuracy verdict: **MOSTLY ACCURATE — body overstates the full-suite failure count; 9 VERIFIED / 1 FAILED (count claim)**

> **Codex-flagged correction:** an earlier draft graded this body **"ACCURATE"**. That was an overclaim: the body asserts a concrete, checkable number (**4** pre-existing failures) that does **not** match this environment's observed result (**1**). A body that states a false count is not "accurate," even when the surrounding narrative is sound. The verdict is corrected to **MOSTLY ACCURATE**, and the count claim is reclassified from PARTIAL to **FAILED (numbers)** to mirror how the same class of defect is graded on PR #23. The qualitative claim (failures are submodule-environment, not changed code) still holds and is verified; the **numeric** claim is wrong and must be corrected in the body before/at merge.

- **VERIFIED (9):** live mid-run round publishing via daemon poll thread (not post-run replay); results.json read from **inside** the tar (`^\d+/results\.json$`, single-open `read_round`, codex sibling-file bug fixed + regressed with a sibling-free verbatim fixture); traversal-safe bounded `extract_round` (streaming `tar.next()`, `MAX_TAR_MEMBERS=4096`, 64MiB/256MiB caps, rejects absolute/`..`/hardlink/symlink, malformed-tar parsed once via (size,mtime_ns) cache); non-dict results.json handled without crashing the poll loop; all four HTML states (D1 seat-color strip blue=harness/red=control + ghost/pulse, D2 lift+CI+confidence meter, D3 legend, D4 empty) render non-blank under Playwright; cli.py/quickstart.py wiring (match event carries `match_out`+`seats`+`live_url`); scoped suite **93 passed** (exact match).
- **FAILED (1) — full-suite failure count is wrong:** body says **"4 pre-existing failures from uninitialized vendor/CodeClash submodule."** With the submodule initialized in the review worktree, real result: **1 failed / 1150 passed / 20 skipped** (`155s`). The one failure — `tests/test_provisioning.py::test_codeclash_importable` (`ModuleNotFoundError: codeclash`) — is `@pytest.mark.integration`, needs `pip install -e vendor/CodeClash`, and touches **no changed code**. The direction of the claim (submodule-env, not code) is correct and verified; the **count of 4 is not truthful for this environment** (actual **1**) and must be corrected in the body.

### Screenshot evidence
11 committed PNGs under `docs/proof/pr-review-2324/` (commit `236d9f9`, byte-identical, pushed; HEAD == origin, clean tree):
`pr24-cli-help.png`, `pr24-full-suite.png`, `pr24-html-smoke.png`, `pr24-html-states.png`, `pr24-liveview-complete.png`, `pr24-liveview-empty.png`, `pr24-liveview-mid-round.png`, `pr24-scoped-93.png`, `pr24-scoped-tests.png`, `pr24-targeted-tests.png`, `pr24-wiring.png`.
**PR comment:** https://github.com/All-The-Vibes/ATV-bench/pull/24#issuecomment-5074847603

> **Caption-consistency flag (item b):** `pr24-full-suite.png` shows **1 failed / 1150 passed / 20 skipped** — it must be captioned as such (one environmental @integration failure), NOT as a fully green run and NOT as the body's "4 failures." The scoped-suite screenshots (`pr24-scoped-93.png`, `pr24-scoped-tests.png`) correctly show 93 passed.

### Security: **Grade A — 0 critical / 0 high**
Two **low** findings, both server-derived / non-web-controlled values:
- `view/live.html:383` — `emptyLine.innerHTML` string-concats `game` (fixed game enum / CLI name). Prefer `textContent` + static span.
- `view/live.html:416` — `leadLink.innerHTML` embeds `status.leaderboard_url` (app-constructed, passed through `encodeURI`; loopback-scoped). Prefer creating an `<a>`, setting `.href`, validating scheme is http(s).
Tar extraction has full zip-slip/symlink/member-count/byte-cap guards + results.json canonicalization vs winner-spoofing. Both HTTP servers bind loopback `127.0.0.1:0` (no SSRF). All subprocess calls list-form, no `shell=True`.

### TDD: no fixes required
`attempted=false`. No real code defects surfaced by E2E or security. Nothing to fix. No new commits/pushes.

### CI / conflicts / mergeable
**Evidence source (codex-flagged).** Verified via `gh pr view 24 --json mergeable,mergeStateStatus,statusCheckRollup` at report time: `mergeable: MERGEABLE`, `mergeStateStatus: CLEAN`; checks `hermetic` ×2 = **SUCCESS**, `import-smoke` = **SUCCESS**, `pr-path-guard` = **SUCCESS** (second `pr-path-guard` + `live-integration` = **SKIPPED**). No `FAILURE` conclusion. **No conflicts** — this is measured **against its current base (#23's branch)**, which is why `mergeStateStatus` is `CLEAN` here where #23's is `BLOCKED` (review gate). Post-#23-merge, the rebase-onto-main step above is required before this remains cleanly mergeable to `main`; CI must be re-confirmed green after the force-push.

**Honest caveat:** as with #23, the green GitHub lane runs `-m 'not integration'`. The local full suite in this worktree is **1 failed / 1150 passed / 20 skipped** — the single failure is the `@integration` `test_codeclash_importable` (submodule import), not a changed-code regression and not run by GitHub CI.

### **GO / NO-GO: 🟢 GO — after #23 merges and #24 is rebased onto main; body count MUST be corrected first**

Engineering is sound; the body's **narrative** is accurate but its **failure count is wrong (4 → 1)** and must be corrected — it is not a "non-blocking" nicety when the report's own verdict grades a false count as a defect. **Blocking sequencing:** (1) #23 must squash-merge to `main` first; (2) `git rebase --onto origin/main origin/feat/quickstart-harness-picker-banner feat/liveview-tdd`, force-push, re-confirm CI green; (3) then merge. **Also correct the body count "4 → 1"** and gate integration-only tests with `pytest -m 'not integration'` in the hermetic lane so the green-suite claim is deterministic.

---

## Consolidated GO/NO-GO

| PR | Body accuracy | Security | CI | Mergeable | Verdict |
|----|---------------|----------|----|-----------|---------|
| #23 | 8 VERIFIED / 1 PARTIAL / 1 FAILED (full-suite headline wrong) | A, 0C/0H | hermetic lane green; local full-suite 1 flaky-Docker FAIL | yes (blocked=review only) | 🟢 GO — fix body counts |
| #24 | 9 VERIFIED / 1 FAILED (failure count 4→1 wrong) | A, 0C/0H | hermetic lane green; local full-suite 1 @integration FAIL | yes (clean, vs #23 base) | 🟢 GO — fix body count, then #23 merge + rebase-onto-main |

> **CI caveat (applies to both):** "green" means the **hermetic `-m 'not integration'` lane** on GitHub, verified via `gh pr view … --json statusCheckRollup` (no `FAILURE` conclusions; `mergeable: MERGEABLE`). It does **not** mean the full local suite is clean — each PR has exactly one environmental failure (Docker-flaky for #23, submodule `@integration` for #24) that GitHub CI does not execute. Neither touches changed code; both are surfaced honestly above rather than folded into the "green" claim.

**Sequence:** squash-merge **#23 → main**, then `git rebase --onto origin/main origin/feat/quickstart-harness-picker-banner feat/liveview-tdd`, force-push, confirm CI, merge **#24**.
