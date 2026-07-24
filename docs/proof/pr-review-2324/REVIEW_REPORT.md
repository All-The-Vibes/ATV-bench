# PR Review Report — #23 & #24 (ATV-bench)

Lead-reviewer synthesis of agent-team evidence (recon → screenshotted E2E claim verification → security audit → TDD fixes → proof publication). Handoff for independent reviewers.

> **Caveat — published-proof correction (2026-07-24):** The originally published PR proof comments miscaptioned the `pr23-full-suite` and `pr24-full-suite` screenshots as "Full test suite passes" / "complete suite passing." That was inaccurate: the **unfiltered** suites are **1088 passed / 1 failed** (#23) and **1145 passed / 6 failed / 20 skipped** (#24). Every failure is environmental/vendored (Docker arena image, uninstalled `codeclash`, or the vendored CodeClash `arena.py:266` bug) and touches **none** of the PRs' changed files; the hermetic lanes CI actually runs are fully green (1071 / 1136 passed). Both PR comments have been corrected to match. This report body already carried the honest counts (see the PARTIAL body-accuracy verdicts below); only the screenshot captions were wrong.

**Repo:** All-The-Vibes/ATV-bench
**PRs:** #23 `feat/quickstart-harness-picker-banner` → `main`; #24 `feat/liveview-tdd` → `feat/quickstart-harness-picker-banner`
**These PRs are STACKED.** See Merge Order.

---

## PR #23 — Quickstart harness picker + gold-medal banner

### Body-accuracy verdict: **PARTIAL** (9 VERIFIED / 1 PARTIAL / 0 FAILED of 10 claims)
- **VERIFIED (9):** CLI `--help` + `quickstart`; 3-harness annotated picker; picker bypass/fail-closed; gold `#FFD700` banner + �� + once-only sentinel; fail-silent on non-TTY/--json/env/unwritable/render-error; `--json` banner-free; `rich>=13` + `questionary` base deps, both lazy-imported; `test_harness_selection.py` 9 pass; `test_banner.py` 9 pass.
- **PARTIAL (1) — full-suite counts:** Body claims *"1049 passed, 17 skipped, 0 failed."* Does **not** reproduce. Real hermetic subset (`-m 'not integration and not live and not e2e and not spike and not drift'`) = **1071 passed / 0 failed**. Bare `uv run pytest -q` = **1088 passed / 1 failed**, the one failure being a Docker `@integration` gomoku test failing on a **vendored CodeClash** bug (`'tuple' has no attribute 'name'` at vendor/CodeClash/.../arena.py:266) — **not PR #23 code**. Zero PR#23 regressions holds; the exact body numbers are wrong. → `overall_body_accurate = false`.

### Screenshot evidence (committed, `docs/proof/pr-review-2324/`)
15 PNGs: pr23-help, -harness-list, -bypass, -banner-render, -banner-gating, -failsilent, -json-clean, -lazy-imports, -deps, -scoped-tests, -quickstart-cli-tests, -hermetic-suite, -full-suite, -sentinel, -banner-double-print-bug. Committed 156b44d, pushed.
**PR comment:** https://github.com/All-The-Vibes/ATV-bench/pull/23#issuecomment-5074365102

### Security: **Grade A** — Critical 0 / High 0 / Medium 2 (same root cause)
- A04 / STRIDE-D (medium): `arena/live_server.py` `/events` spawns a bot-subprocess pair + runs a full match per connection on a `ThreadingHTTPServer` with no concurrency cap / rate limit → local DoS. **Contained** by `127.0.0.1:0` localhost-only bind. Recommend a max-concurrent-match semaphore / single-flight guard. Non-blocking.
- No shell=True, no eval/pickle/yaml.load, no hardcoded secrets, no SSRF; workflows injection-safe (env-passed untrusted values, SHA-pinned, `persist-credentials:false`).

### TDD: 1 real bug found, fixed, pushed
- **Bug (medium):** Gold-medal banner **printed twice** on first real-TTY run — `render_banner()` built `rich Console(record=True)` with no `file=`, so `console.print(panel)` emitted to stdout *and* recorded; `maybe_show_banner()` then printed the recorded copy again (and `stream=` was ignored).
- **Fix:** passed `file=io.StringIO()` to the recording Console. RED tests added (`test_maybe_show_prints_banner_exactly_once`, `test_render_banner_does_not_emit_to_stdout`) → GREEN.
- **Commit:** `0f0597a` fix(quickstart): render gold-medal banner once on first run — **pushed**.
- **Suite after:** 47 passed (banner/quickstart_cli/quickstart_engine/harness_selection/interactive_select).

### CI / mergeability
- CI **green** (hermetic x2, import-smoke, pr-path-guard pass; live-integration skipping — acceptable).
- **No conflicts.** `mergeable:true`. `mergeable_state:"blocked"` = branch-protection REVIEW_REQUIRED, not a conflict.

### **Verdict: GO** — no blocking issues. Recommend correcting the PR body's hermetic count (1071, not 1049) as a non-blocking follow-up.

---

## PR #24 — Live round-by-round view (TDD)

### Body-accuracy verdict: **PARTIAL** (9 VERIFIED / 1 PARTIAL / 0 FAILED of 10 claims)
- **VERIFIED (9):** mid-run daemon poll loop (real animation, not post-run replay); watcher reads in-tar `<N>/results.json` (codex-caught sibling bug truly fixed + tested); traversal-safe bounded `extract_round` (4096 members / 64 MiB member / 256 MiB total, rejects abs paths/`..`/hard+symlinks; reparse-storm cache); non-dict results.json handled without crash; four HTML states render non-blank (empty/mid/complete + legend); D1 seat-color chip semantics; D2 lift+CI+confidence / D3 legend / D4 empty; cli.py/quickstart.py wiring (match_out+seats+live_url, gated off under --yes/--json/no-TTY); **93 scoped tests pass** in one clean Chromium run.
- **PARTIAL (1) — full-suite failure count & cause:** Body claims *"4 pre-existing failures from the uninitialized vendor/CodeClash submodule."* Actual: **6 failed / 1145 passed / 20 skipped**. Submodule **is** initialized (f0694c6 checked out). Real causes: 5× `test_containment.py` (need a built Docker arena image → empty subprocess stdout) + 1× `test_provisioning.py::test_codeclash_importable` (`codeclash` package not pip-installed). **None touch PR #24's changed files** → non-blocking, but count (6≠4) and root-cause are both wrong. → `overall_body_accurate = false`.

### Screenshot evidence (committed, `docs/proof/pr-review-2324/`)
8 PNGs: pr24-scoped-93, -scoped-tests, -html-smoke, -liveview-empty, -liveview-mid-round, -liveview-complete, -cli-help, -full-suite. Committed 632a73e, pushed.
**PR comment:** https://github.com/All-The-Vibes/ATV-bench/pull/24#issuecomment-5074362672

### Security: **Grade A** — Critical 0 / High 0 / Medium 0 — no findings
- `frames.py` tar extraction mitigates zip-slip/tar-DoS (streams via `tar.next()`, per-member/total/count caps, read-size bounding, rejects abs/`..`/links). `live_server.py` + `liveview.py` bind `127.0.0.1:0` (no external/SSRF surface). Subprocess argv-list only, no shell=True; untrusted bot stderr discarded. HTML uses `textContent`; no pickle/yaml.load/eval; no secrets.

### TDD: no code defect → tree untouched (`attempted:false`)
- Two flagged items are **environmental/doc-accuracy, not code bugs**:
  1. Full-suite count/cause misstatement (above) — correct the PR body.
  2. **(medium signal)** Shipped review venv omitted `pytest/chess/playwright/pyyaml`; bare `uv run pytest` silently fell back to system Python 3.12 and produced 11 spurious failures. Notable because **`chess>=1.9` is a NEW hard runtime dependency** this PR adds. After `uv sync --extra dev`, `import chess` (1.11.2) works and the hermetic lane `-m "not live and not integration"` = **1136 passed / 17 skipped / 18 deselected** (fully green — the exact selection CI runs).
- No RED test warranted; no commit/push.
- **Suite after (unchanged tree):** hermetic 1136 passed; unfiltered 1 failure = `test_codeclash_importable` (integration-marked, submodule/extra condition, not a code defect).

### CI / mergeability
- CI **green** (hermetic, import-smoke, pr-path-guard pass; live-integration + dup pr-path-guard skipping).
- **No conflicts.** `mergeable:true`, `mergeable_state:"clean"`.

### **Verdict: GO** (after ancestry-preserving merge or rebase) — no blocking code issues. Blocking prerequisite is *sequencing*, not defect: #24 targets #23's branch and must be brought onto `main` after #23 lands **via a merge commit (Path A) or a rebase (Path B) — not a squash+retarget**, then have CI and conflict checks rerun (see Merge Order). Recommend correcting the PR body to "6 environmental failures (5 need a Docker arena image, 1 needs the codeclash package), none touching changed files."

---

## MERGE ORDER (mandatory — STACKED PRs)

**#23 first, then #24.**

- **Dependency rationale:** #24's base branch is `feat/quickstart-harness-picker-banner` (**#23's head**), not `main`. #24's diff (+3979/-211) is measured against #23, so #24 cannot merge to `main` while #23 is unmerged without dragging in / double-counting #23's work.

- **⚠️ Retarget-after-squash caveat (codex-flagged, corrected 2026-07-24):** The earlier guidance — "merge #23, then merely retarget #24 to `main`" — is **unsafe if #23 is squash-merged**. A squash merge collapses #23's commits into one **new** commit on `main` whose SHA/ancestry does **not** match the `feat/quickstart-harness-picker-banner` commits #24 is built on. After a squash merge, GitHub's auto-retarget recomputes #24's diff against `main` and re-surfaces #23's already-landed changes as **phantom additions and near-certain conflicts** (every line #23 touched now appears twice from git's perspective). Retargeting alone does **not** fix this — you must preserve ancestry or rebase.

- **Safe sequence — pick ONE path:**
  - **Path A — merge-commit (preserves ancestry):** Merge **#23 → main using a real merge commit** (NOT squash, NOT rebase-merge). Then retarget **#24 → main**; because #23's original commit SHAs are now reachable from `main`, git sees #24's diff as #24-only with no phantom conflicts. **Rerun CI and re-verify `mergeable`/no-conflicts on #24** before merging it.
  - **Path B — rebase #24 onto post-merge main:** If #23 is squash- or rebase-merged, do **not** rely on retarget alone. After #23 lands, rebase/update #24 onto the new `main` (`git rebase --onto main feat/quickstart-harness-picker-banner feat/liveview-tdd`, dropping the now-duplicated #23 commits), force-push #24, retarget its base to `main`, then **rerun CI and the conflict check** on the recomputed branch before merging.
  - In both paths treat "green + no conflicts" as a fact to **re-establish after #23 lands**, not one inherited from the pre-merge state below.

- Do **not** merge #24 first — its base does not exist on `main` yet.
- Do **not** squash-merge #23 and then merge #24 by retarget alone — that is the exact failure codex flagged.

## Per-PR GO / NO-GO
| PR | Verdict | Body accurate | Security | CI | Blocking issues |
|----|---------|---------------|----------|----|-----------------|
| #23 | **GO** (merge 1st) | PARTIAL (9/1/0) — hermetic count wrong (1071 real) | A · 0C/0H/2M | green, no conflicts | none (1 medium bug fixed+pushed `0f0597a`) |
| #24 | **GO** (merge 2nd, via merge-commit or rebase onto main — not squash+retarget) | PARTIAL (9/1/0) — 6 fails not 4, cause misstated | A · 0C/0H/0M | green pre-merge; **must rerun after #23 lands** | none code; sequencing prerequisite only |

**Non-blocking follow-ups:** (1) fix #23 body hermetic count; (2) fix #24 body full-suite count+cause; (3) #23 live_server `/events` concurrency cap; (4) provisioning smoke test asserting `import chess` + correct venv interpreter so env drift fails loudly.

> **Merge-mechanics correction (codex, 2026-07-24):** Do not land #24 by squash-merging #23 and then retargeting #24 to `main`. A squash merge rewrites #23's ancestry, so a bare retarget re-surfaces #23's diff as phantom conflicts. Land #24 via **Path A (merge commit for #23, then retarget)** or **Path B (rebase #24 onto post-merge `main`)**, and **rerun CI + conflict checks** on the recomputed #24 before merging. See Merge Order.
