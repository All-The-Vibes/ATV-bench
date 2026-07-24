# PR Review Report — #23 & #24 (ATV-bench)

Lead-reviewer synthesis of agent-team evidence (recon · screenshotted E2E · atv-security · TDD · proof).
Repo: All-The-Vibes/ATV-bench · Date: 2026-07-24

---

## Merge Order (STACKED — read first)

These PRs are **stacked**:

- **#23** `feat/quickstart-harness-picker-banner` → base `main`
- **#24** `feat/liveview-tdd` → base **`feat/quickstart-harness-picker-banner`** (#23's head)

**#24 cannot merge to `main` before #23.** Its diff (+3979/-211) is measured against #23's branch and depends on #23's modules (`quickstart.py`, `runner.py`, `live_server.py`, `codeclash_env.py`, banner/harness scaffolding).

**Required sequence:**
1. Merge **#23 → `main`** first.
2. **Preserve #24's ancestry before retargeting.** ⚠️ #24's true merge-base with #23 is the shared commit `9c97555`, **not** #23's head. If #23 is **squash-merged** (or rebased) into `main`, its commits get **new SHAs** and #24 is left with the old #23 commits as unmerged ancestors — GitHub will recompute #24's diff to re-include all of #23's changes, and a plain retarget will show a bloated/conflicting diff. To avoid this, **rebase #24 onto the new `main` head** (`git rebase --onto main <old-#23-head> feat/liveview-tdd`) after #23 lands, dropping the now-duplicated #23 commits, then force-push. Only if #23 is merged with a **true merge commit** (ancestry preserved) can you retarget #24 to `main` without a rebase.
3. **Retarget #24's base to `main`**, let GitHub recompute the diff, re-confirm CI green.
4. Merge **#24 → `main`**.

Note: #24 does **not** contain #23's banner-double-print fix `0f0597a` (verified: not an ancestor of `feat/liveview-tdd`). The fix reaches `main` via #23; ensure the post-rebase #24 build still passes with `main`'s banner behavior.

Merging #24 first, or before retargeting, would either be blocked or pull #23's whole diff in under #24's banner.

---

## PR #23 — Quickstart harness picker + first-run banner

### Body accuracy: ⚠️ INACCURATE (8 VERIFIED / 1 PARTIAL / 1 FAILED of 10)

- **Claim 9 (PARTIAL):** body says `test_banner.py` has **9** tests; actual **11** (all pass). More coverage than claimed — documentation drift only.
- **Claim 10 (FAILED):** body says full hermetic suite = **1049 passed / 17 skipped / 0 failed (zero regressions)**. Real run in-env = **1090 passed / 0 skipped / 1 FAILED** in 382s. The single failure — `tests/test_wave_a_games.py::test_wave_a_fake_match_scores[gomoku]` — is a **flaky live-Docker CodeClash match** that **passed on two isolated reruns** (105s each). Environmental flakiness, not a deterministic PR #23 regression, but the body's "zero regressions / 1049 / 17 / 0" framing does **not** reproduce in this environment.
- Claims 1–8 fully VERIFIED: CLI+`quickstart` help, 3-harness annotated picker with readiness states, all bypass/fail-closed paths, gold `#FFD700` 🥇 banner shown-once via `~/.atv-bench/.banner_shown_v1` sentinel, fail-silent across non-TTY/`--json`/env/unwritable-home/render-error, banner-clean `--json`, `rich>=13`+`questionary` in base deps both lazy-imported, harness suite 9/9.

### Resolved code defect (banner double-print) — commit `0f0597a`
Correction to an earlier synthesis that stated "No banner double-print bug": there **was** a real defect, and it is **fixed in this PR**. `render_banner()` built a rich `Console(record=True)` with **no file**, so `console.print(panel)` emitted the panel to real stdout as a side effect; `maybe_show_banner()` then printed the recorded copy **again** — the banner rendered **twice** on first run and the `stream=` redirect was ignored. Commit `0f0597a` (`src/atv_bench/banner.py`) gives the Console an in-memory `io.StringIO` file so it only records, leaving `maybe_show_banner()` as the single emitter. A RED→GREEN regression was added in `tests/test_banner.py` (+27 lines) asserting single emission / respected redirect. Verified: `pytest tests/test_banner.py` → **11 passed**. This is the reason banner-count coverage is 11 not the 9 claimed in the body.

### Screenshot evidence
15 PNGs committed to `feat/quickstart-harness-picker-banner` (commit `0532ea6`, pr23-only staged) under `docs/proof/pr-review-2324/`:
`pr23-help.png, pr23-deps.png, pr23-lazy-imports.png, pr23-banner-render.png, pr23-banner-gating.png, pr23-failsilent.png, pr23-json-clean.png, pr23-harness-list.png, pr23-bypass.png, pr23-sentinel.png, pr23-scoped-tests.png, pr23-quickstart-cli-tests.png, pr23-full-suite.png, pr23-hermetic-suite.png, pr23-banner-double-print-bug.png`
PR comment: https://github.com/All-The-Vibes/ATV-bench/pull/23#issuecomment-5074851214

### Security: **Grade A** — 0 critical / 0 high
Net-positive posture: adds a containment boundary (netns egress-deny + rlimits + ephemeral creds + per-run HOME isolation) around untrusted adapter subprocesses. Argv-list subprocess (no `shell=True`), `shutil.which` detection, `createElement`/`textContent` HTML (no XSS sink), `mkstemp`+0400+unlink creds, loopback-ephemeral demo server, least-privilege workflows. PR #24's tar surface is not in this PR.
- **1 LOW (informational):** `live_server.py` ThreadingHTTPServer has no per-connection cap — but binds `127.0.0.1:0` loopback, daemon, run-once. Negligible.

### TDD
- `attempted=false`, `resolved=[]`, nothing pushed. The sole E2E failure was triaged as **environmental**, not a code defect.
- Key point: the flag's own proposed remediation (a non-Docker unit test for `runner.summarize_tournament`'s winner name→harness remap) **already ships** in this PR — `test_runner.py::test_summarize_tournament_maps_bare_name_back_to_harness_key` + tie passthrough — both pass. Winner-attribution logic is already decoupled from the flaky arena test.
- Suite after (scoped): 55 passed; `test_runner.py` alone 12 passed.

### CI / mergeability
Green — hermetic (x2), import-smoke, pr-path-guard all pass; live-integration + one pr-path-guard "skipping" (acceptable). `mergeable:true`, no conflicts. `mergeable_state:"blocked"` = branch protection (REVIEW_REQUIRED), not a conflict.

### Verdict: ✅ **GO** (with a non-blocking doc fix)
One real code defect (banner double-print) was found **and already fixed in-PR** by `0f0597a` with a RED→GREEN regression test (11 banner tests pass); security-clean, CI green. **Non-blocking:** correct the PR body's test-plan counts — banner tests 9→11 (the +2 are the double-print regression) and the "1049/17/0 zero regressions" line, which does not reproduce (real: 1090/0/1, the 1 being a flaky Docker test green on rerun). Recommended (non-blocking) hardening: serialize or bounded-retry the flaky Docker arena parametrization to stop container-contention flakes.

---

## PR #24 — Live round-by-round gameplay view

### Body accuracy: ⚠️ MOSTLY ACCURATE (9 VERIFIED / 1 PARTIAL of 10)

- **Claim 10 (PARTIAL — suite-count mismatch):** body says the full suite has **4** pre-existing failures from an uninitialized `vendor/CodeClash` submodule. With the submodule initialized in the review worktree, the real run = **1 failed / 1150 passed / 20 skipped** in 156s. The stated count of **4** does **not** reproduce (real: **1**), so the numeric claim is inaccurate/stale-pessimistic. The one remaining failure `test_provisioning.py::test_codeclash_importable` is `@pytest.mark.integration`, needs `pip install -e vendor/CodeClash`, touches no changed code — genuinely environmental. The **qualitative** claim (failures are pre-existing/environmental, not PR #24 regressions) holds; only the **count** is wrong.
- Claims 1–9 VERIFIED with strong evidence: mid-run daemon-poll live publish (not batch replay), in-tar `^\d+/results\.json$` fix + sibling-free regression test, traversal-safe bounded `extract_round` (streaming member cap 4096, 64MiB/256MiB budgets, path/link rejection, once-only malformed-tar failure cache), non-dict `results.json` guard, all four HTML states (D1 seat-color strip ghost/pulse, D2 lift+CI+confidence meter, D3 legend, D4 empty) render non-blank under Playwright, cli/quickstart wiring with `match_out`+seats+`live_url`, scoped suite exactly 93 passed.

### Screenshot evidence
11 PNGs committed to `feat/liveview-tdd` (commit `236d9f9`, rebased + pushed) under `docs/proof/pr-review-2324/`:
`pr24-cli-help.png, pr24-full-suite.png, pr24-html-smoke.png, pr24-html-states.png, pr24-liveview-complete.png, pr24-liveview-empty.png, pr24-liveview-mid-round.png, pr24-scoped-93.png, pr24-scoped-tests.png, pr24-targeted-tests.png, pr24-wiring.png`
PR comment: https://github.com/All-The-Vibes/ATV-bench/pull/24#issuecomment-5074847603

### Security: **Grade A** — 0 critical / 0 high
Tar extraction traversal-safe with explicit zip-bomb/OOM caps; both local servers + CLI viewer bind `127.0.0.1:0` loopback (no SSRF/external bind); untrusted data via `textContent`/`createElement`, `html.escape`, `innerHTML` only on static/`encodeURI` strings; list-form subprocess, no secrets, no `.github/` config touched.
- **3 LOW (all loopback-gated):** SimpleHTTPRequestHandler directory listing; per-request match spawn without rate limit; unauthenticated local exposure. All mitigated by loopback+ephemeral+short-lived. Optional: fixed handler serving only known files.

### TDD
No real code defects surfaced by E2E or security. `attempted=false`, nothing to fix, nothing pushed.

### CI / mergeability
Green — hermetic, import-smoke, pr-path-guard pass; live-integration + duplicate pr-path-guard "skipping". `mergeable:true`, `mergeable_state:"clean"`, no conflicts.

### Verdict: ✅ **GO** (after #23 merges + ancestry-preserving retarget/rebase to `main`)
No code defects, security-clean, CI green. **Blocking on ordering only:** must merge **after** #23. If #23 lands via **squash/rebase** (new SHAs), **rebase #24 onto `main`** (`git rebase --onto main <old-#23-head> feat/liveview-tdd`, force-push) before retargeting; only a true merge-commit of #23 permits a plain retarget. **Non-blocking:** reconcile the body's "4 failures" — the only failure is the `@integration` `test_codeclash_importable` (needs `pip install -e vendor/CodeClash`); optionally assert `pytest -m 'not integration'` is 0-fail on a submodule-initialized checkout to make the green-suite claim deterministic.

---

## Bottom line
- **#23: GO** — merge to `main` first. One real defect (banner double-print) was caught and **already fixed in-PR** (`0f0597a` + regression test). Fix body test counts (non-blocking).
- **#24: GO** — merge second. Body is **mostly accurate** but overstates the failure count (says 4, real is 1); reconcile before/after merge (non-blocking). If #23 is **squash-merged**, **rebase #24 onto the new `main`** (ancestry is not preserved by squash) before retargeting — a plain retarget will re-pull #23's diff.
- Both security Grade A (0 critical / 0 high). The one banner defect in #23 is fixed; all remaining flagged suite failures are environmental (flaky Docker / uninitialized-submodule integration test), not regressions.
