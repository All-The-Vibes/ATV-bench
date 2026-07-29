# End-to-End PR Review Report — ATV-bench PRs #21 & #22

_Lead reviewer synthesis. Evidence: recon + screenshotted E2E claim verification + atv-security scan._
_Date: 2026-07-22 · Repo: All-The-Vibes/ATV-bench_

---

## PR #21 — Docs-only sync (README / CONTRIBUTING / arenas)

### Body-accuracy verdict: ACCURATE ✅
- **9/9 claims VERIFIED, 0 FAILED, 0 PARTIAL.**
- Genuinely doc-only: commit `9cb1efa` touches exactly `README.md`, `CONTRIBUTING.md`, `docs/arenas.md` (no `.py`, no `uv.lock`, no `pyproject.toml`).
- Verified live: games 20 live / 22 total; 3 live harnesses (claude-code, copilot-cli, codex); README badge 983 passing; `arenas.md` references `docs/proof/wave-c/matrix.json` (20 PASS / 2 upstream-blocked); `plan-schedule` byte-identical under fixed `--seed`; `rate --enforce-gates` fails closed on thin corpus (exit non-zero, G5/G6); `lift` refuses phantom-precision single-cluster CI (exit 2); all 5 proof scripts exist.
- **Minor note (non-blocking):** one stale `_e2e/<arena>/` string survives in `README.md` line 329, but it describes a script OUTPUT path, not the matrix reference — outside the claim's scope. The 983 badge was verified at badge/artifact level, not re-counted via full slow suite.

### Screenshot evidence (docs/proof/pr-review-2126/)
- pr21-doconly.png, pr21-games.png, pr21-harnesses.png, pr21-badge.png, pr21-arenas.png, pr21-determinism.png, pr21-enforce-gates.png, pr21-lift-phantom.png, pr21-scripts.png

### Security: **Grade A** — 0 critical, 0 high, no findings
Authoritative changed surface is **documentation only** (`CONTRIBUTING.md`, `README.md`, `docs/arenas.md` — no `.github/`, no `.vscode`, no `.py`). atv-security config/OWASP/STRIDE are therefore **N/A** — there is nothing executable to flag. Saved artifact: `pr21-atv-security.txt`/`.png`.

### CI / mergeability
- CI reported **green** by live `gh pr checks` at review time: 3 checks pass (hermetic, import-smoke, pr-path-guard); live-integration skipping (expected).
- **No conflicts** (`mergeable:true`) as observed live. `mergeable_state:"blocked"` = branch protection (requires approval), NOT a conflict.
- **Evidence:** CI/mergeability recon is saved as `pr21-recon.txt`/`.png` (`gh api .../pulls/21`, `gh pr checks 21`, `gh api .../pulls/21/files`) and confirms the statements above.
- The local `git diff main...HEAD` showing 112 files is a stale-base artifact (local `main` predates #19/#20); authoritative GitHub API diff is 3 markdown files.

### GO / NO-GO: **GO** ✅ — no blocking issues.

---

## PR #22 — Quickstart one-command harness eval UX (large feature)

### Body-accuracy verdict: ACCURATE WITH CAVEATS (1 PARTIAL registration-only, 1 PARTIAL test-count) ⚠️
- **9/11 claims VERIFIED, 2 PARTIAL, 0 FAILED.**
- Verified live: `quickstart` command + help/UX; harness fingerprint inference; non-interactive `--model/--yes/--json` path emitting machine-readable QuickstartResult; default 3-game trio (lightcycles/chess/ants) vs `--all` = 20 live games (`live_keys()==20`); G5/G6 credibility gates FAIL CLOSED on thin corpus (PROVISIONAL, gate.passed=false); `models_with_current()` prepends configured model as picker default; `questionary>=2.0` runtime dep; self-contained scorecard.html (0 external resources) + scorecard-example.png present. 37 scoring/lift unit tests green.
- **PARTIAL — `bare:codex` runnable claim:** `CodexCliAdapter` is **registered** and `bare:codex` **resolves** to a `BareModelAdapter` (registration/resolution verified). However, the live end-to-end match **failed to execute**: the CodeClash runner rejects the `bare:codex` branch name (colon is invalid in git refs, exit 128). The PR body's "works / runnable" framing is therefore only **partially** substantiated — the adapter is wired in but a live `bare:codex` eval **cannot currently run end-to-end**. This directly contradicts a plain reading of the "runnable" claim and is downgraded from the prior "non-blocking follow-up" treatment. The fail-closed gates correctly refused to emit a score, but that does not make the execution path functional. See BLOCKING follow-up below.
- **PARTIAL — test-count claim:** PR body says "1020 passed, 0 failures"; reviewer observed **1040 passed / 4 failed / 20 skipped**. All 4 failures were **environmental** (CodeClash submodule uninitialized in the fresh worktree). After `git submodule update --init vendor/CodeClash` + `uv pip install -e '.[run]'`, those exact 4 tests pass. **Code is green**; only the specific integer 1020 does not match the 1040 observed (suite has grown/differs). Not a correctness defect.

> **Evidence-base note (RESOLVED via authoritative GitHub API diff):** Earlier drafts cited PR #22 as "123 files / +16k" from the **local `git diff main...HEAD`**, whose local `main` predates #19/#20 (stale base). Now reconciled against the authoritative GitHub API diff, saved as artifacts `pr21-recon.txt`/`.png` and `pr22-recon.txt`/`.png`:
> - **PR #21:** 3 files (CONTRIBUTING.md, README.md, docs/arenas.md), +66/−8. mergeable:true, mergeable_state:blocked (branch protection). CI: hermetic/import-smoke/pr-path-guard pass; live-integration skipping.
> - **PR #22:** **20 files** (API `changed_files`); authoritative `/pulls/22/files` lists src/ + tests/ + docs — `vendor/CodeClash` does **not** appear as a PR #22 addition. The "123 files/+16k" figure is a stale-base artifact and is **withdrawn**. CI green, mergeable:true, mergeable_state:blocked.
> Recon commands are now persisted as command-output artifacts, addressing the reviewer's "recon not saved" finding.

### Screenshot evidence (docs/proof/pr-review-2126/)
- pr22-quickstart-help.png, pr22-harnesses.png, pr22-deps-adapter.png, pr22-quickstart-run.png, pr22-gates-models.png, pr22-games-selection.png, pr22-20-games.png, pr22-codex-adapter.png, pr22-models-default.png, pr22-tests-quickstart.png, pr22-fullsuite.png, pr22-retest-recover.png, pr22-fingerprint.png, pr22-failclosed.png, pr22-scoring-tests.png, pr22-scorecard-html.png

### Security: **Grade A** — 0 critical, 0 high, no findings
No agentic surfaces (config N/A). OWASP: subprocess list-arg form (no `shell=True`), `yaml.safe_load`, live server binds 127.0.0.1, frontend dynamic values routed through `esc()`/`safeHref` allowlist (blocks `javascript:`), `live.html` uses `textContent`. STRIDE: untrusted bots confined by docker `--network none --read-only --user 65534 --memory 512m --pids-limit 128`, no `--privileged`/host mounts/docker.sock. Saved artifact: `pr22-atv-security.txt`/`.png`.

### CI / mergeability
- CI reported **green** by `gh pr checks` at review time: hermetic (x2), import-smoke, pr-path-guard pass; live-integration + one pr-path-guard skipping (expected).
- **Evidence:** CI/mergeability recon is saved as `pr22-recon.txt`/`.png` (`gh api .../pulls/22` → `mergeable:true, mergeable_state:blocked`; `gh pr checks 22` → all non-skipped checks pass; `gh api .../pulls/22/files` → 20 files). The atv-security scan is saved as `pr22-atv-security.txt`/`.png`. All CI-green / no-conflict / Grade-A statements are independently re-verifiable from these saved artifacts.
- `mergeable_state:"blocked"` = branch protection (requires approval), not a conflict (as observed live).
- **Submodule claim caveat (stale base):** the `vendor/CodeClash` git submodule appears as an addition **only in the stale-base local `git diff main...HEAD`**. The authoritative `origin/main...HEAD` diff is much smaller and does **not** show `vendor/CodeClash`/`.gitmodules` as a PR #22 addition — the submodule almost certainly already exists on current `main` (introduced by an earlier merged PR), so attributing it to PR #22 is **overclaimed and unverified**. Treat "PR #22 adds `vendor/CodeClash`" as **not evidence-backed**; do not gate the merge on it. The 4 local test failures were a fresh-worktree submodule-init artifact regardless of which PR owns the submodule; CI initialization of any existing submodule pin was **inferred**, not directly confirmed from saved artifacts.

### GO / NO-GO: **CONDITIONAL GO** ⚠️
Blocking follow-up: (1) **`bare:codex` cannot run a live end-to-end eval** — the CodeClash runner rejects the colon in the branch name (exit 128). Adapter names must be sanitized into ref-safe branch names before the `bare:codex` path can be considered runnable end-to-end. This contradicts a plain reading of the PR body's "runnable" claim and should be resolved (or the claim scoped to "registered/resolvable") before or immediately after merge.
Non-blocking follow-ups: (2) reconcile the "1020" figure in the PR body with the current suite count (1040 observed). CI-green and mergeability are backed by the saved `pr22-recon.txt`/`.png` recon artifacts; optionally re-run `gh pr checks 22` immediately before merge as a freshness check.

---

## MERGE ORDER RECOMMENDATION

**Merge PR #22 (feature) FIRST, then PR #21 (docs).**

Justification:
- **No hard code dependency either direction** — both are independently mergeable and both are CI-green with no conflicts.
- **PR #21 documents the state of the repo** (games/harnesses/badge/arenas) that PR #22's feature work contributes to. Merging the large feature PR first means the docs in #21 describe the actual merged `main`, avoiding a documentation/reality gap between the two merges.
- **Conflict-surface asymmetry (authoritative diff):** per the saved recon artifacts, PR #22 = **20 files** touching `README.md`, `pyproject.toml`, `src/atv_bench/*.py` (9), `tests/*.py` (7), and 2 docs; PR #21 = **3 files** (`README.md`, `CONTRIBUTING.md`, `docs/arenas.md`). **The only overlapping file between the two PRs is `README.md`.** (PR #22 does **not** touch `docs/arenas.md` or `uv.lock` — earlier drafts said so from a stale-base local diff; that is withdrawn.) Whichever PR merges second must absorb the other's `README.md` edits. It is far cheaper to rebase the 3-file docs PR (#21) onto a `main` that already contains the 20-file feature PR (#22) than the reverse, so **merging #22 first minimizes rebase cost**. This rests on the confirmed single-file (`README.md`) overlap, not on any unverified size figure.
- After #22 lands, re-verify #21's doc claims still match `main` (they should — #21 was authored to describe this exact feature set) and resolve any trivial README/arenas overlap before merging #21.

If policy requires the smallest/lowest-risk change first, #21-then-#22 is acceptable — but then expect a mechanical **`README.md`** rebase on PR #22 (the only overlapping file).

---

## FINAL VERDICT

| PR | Type | Body Accuracy | Security | CI | Conflicts | Decision |
|----|------|---------------|----------|----|-----------|----------|
| #22 | Feature (20 files) | 9 VERIFIED / 2 PARTIAL / 0 FAILED | A (0C/0H) | Green | None | **CONDITIONAL GO** |
| #21 | Docs-only (3 files) | 9 VERIFIED / 0 FAILED | A (0C/0H) | Green | None | **GO** |

**Recommended merge order: #22 → #21** (rationale robust to stale-base file counts; overlap = `README.md` only, per saved recon). PR #21 is GO; PR #22 is CONDITIONAL GO pending the `bare:codex` ref-safe-name fix (live end-to-end eval currently fails, exit 128). CI-green and mergeability for both PRs are backed by the saved `pr21-recon.txt`/`pr22-recon.txt` artifacts.

---

## SANTA-LOOP VERIFICATION (dual independent review)

**Final verdict: NICE** ✅ — both reviewers PASS, 0 critical issues.

| Reviewer | Model | Final Verdict |
|----------|-------|---------------|
| A | Claude Opus (pr-review-toolkit:code-reviewer, context-isolated) | **PASS** (7/7 criteria) |
| B | **codex** (`codex exec --sandbox read-only`, external model) | **PASS** (7/7 criteria) |

Codex (Reviewer B) performed the required independent verification, `cat`-confirming every saved artifact and cross-checking file counts against the live GitHub API. The loop ran adversarially: codex returned FAIL on earlier rounds, correctly catching (1) the `bare:codex` runnable overclaim, (2) unsaved recon/security artifacts, and (3) a stale "123 files" figure + `arenas.md`/`uv.lock` overlap error. All were fixed and re-verified to convergence.

### Evidence artifacts (docs/proof/pr-review-2126/)
- **E2E screenshots (25):** pr21-*.png (9), pr22-*.png (16)
- **Recon (CI + mergeability + authoritative file list):** pr21-recon.txt/png, pr22-recon.txt/png
- **atv-security scans:** pr21-atv-security.txt/png, pr22-atv-security.txt/png
