# Fix: `uv tool install` does not pull the CodeClash `run` dependency

## Root cause

The reported symptom is `atv-bench doctor` reporting:

```
· CodeClash arena dep (for `run`): vendored CodeClash not importable — reinstall the tool ...
```

The install itself did **not** fail. The problem is the **install command advertised to
users only installs the base dependencies, never the `[run]` extra**, and the remediation
message tells the user to re-run the exact same command — an infinite no-op loop.

### Chain of evidence

1. `pyproject.toml` puts the CodeClash git dependency inside the **optional** `run` extra:
   ```toml
   [project.optional-dependencies]
   run = ["codeclash @ git+https://github.com/CodeClash-ai/CodeClash@f0694c6...", ...]
   ```
   The 19 packages installed in the user's log are the base deps only — `codeclash`,
   `litellm`, `mini-swe-agent`, etc. are absent.

2. The advertised install (README lines 25/27/120 and the error text in
   `preflight.check_codeclash` + `codeclash_env.import_codeclash`) is:
   ```
   uv tool install --from git+https://github.com/All-The-Vibes/ATV-bench atv-bench
   ```
   This resolves the base package with **no extras** → `codeclash` never installed →
   `codeclash_env.codeclash_available()` returns `False` → doctor prints the `·` line.

3. The remediation string is:
   ```
   uv tool install --reinstall --from git+.../ATV-bench atv-bench
   ```
   Same command minus `--reinstall`. Re-running it reinstalls the *same base-only*
   set. **The fix text does not resolve the problem it describes.**

4. Verified working install (extra requested via a PEP 508 `--from` spec):
   ```
   uv tool install --reinstall \
     --from 'atv-bench[run] @ git+https://github.com/All-The-Vibes/ATV-bench' atv-bench
   ```
   After this, `import codeclash` succeeds and `doctor` prints
   `✓ CodeClash arena dep (for run): importable at pinned version`.

   Note the naive `--from git+... 'atv-bench[run]'` form is rejected by uv:
   `Package requirement (--from) conflicts with install request`. The extra MUST be
   folded into the `--from` PEP 508 string as `atv-bench[run] @ git+...`.

### Why the existing distribution tests did not catch it

`tests/test_distribution.py::test_exit9_tool_install_remediation` only asserts the fix
text *mentions* `uv tool install`/`--reinstall`/`doctor`. It never asserts the advertised
command actually requests the `run` extra. So a remediation that reinstalls base-only
passes the test while being functionally useless.

## Scope / non-goals

- **In scope:** correct every user-facing install + remediation string so the command a
  user runs actually installs `codeclash`; tighten tests to encode "must request `[run]`".
- **Out of scope:** moving CodeClash out of an extra into base deps (rejected — base
  install must stay light for `fingerprint`/`submit`/`board`/`play` which don't need
  Docker/CodeClash). Changing the CodeClash pin.

## Acceptance criteria (TDD — write these tests first, watch them fail, then fix strings)

### AC1 — Remediation strings request the `run` extra
`tests/test_distribution.py` (new test `test_codeclash_fix_requests_run_extra`):
- Gather every user-facing codeclash-dep remediation string (reuse `_codeclash_fix_texts()`:
  `preflight.check_codeclash().fix` and `import_codeclash()`'s `CodeClashUnavailable` message).
- Assert that any `uv tool install`/`uv tool upgrade` recovery in those strings requests the
  `run` extra — i.e. the string contains `atv-bench[run]` (a `uv tool upgrade` bare form is
  only acceptable if a matching `[run]` install form is also present).
- **RED first:** current strings contain bare `atv-bench`, no `[run]`.

### AC2 — README advertises the extra for `run`
`tests/test_distribution.py` (new `test_readme_install_requests_run_extra`):
- The README install/demo commands that lead into `run --demo` / a live `run` must use the
  `atv-bench[run] @ git+...` form (or an explicit two-step: base install + documented
  `--with`/reinstall-with-extra for `run`).
- Assert README contains `atv-bench[run] @ git+https://github.com/All-The-Vibes/ATV-bench`.
- Keep existing `test_uvx_invocation_string` green (still names a git source).
- **RED first:** README lines 25/27/120 have no `[run]`.

### AC3 — Invocation-shape guard is correct
Add an assertion (unit, no network) documenting the uv constraint so we never regress to the
rejected form: no advertised string may pair `--from git+…ATV-bench` with a separate
`atv-bench[run]` install target on the same command. The extra must be inside the `--from`
PEP 508 spec.

### AC4 — Base install stays extra-free (regression guard)
`test_distribution.py`: assert the base/quickstart install line (the one before `run` is
introduced) does **not** carry `[run]`, so the light-install path for
fingerprint/submit/board/play is preserved.

### AC5 — Manual end-to-end verification (documented, not CI-gated)
Documented in the PR body / `docs/errors.md`:
```
uv tool install --reinstall --from 'atv-bench[run] @ git+https://github.com/All-The-Vibes/ATV-bench' atv-bench
atv-bench doctor   # → ✓ CodeClash arena dep (for run): importable at pinned version
```
(Already verified locally during diagnosis.)

## Implementation steps

1. **RED:** add AC1–AC4 tests to `tests/test_distribution.py`; run `pytest -k distribution`,
   confirm the four new tests fail.
2. **GREEN — fix the source strings:**
   - `src/atv_bench/preflight.py::check_codeclash` `fix=` → use
     `uv tool install --reinstall --from 'atv-bench[run] @ git+https://github.com/All-The-Vibes/ATV-bench' atv-bench`.
   - `src/atv_bench/codeclash_env.py::import_codeclash` `CodeClashUnavailable` message → same form.
   - `README.md` lines ~25, ~27, ~120 → the `run`-leading commands use the `[run]` extra form;
     leave any pure-quickstart light install as base (AC4).
   - `docs/errors.md` exit-9 section → mirror the corrected command.
3. **GREEN:** re-run `pytest -k distribution` → all green.
4. **Full suite:** `pytest -q` (unit/deterministic only) to confirm no collateral regressions
   in preflight/doctor tests.
5. **Manual:** run AC5 command sequence, capture `doctor` output for the PR.

## Files touched

- `src/atv_bench/preflight.py`
- `src/atv_bench/codeclash_env.py`
- `README.md`
- `docs/errors.md`
- `tests/test_distribution.py` (tests first)
