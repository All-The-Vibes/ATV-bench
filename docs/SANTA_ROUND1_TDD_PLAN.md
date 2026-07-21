# Santa-loop Round 1 — TDD remediation plan

Two independent reviewers (Claude Opus `python-reviewer` + Codex `gpt-5.4`, no shared
context) reviewed the science-critical core of the combined harness-eval line. Both
converged on the **single-cluster zero-width CI** as the headline defect. Codex added four
more real findings. Verdict: **NAUGHTY** → fix under TDD, re-review with fresh reviewers.

Each fix is written RED-first: a failing test that pins the *scientific* property, then the
minimal code change to make it pass.

## Findings → tests → fixes

| # | Finding (reviewer) | Failing test (RED) | Fix (GREEN) |
|---|--------------------|--------------------|-------------|
| 1 | **Single-cluster clustered bootstrap collapses to a zero-width CI** — phantom precision in exactly the direction clustering exists to prevent (BOTH reviewers, critical) | `test_single_cluster_bootstrap_refuses` in `test_lift_clustered.py` and `test_stats.py` | Raise `LiftError` / `ValueError` when `n_unique_clusters < 2` in `lift.compute_lift` and `stats.bootstrap_ci` |
| 2 | **`bootstrap_ci` lacks input validation** — empty i.i.d. data → NaN bounds; empty clustered → raw `ValueError`; mismatched `cluster_ids` silently drops rows (Codex, critical) | `test_bootstrap_ci_input_validation` in `test_stats.py` | Validate non-empty `values`, and `len(cluster_ids) == len(values)`; raise `ValueError` with a clear message |
| 3 | **`evaluate_quality_gates` is fail-OPEN on missing metrics** — an empty stats blob passes publication gating, violating the fail-closed rulebook (Codex, critical) | `test_empty_stats_blob_fails_closed` in `test_gates.py` | Require the four load-bearing signals to be present; a missing required signal is a `missing_signal` failure → `passed=False` |
| 4 | **Optimizer convergence ignored** — `scipy.optimize.minimize(...).success` unchecked in `lift._fit_theta` and `stats._bradley_terry_fit`; an unconverged fit is silently used (Codex) | `test_unconverged_fit_raises` in `test_stats.py` | Raise a clear error when `res.success` is False (with iteration budget bumped so healthy fits still converge) |
| 5 | **Bundle omits cluster policy inputs** — clustered-bootstrap CI claims are not reproducible from the bundle because `cluster_ids`/CI level aren't persisted (Codex) | `test_bundle_persists_ci_level_and_cluster_policy` in `test_bundle.py` | Persist `ci` and (when clustered) the `cluster_ids` in `reproduce`; recompute honoring them in `verify_bundle` |
| 6 | **CodeClash import smoke test no longer in required PR CI** (Codex) | n/a (CI config) | Add a PR-triggered `integration-smoke` job that inits the submodule, installs `.[run,dev]`, and runs `-m integration` so the `.[run]` import contract stays pre-merge |

## Non-blocking (logged, not fixed this round)
- Content-addressing is tamper-*evident*, not a signature — acknowledged design scope, both reviewers agree it meets the stated claim.
- `compute_lift` picking the lexicographically-first base model when a harness spans >1 model: guarded by roster invariant; left as a follow-up (surfaced in PR body).
- Magic-number thresholds (`0.1` intransitivity, gate defaults) → named constants: cosmetic, deferred.
