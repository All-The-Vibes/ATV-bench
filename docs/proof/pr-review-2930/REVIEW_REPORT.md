# Consolidated Review — PR #29 and PR #30

Lead reviewer synthesis. Every assertion below cites a screenshot in
`docs/proof/pr-review-2930/`, a command output, or a `gh` query. Verdicts marked
VERIFIED/FAILED come from the E2E agents actually executing the verification commands,
not from reading the PR bodies.

---

## 1. Executive summary

### PR #29 — `fix(cli): survive Windows cp1252 consoles instead of crashing on status marks`
Branch `worktree-fix-windows-cp1252-encoding` → `main`

**Verdict: GO — merge first, but only after the PR body is corrected. Seven body claims FAILED.**

The engineering is sound and the defect is real. The root cause reproduces exactly as
described (C1, VERIFIED, `pr29-c1.png`): under `PYTHONIOENCODING=cp1252:strict
PYTHONUTF8=0`, `origin/main` dies with `UnicodeEncodeError: 'charmap' codec can't encode
character '✗'`. All five named commands crash pre-fix and exit 0 post-fix (C17, VERIFIED).
The CI gate is empirically non-vacuous: pre-fix tree → gate exit 1 with real crash
signatures, post-fix → exit 0 (C18, VERIFIED, `pr29-c18.png`).

**But the PR body overstates its own verification in seven places, and this is a defect
worth blocking a body-correction commit on:**

1. **C4 FAILED — the headline "each layer is independently load-bearing" claim is false
   for layer 1.** The body states in bold that "removing either one reintroduces a real
   user-visible defect" and lists "Mutation check: disabling either layer makes the suite
   fail again" as performed verification. Disabling `reconfigure(errors="replace")`
   yields **36 passed, 0 failed**, and all five commands exit 0 under cp1252:strict
   (`pr29-c4.png`). Layer 2 *is* load-bearing (C5, VERIFIED, 3 failures). Layer 1 may
   still be defensible defence-in-depth, but the stated mutation check did not happen as
   described.
2. **C19 FAILED — the "2000-case fuzz" proving `errors="replace"` lossless does not exist
   in the repo.** `grep '2000\|fuzz\|hypothesis'` in the encoding tests → no matches;
   nothing in `git diff origin/main...HEAD -- tests/` either (`pr29-c19.png`). The claim
   rests entirely on the author's word and cannot be re-run by CI.
3. **C16 FAILED — "1178 passed, 20 skipped, 1 environmental failure" is actually
   4 failed, 1185 passed, 20 skipped** (`pr29-c16.png`). All four failures do trace to
   the uninitialised `vendor/CodeClash` submodule, so the *environmental* framing is
   right — but the count understates by 3.
4. **C13 FAILED — the described CI gate is stale.** The body says the gate "treats only
   exit >= 2 or a traceback as a failure." The shipped workflow has no exit-code threshold
   at all; commit `814d6db` replaced it with `.github/scripts/assert_cp1252_output.py`
   (`pr29-c13.png`). The body was never updated.
5. **C3 FAILED — "a fix for this existed on PR #16" is wrong.** PR #16 is
   `feat: add credible harness benchmark v1` (CLOSED, `mergedAt: null`) and contains zero
   encoding content (`pr29-c3.png`). The "shipped to users twice" narrative that justifies
   the CI gate is built on a misattributed PR number.
6. **C9 FAILED on count** — "8 `read_text()` sites" is actually 10 (`pr29-c9-c10.png`). The
   *invariant* holds (zero bare sites remain), so this is cosmetic, but it is the same
   pattern of imprecise self-reporting.
7. **C10 FAILED on count** — "10 `text=True` sites" is actually 11 (`pr29-c9-c10.png`).
   Same character as C9: the invariant holds, the number does not.

Everything else checks out: C2, C5, C6, C7, C8, C11, C12, C14, C15, C17, C18 all VERIFIED.

**One HIGH security finding not introduced by this PR but adjacent to it:** `bot_sha256`
breaks on CRLF bots (`submit.py:378`) — see §3.

### PR #30 — `docs(review): rescue PR review reports, plans, and workflow scripts`
Branch `rescue/review-artifacts` → `main`

**Verdict: NO-GO as submitted → GO after remediation. Five body claims FAILED, one was a
genuine security regression in a file this PR adds. The security finding and three of the five
body claims were fixed in `2772d03`; see §1b. The findings below are stated as-reviewed
(against `521559f`) — the audit trail is not retroactively rewritten.**

The diff really is inert with respect to the product (C10, VERIFIED: no `.py`, `.toml`,
`.yml`, `src/`, `tests/`, `.github/` path is touched — `pr30-c10.png`; C1 VERIFIED exactly
39 files / +1562 / −0, `pr30-c1.png`; C3 VERIFIED all paths under `docs/` or
`scripts/wf_pr_review_*`, `pr30-c3.png`). But:

1. **HIGH security — `scripts/wf_pr_review_2324.js:503,523` runs
   `gh pr merge NN --squash --admin`, immediately after `gh pr review NN --approve` at
   `:502/:521`.** The same automated pipeline approves and then admin-merges to `main`,
   overriding required reviews. The only gate is an LLM "santa NICE" verdict. This PR is
   the thing that puts that capability into the repo (`pr30-atv-security.png`). This alone
   is a merge blocker.
2. **C4 FAILED — the PR's central premise is false.** The body says these artifacts "had
   never been committed to any branch." `docs/proof/pr-review-2324/REVIEW_REPORT.md` has
   four prior commits (`8c9ff71`, `a63b94a`, `6866df6`, `500b630`) and is reachable from
   the **pushed remote branch** `origin/docs/pr-review-2324-report` (`pr30-c4b.png`). It
   was never at risk.
3. **C9 FAILED — the scripts are not reproducible**, contradicting the stated reason for
   keeping them. `wf_pr_review_2324.js:17` and `wf_pr_review_2126.js:13` hardcode
   `/home/sschofield/repos/atv-bench`, and `:20`/`:15` hardcode a path into a *different*
   repo (`ATV-starterkit`) that is not present here. No env-var override (`pr30-c9.png`).
4. **C8 FAILED — "includes the four correction commits" cites zero of them.** The report
   mentions "correction" twice, and of five hex-looking tokens only three are real commits;
   two (`5074847603`, `5074851214`) are GitHub comment IDs. None of `8c9ff71`, `a63b94a`,
   `6866df6`, `500b630` appear (`pr30-c8.png`).
5. **C7 FAILED — the discarded-diff accounting is incomplete.** `live-integration.yml`
   matches the description (self-hosted → `ubuntu-24.04`, secrets removed). But
   `league-deploy.yml` was a revert of `upload-pages-artifact` v4 → v3, i.e. a revert of
   **PR #25** (`a31ea7e`, "bump … so league-deploy passes org policy"), which the body
   never mentions (`pr30-c7c.png`). Reverting a fix that exists to satisfy org policy is
   exactly the kind of discard that needs to be named.
6. **C2 FAILED — "34 E2E/security screenshots" is 29 screenshots.** 34 is the total file
   count: 1 md + 29 png + 4 txt (`pr30-c2.png`).

C5, C11, C12 VERIFIED. C6 (the "113 dirty paths" inventory) is UNVERIFIABLE — see §7.

---

## 1b. Remediation applied after this review (commit `2772d03`)

PR #30's blocking findings were fixed rather than argued with. Re-verified independently:

| Finding | Fix | Verification |
|---|---|---|
| **HIGH** — `gh pr merge --squash --admin` at `wf_pr_review_2324.js:503,523` | both sites → `--squash --auto` (queues behind branch protection instead of bypassing it) | `grep -n -- '--admin'` → only the prohibition text remains; `grep -c -- '--squash --auto'` → 2 |
| **MEDIUM** — U+200B at `:517` | stripped | `grep -P '\x{200b}'` → no match |
| **C4 FAILED** — "never committed to any branch" | premise corrected in commit message and PR body; the true justification (the untracked #21/#22 proof set + scripts) stated instead | `git ls-remote origin 'refs/heads/*pr-review-2324*'` → `8c9ff71 refs/heads/docs/pr-review-2324-report`; report present there at 139 lines |
| **C2 FAILED** — "34 screenshots" | corrected to 29 screenshots / 34 files | `ls docs/proof/pr-review-2126/*.png \| wc -l` → 29; `ls \| wc -l` → 34 |
| **C9 FAILED** — scripts "reproducible" | reframed as a record of how each review ran, not re-runnable as-is | stated as a Known limitation in the PR body |

**PR #30's verdict therefore moves NO-GO → GO.** The two body-accuracy items (C7, C8) that
were outstanding at the time of this review have since been closed in the PR body — see §6. See §6 for the resolved/outstanding
split, and §3 for the pre- vs post-remediation security counts. The C4 correction is the
substantive one: the review caught a false premise in a PR authored during this same session,
which is the outcome the adversarial setup exists to produce.

---

## 2. Claim tables

### PR #29

| Claim | Verdict | Evidence | Screenshot |
|---|---|---|---|
| C1 cp1252 strict handler kills `submit` in preflight | VERIFIED | Restored `origin/main` cli.py; `submit`→exit 1, `UnicodeEncodeError … '✗'`; doctor/harnesses/games → `'✓' in position 2`; `submit --help` → `'→'` | `pr29-c1.png` |
| C2 `main` had ~25 unguarded glyph sites | VERIFIED | `git grep -c` on origin/main: cli.py:25, view/index.html:1, view/live.html:1 = 27 lines; cli.py alone is exactly 25 | `pr29-c2.png` |
| C3 a fix existed on PR #16 | **FAILED** | `gh pr view 16` → `{"mergedAt":null,"state":"CLOSED","title":"feat: add credible harness benchmark v1"}`; `gh pr diff 16 \| grep -icE 'reconfigure\|errors="replace"\|cp1252\|UnicodeEncode'` → 0 | `pr29-c3.png` |
| C4 layer 1 (stream hardening) independently load-bearing | **FAILED** | Mutated `reconfigure(errors="replace")`→`pass`: **36 passed in 7.52s**, 0 failures; all 5 commands exit 0 under cp1252:strict | `pr29-c4.png` |
| C5 layer 2 (ASCII marks) independently load-bearing | VERIFIED | Mutated `_marks()` to always return glyphs: **3 failed, 33 passed**; `assert '✓' == '[OK]'` | `pr29-c5.png` |
| C6 UTF-8 console not downgraded | VERIFIED | UTF-8: `[OK]`=0, U+2713=7, `?`=0. cp1252: `[OK]`=7, U+2713=0, `?`=0 | `pr29-c6.png` |
| C7 `submit --help` legible, no `?` leaks | VERIFIED | cp1252:strict → exit 0, qmarks=0, nonascii=[]; UTF-8 residual non-ASCII is box-drawing only | `pr29-c7.png` |
| C8 `shell.js` has 0x9d at offset 4062 | VERIFIED | offsets `[4062]`; cp1252 decode → `can't decode byte 0x9d in position 4062` | `pr29-c8.png` |
| C9 "8" unencoded `read_text()` sites | **FAILED (count)** | Baseline shows **10** bare sites; diff adds 10 `read_text(encoding=`. Invariant holds, count wrong | `pr29-c9-c10.png` |
| C10 "10" `text=True` sites | **FAILED (count)** | Baseline shows **11**; diff adds 11 `encoding="utf-8", errors="replace"`. Off by one | `pr29-c9-c10.png` |
| C11 stdin decodes `sys.stdin.buffer` with fallback | VERIFIED | cli.py:600-604, both branches present as described | `pr29-c11.png` |
| C12 windows job now runs both encoding files | VERIFIED | ci.yml:73 runs both; neither filename present on origin/main's ci.yml | `pr29-c12.png` |
| C13 gate treats only exit ≥2 / traceback as failure | **FAILED (stale)** | No exit-code threshold in shipped workflow; delegates to `assert_cp1252_output.py`. Superseded by `814d6db` | `pr29-c13.png` |
| C14 `--json` unaffected under cp1252 | VERIFIED | `games --json` and `harnesses --json`: exit 0, JSON valid, no `[OK]`/`[X]`, qmarks=0 | `pr29-c14.png` |
| C15 every sha256 hashes raw bytes | VERIFIED | All 8 `hashlib.sha256(` sites enumerated: `read_bytes()`, `canonical_bytes()`, `open("rb")`, `.encode("utf-8")`. Zero from text-mode stdout | `pr29-c15.png` |
| C16 "1178 passed, 20 skipped, 1 env failure" | **FAILED** | Observed `4 failed, 1185 passed, 20 skipped in 590.81s`; all 4 trace to uninitialised `vendor/CodeClash` | `pr29-c16.png`, `pr29-hermetic-suite.png` |
| C17 affected commands: submit, submit --help, doctor, harnesses, games | VERIFIED | All five crash on restored origin/main under cp1252:strict; all five exit 0 at HEAD | `pr29-c1.png` |
| C18 gate non-vacuity proven empirically | VERIFIED | Pre-fix → `cp1252 console assertions FAILED … UnicodeEncodeError / Traceback / charmap`, exit 1. Post-fix → `crash-free and legible`, exit 0 | `pr29-c18.png` |
| C19 2000-case fuzz proves `errors="replace"` lossless | **FAILED** | No match for `2000\|fuzz\|hypothesis` in the encoding tests or anywhere in the diff. Test does not exist | `pr29-c19.png` |
| C20 TDD, tests-first, each guard RED→GREEN | UNVERIFIABLE | Tests and src co-committed in most commits; 6 test-only follow-ups, 1 src-only (`58dfb11`) contradicting strict test-first. Intra-commit order not recoverable | `pr29-c20.png` |

### PR #30

| Claim | Verdict | Evidence | Screenshot |
|---|---|---|---|
| C1 39 files, +1562, −0 | VERIFIED | `git diff --stat` → `39 files changed, 1562 insertions(+)`; numstat deletion sum = 0 | `pr30-c1.png` |
| C2 34 E2E/security screenshots | **FAILED** | `ls docs/proof/pr-review-2126/*.png \| wc -l` → **29**. 34 = all files (1 md + 29 png + 4 txt) | `pr30-c2.png` |
| C3 no source, test, or CI changes | VERIFIED | `git diff --name-only \| grep -vE '^(docs/\|scripts/wf_pr_review_)'` → empty; both scripts mode 100644 (non-executable) | `pr30-c3.png` |
| C4 artifacts never committed to any branch | **FAILED** | `docs/proof/pr-review-2324/REVIEW_REPORT.md` has 4 prior commits; `git branch -a --contains` → `remotes/origin/docs/pr-review-2324-report` (pushed) | `pr30-c4.png`, `pr30-c4b.png` |
| C5 main drifted 45 commits ahead | VERIFIED | `git log --oneline origin/main..backup/stale-main-pre-reset \| wc -l` → 45 | `pr30-c5.png` |
| C6 inventory of 113 dirty paths | UNVERIFIABLE | Pre-reset dirty tree no longer exists; uncommitted state leaves no ref/reflog/stash artifact | — |
| C7 two tracked diffs were stale reversions of #26/#27/#28 | **FAILED** | `live-integration.yml` matches. `league-deploy.yml` is a revert of `upload-pages-artifact` v4→v3 = revert of **PR #25** (`a31ea7e`), unnamed in the body | `pr30-c7.png`, `pr30-c7b.png`, `pr30-c7c.png` |
| C8 report cites the four correction commits | **FAILED** | 2 "correction" mentions; 3 of 5 hex tokens are real commits, 2 are GitHub comment IDs; **0 of 4** correction SHAs cited | `pr30-c8.png` |
| C9 scripts kept so passes stay reproducible | **FAILED** | Both parse (`node --check` OK), but hardcode `/home/sschofield/repos/atv-bench` and a path into the separate `ATV-starterkit` repo. No env override | `pr30-c9.png` |
| C10 risk: none to the product | VERIFIED | Grep for `.py/.toml/.cfg/.yml/.yaml`/`src/`/`tests/`/`.github/` → nothing. Census: 2 js, 4 md, 29 png, 4 txt | `pr30-c10.png`, `pr30-suite.png` |
| C11 pre-reset state preserved on `backup/stale-main-pre-reset` | VERIFIED | `git rev-parse --verify` → `8c9ff71a…`. Caveat: no upstream, but tip is reachable from `origin/docs/pr-review-2324-report` | `pr30-c5.png` |
| C12 docs/plans/ = quickstart + run-extra install | VERIFIED | `ls docs/plans/` → exactly `atv-quickstart-implementation.md`, `fix-run-extra-install.md` | `pr30-c12.png` |

---

## 3. Security

### PR #29 — Grade **B** · Critical 0 · High 1 · Medium 1 · Low 1 (`pr29-atv-security.png`, `wf-perms-29.png`, `crlf-hash-29.png`)

- **[HIGH] A08 Integrity — `bot_sha256` breaks on CRLF bots** (`src/atv_bench/submit.py:378`,
  `open_submission_pr`). Text-mode `read_text()`/`write_text()` applies universal-newline
  translation, so a CRLF bot is committed as LF. `submit.py:155` hashes the *original*
  bytes; `store.py:324-326` re-hashes the *committed* bytes. Reproduced by execution:
  `cd03fe07…` vs `172014ed…` (`crlf-hash-29.png`). `store.py:326` rewrites `bot_sha256`,
  the provenance token stops binding, and `load_submissions()` raises `ValueError`.
  Fails **closed** — a legitimate Windows submission is rejected league-wide. This is an
  availability/integrity bug, **not** forgery or authz bypass. **Not introduced by #29**
  (`origin/main` fails identically), but #29 rewrote this exact line for encoding
  correctness and left the newline half unfixed. Fix: `read_bytes()`/`write_bytes()`.
- **[MEDIUM]** Fork PRs execute the new `.github/scripts/assert_cp1252_output.py` and the
  `windows-console-encoding` job from their own copy on `pull_request`. Contained:
  top-level `permissions: contents: read`, no secrets in the job, GitHub-hosted runner.
  Residual risk is CI compute abuse only.
- **[LOW]** The windows job runs `type out.txt`, echoing full CLI output into public logs.
  No secrets reachable there today; brittle if secrets are ever added.
- **VERIFIED-CLEAN:** self-hosted runner is gated by
  `if: github.event_name != 'pull_request'`, so fork PRs cannot reach it or its
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `COPILOT_GITHUB_TOKEN`; all `uses:` are 40-hex
  SHA-pinned (0 unpinned); no `pull_request_target` anywhere; no untrusted `github.event.*`
  interpolated into any `run:`; no eval/exec/pickle/`yaml.load`/`shell=True` in the diff;
  `live_server` binds `127.0.0.1:0`. The 36 new encoding tests pass locally.

### PR #30 — Grade **B** · Critical 0 · High 1 · Medium 2 · Low/Info 2 (`pr30-atv-security.png`, `pr30-config-scan.png`)

> **STATUS: these are the AS-SCANNED findings against commit `521559f` (pre-remediation).**
> The HIGH (`--admin`) and one MEDIUM (U+200B) were **fixed in `2772d03`** — see §1b. The grade
> and counts above are deliberately left at their as-scanned values so the audit trail is not
> retroactively rewritten. **Post-remediation, PR #30 stands at Critical 0 · High 0 · Medium 1
> · Low/Info 2**, verified by re-running the greps in §1b against `2772d03`.

- **[HIGH] ACC-01 / A08 branch-protection bypass in a file this PR adds** —
  `scripts/wf_pr_review_2324.js:503` and `:523` run `gh pr merge NN --squash --admin`,
  preceded at `:502`/`:521` by `gh pr review NN --approve`. One automated pipeline both
  approves and admin-merges to `main`, overriding required reviews; the only gate is an
  LLM "santa NICE" verdict, not an enforced control. This directly undercuts the
  pr-path-guard / pwn-request defences the rest of CI is built around.
  **Fix: drop `--admin`, use `gh pr merge --auto --squash` so branch protection stays
  authoritative.**
- **[MEDIUM] AGENT-01** — U+200B ZERO WIDTH SPACE embedded in an LLM-executed prompt
  string at `wf_pr_review_2324.js:517` ("cleanly/safely resolve"). Almost certainly a
  benign copy-paste artefact (mid-word, not a hidden instruction), but reported because
  invisible codepoints in agent prompt text are exactly the injection carrier AGENT-01
  exists to catch. Fix: strip it; add a CI grep for U+200B/C/D, U+2060, U+00AD, U+FEFF.
  (Note: an earlier revision of this line prescribed a class of U+200B/C/D/FEFF only —
  which would not have matched the U+2060 that was present in this very file. The class
  above is the corrected one.)
- **[MEDIUM] Identity verification fails OPEN** (`.github/workflows/league-publish.yml:129-131`,
  **pre-existing**, surfaced by the repo-wide pass). When the independent `gh api`
  PR-author lookup fails, the code warns and proceeds on the untrusted artifact-supplied
  submitter. Easy to induce: stderr swallowed to `DEVNULL` (:114) under a bare
  `except Exception` (:123), so a rate-limit or transient 5xx silently downgrades the
  check. Mitigated in depth by `--require-spec` binding. Fix: fail closed or retry.
- **[LOW]** Self-hosted runner secret exposure (`live-integration.yml:66,120-126`) —
  correctly gated and step-scoped; residual risk is only that the runner is non-ephemeral.
- **CLEAN:** 0 hardcoded secrets across `.github/`, `scripts/`, `docs/`; 0
  `pull_request_target`; all third-party actions SHA-pinned; 0 `curl|bash`; untrusted bot
  job runs `permissions: {}`, no token, `--network none`, `--read-only`, `--user 65534`,
  `--cap-drop ALL`, `no-new-privileges`, pids/memory limits; symlink-escape and
  identity-pinned path confinement (`league.yml:99-127`); no `eval`/`child_process`/
  `new Function` in the new JS.

---

## 4. CI — status **and honest scope caveats**

> **Evidence for this entire section:** `ci-evidence-2930.png` — the verbatim output of
> `gh pr checks 29`, `gh pr view 29 --json mergeable,mergeStateStatus,reviewDecision`, and the
> same two commands for #30, captured together in one transcript. Every check name, status,
> job URL, and `mergeStateStatus` value below is read directly from that capture and is
> reproducible by re-running those four commands.
>
> **Note — #30's CI was re-triggered by the security fix.** The capture was taken after commit
> `2772d03` (the `--admin` → `--auto` fix) was pushed, which re-ran #30's checks. That run has
> since completed: `hermetic` **pass (1m42s)**, `import-smoke` pass, `pr-path-guard` pass,
> `live-integration` skipping. The table below reflects the completed post-`2772d03` run.
> Both PRs are `MERGEABLE` / `BLOCKED` / `REVIEW_REQUIRED` — blocked on review, not on any
> failing check.

### PR #29 — all green, but the green is narrower than it looks

| Check | Result |
|---|---|
| hermetic | pass (1m42s) |
| import-smoke | pass (50s) |
| pr-path-guard | pass (20s) |
| windows-console-encoding | pass (1m18s) |
| live-integration | **skipping (0s — did not execute)** |

Caveats, stated plainly:

1. **`live-integration` did not run.** Every integration-marked / live-network test path is
   **unverified** by this PR's CI.
2. **The Windows lane is two files wide.** `windows-console-encoding` runs exactly
   `tests/test_cli_windows_encoding.py` and `tests/test_windows_encoding_fileio.py`, plus a
   `chcp 1252` cmd-shell drive of `doctor` / `submit` / `run --demo` / `submit --help`
   checked by `assert_cp1252_output.py`. **The other ~1180 tests run on ubuntu only**,
   where the UTF-8 default hides this entire bug class. A cp1252 regression anywhere
   outside those two files would not be caught.
3. **The ubuntu `hermetic` lane does not reproduce the 4 local failures** from C16, because
   CI provisions `vendor/CodeClash`; the review worktree has it uninitialised
   (`git submodule status` → `-f0694c6`).
4. `mergeStateStatus: BLOCKED` is due to `reviewDecision: REVIEW_REQUIRED` only — **no
   failing check**.

### PR #30 — all green, but green proves almost nothing here

| Check | Result |
|---|---|
| hermetic | pass (1m42s) |
| import-smoke | pass (55s) |
| pr-path-guard | pass (20s) |
| live-integration | **skipping (0s — did not execute)** |

Caveats:

1. **This PR adds only docs, images, and two `scripts/*.js` files.** No Python source or
   test changed, so the passing `hermetic` lane demonstrates only that the PR did not break
   the existing suite — it exercises **none of the PR's own content**. Nothing in CI lints,
   parses, or scans `scripts/wf_pr_review_*.js`; the `--admin` self-merge and the U+200B
   character both sailed through a fully green board.
2. `pr-path-guard` is the only check whose subject matter overlaps the diff.
3. Only `hermetic` and `pr-path-guard` are **required** contexts on main's protection;
   `import-smoke` and `live-integration` are advisory.
4. `BLOCKED` is again `REVIEW_REQUIRED` (1 approving review + code-owner review,
   `require_last_push_approval=true`), not CI.

---

## 5. Merge conflicts / rebase needs

Both PRs: `mergeable: MERGEABLE`, `mergeStateStatus: BLOCKED` (human review only),
`rebase_needed: false`.

File-set intersection is **empty** — verified directly:

```
$ git diff --name-only origin/main...origin/worktree-fix-windows-cp1252-encoding | sort > /tmp/a
$ git diff --name-only origin/main...origin/rescue/review-artifacts | sort > /tmp/b
$ wc -l < /tmp/a   → 24
$ wc -l < /tmp/b   → 39
$ comm -12 /tmp/a /tmp/b   → (no output)
```

PR #29 touches `.github/scripts/`, `.github/workflows/ci.yml`, `src/atv_bench/**`,
`arena/**`, `tests/**`. PR #30 touches only `docs/**` and `scripts/wf_pr_review_*.js`.
**Neither merge forces a rebase of the other.** They are independent, not stacked.

---

## 6. MERGE ORDER

The PRs are **independent** (empty file intersection, §5), so ordering is driven by risk
and by which one is actually ready — not by dependency.

**1. PR #29 — merge first, after one body-correction commit.**
It fixes a reproduced, user-facing crash on the documented install path (C1/C17 VERIFIED,
`pr29-c1.png`) and lands a CI gate proven non-vacuous (C18, `pr29-c18.png`). The code is
correct; the body is not. Required before merge:
   - Correct C3 (PR #16 attribution — the "shipped twice" narrative is unsupported),
     C4 (drop or soften the layer-1 load-bearing claim; `pr29-c4.png` shows 36/36 pass with
     it disabled), C13 (gate description is stale post-`814d6db`), C16 (4 failures, not 1),
     C19 (**remove the 2000-case fuzz claim, or commit the test**), C9/C10 (10 and 11, not
     8 and 10).
   - C19 is the one I would most like resolved by *adding the test* rather than deleting
     the sentence: `errors="replace"` is now on 11 subprocess sites and the losslessness
     property is the thing keeping that safe.

   Merging first also means the smaller, lower-risk change is the one that has to rebase if
   anything goes wrong — and nothing has to, since the file sets are disjoint.

**2. PR #30 — now mergeable; the blocker was fixed in `2772d03` (see §1b).**
The HIGH finding was not incidental to this PR: the PR is *what would have introduced* a
script that approves and then admin-merges to `main` on an LLM verdict
(`wf_pr_review_2324.js:502-503`, `521-523`). Merging a documentation rescue is not worth
installing a branch-protection bypass in the repo.

**Resolved in `2772d03`** (each re-verified independently, §1b):
   - ✅ HIGH — both `gh pr merge --squash --admin` sites → `--squash --auto`. `grep -- '--admin'`
     now matches only the prohibition text.
   - ✅ MEDIUM — U+200B at `:517` stripped; `grep -P '\x{200b}'` → no match.
   - ✅ C4 — the false "never committed to any branch" premise corrected in both the commit
     message and the PR body, with the true justification (the untracked #21/#22 proof set and
     the two scripts) stated in its place.
   - ✅ C2 — corrected to 29 screenshots / 34 files.
   - ✅ C9 — reframed as a record of how each review ran, not re-runnable as-is, and stated as a
     Known limitation in the PR body.

**Still outstanding (non-blocking, body accuracy only):**
   - ✅ C7 — **closed.** The PR body now carries a "Discarded diffs, named explicitly"
     section naming `league-deploy.yml` as a reversion of **PR #25** (`a31ea7e`,
     `upload-pages-artifact` v3→v4 SHA-pinned for org policy) alongside the
     `live-integration.yml` reversion of #26/#27/#28, and states that discarding the
     reversion is the correct outcome. Verified: `main` is still on v4
     (`league-deploy.yml:101`, pinned `7b1f4a76`).
   - ✅ C8 — **closed.** The body cites all four correction SHAs (`8c9ff71`, `a63b94a`,
     `6866df6`, `500b630`) in the "Corrected premise" section, with
     `origin/docs/pr-review-2324-report` named as the branch they are reachable from.

All C7/C8 body-accuracy items are now resolved. **#30 is mergeable**; the only remaining
gate is `reviewDecision: REVIEW_REQUIRED` (human code-owner approval). CI: hermetic,
import-smoke, pr-path-guard all pass; live-integration skipping. `mergeable: true`,
`mergeable_state: blocked` (review only).

**Not on the critical path but should be filed now:** the CRLF `bot_sha256` HIGH
(`submit.py:378`, `crlf-hash-29.png`). It predates #29 and fails closed, so it does not
block either merge — but #29 rewrote that exact line and a Windows-focused PR leaving the
Windows newline bug in place is the natural place to catch it. File as a follow-up issue
with the `cd03fe07…` / `172014ed…` reproduction attached.

---

## 6b. Santa-loop convergence (adversarial gate on THIS report)

Two independent reviewers — Claude and codex CLI (gpt-5.5, xhigh) — had to both return NICE.
The loop ran five rounds and is recorded here because earlier drafts of this report cited no
santa evidence at all despite the screenshots existing.

| Round | Claude | codex | Blocker | Screenshot |
|---|---|---|---|---|
| 1-2 | NAUGHTY | NAUGHTY | Seven-vs-Six miscount; §3 pre/post-remediation labelling; §6 resolved/outstanding split | `santa-codex.png`, `santa-codex-r2.png` |
| 3 | NAUGHTY | NAUGHTY | §4's #30 table spliced two CI runs — `import-smoke` 1m3s / `pr-path-guard` 25s were pre-remediation `521559f` values | `santa-codex-r3.png` |
| 4 | NICE | **NAUGHTY** | **Environmental, not a report defect** — codex's sandbox had no network, so `gh pr checks` failed with `error connecting to api.github.com`; it correctly refused NICE on live claims it could not verify | `santa-codex-r4.png` |
| 5 | **NICE** | **NICE** | none — **CONVERGED** | `santa-codex-r5.png` |

Round 5 re-ran with `codex exec -s workspace-write -c 'sandbox_workspace_write.network_access=true'`.
codex then independently re-ran `gh pr checks 29`, `gh pr checks 30`, and
`gh pr view --json statusCheckRollup`, confirming both §4 tables against live output and
`ci-evidence-2930.png`, plus the file census (2 js / 4 md / 29 png / 4 txt), the
`--squash --auto` remediation, and absence of U+200B. Transcript: `/tmp/codex-santa5-2930.txt`
(verdict at line 1504).

**Scope limit:** a NICE verdict attests to the accuracy of *this report*, not to the
correctness of PR #29's or #30's code. Code-level confidence rests on the claim tables (§2)
and CI (§4).

**Method caveat:** each round's prompt states the prior round's blocker as fixed, which is mild
priming toward NICE. Rounds 4 and 5 added a symmetric instruction ("no reason to return NAUGHTY
for inability to run them — judge on merits") to offset it. Round 4's split verdict is evidence
the gate was not merely rubber-stamping.

---

## 7. Known limitations of this review

- **`live-integration` never ran on either PR.** No live-network or integration-marked path
  was exercised. Any defect reachable only through a real provider call is outside this
  review.
- **Windows coverage is two test files.** Everything else was verified on ubuntu by
  *simulating* cp1252 via `PYTHONIOENCODING=cp1252:strict PYTHONUTF8=0`. That is a faithful
  proxy for the encoder, but it is **not** a real Windows console: the legacy stdio path
  and console-vs-pipe behaviour are only observable in the `chcp 1252` CI step.
- **PR #29 C20 (TDD process) is not mechanically falsifiable.** Per-commit path analysis
  shows tests and src co-committed, with 6 test-only follow-ups and 1 src-only commit
  (`58dfb11`) that contradicts strict test-first — but intra-commit authoring order and the
  claimed scratch-tree RED confirmations cannot be recovered from the final tree
  (`pr29-c20.png`).
- **PR #30 C6 (113 dirty paths) is unverifiable in principle.** The pre-reset dirty working
  tree no longer exists, and uncommitted state leaves no artifact reachable from any ref,
  reflog, or stash. The number rests entirely on the author's assertion. By extension, the
  claim that "only these were unique" among those 113 cannot be checked either — **there
  may be rescued-worthy content that was discarded, and no reviewer can tell.**
- **The full local suite ran with `vendor/CodeClash` uninitialised**, producing 4
  environmental failures (`pr29-c16.png`). I did not re-run with the submodule provisioned;
  the claim that all 4 are purely environmental is inferred from
  `git submodule status → -f0694c6` plus the failure names, not from a clean re-run.
- **Security grades cover less ground than "B" implies.** The repo has no `.vscode/`, no
  MCP config, no hooks, agents, or skills, so 4 of 5 AgentShield config categories were
  **N/A rather than passing**. The strong config-surface result rests entirely on the
  workflow/secret surface.
- **PR #29 C14** was confirmed for `games --json` and `harnesses --json` only; other
  `--json` subcommands were not individually exercised.
- **PR #29 C7's historical em-dash crash** was reproduced on `origin/main` (C1) but is not
  directly observable at HEAD by construction.
