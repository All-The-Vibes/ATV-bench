# Gap-Fill TDD Plan — porting PR16's data-science rigor onto PR17's execution spine

**Context.** Deep-research (6 parallel readers + adversarial synthesis) compared shyam's
PR #16 (`codex/harness-benchmark-v1`) and our PR #17 (`worktree-gm-…-17320`) against
`IMPLEMENTATION_PLAN.md` + README. Finding: PR17 is the working executor (real referees,
live Docker matches, factored Bradley-Terry + lift, 864 hermetic tests) but publishes
ratings/lift with **statistical and decision-layer gaps**. PR16 is the credibility
rulebook (paired scheduling, clustered stats, futility/winner gates, immutable bundles)
but largely unimplemented on a live spine. This PR ports PR16's *hermetically testable*
machinery onto PR17's real code.

**Branch base:** PR17 head (`worktree-gm-feat-community-league-17320`).
**Discipline:** RED → GREEN → REFACTOR. Every item below is pure-Python, deterministic,
seedable, and needs **zero live LLM time**.

## The single most important fault line

PR17's `lift.compute_lift` bootstrap **resamples whole matches i.i.d.** ("each match is an
independent run"). But N games built by ONE harness build-artifact are *not* N independent
observations — they share the artifact. Resampling the nested unit understates CIs
(anticonservative), so every "CI excludes 0 → real harness" claim is potentially phantom
precision. PR16's central rule: *resample the cluster (task/build-artifact), not the row.*
PR17 already ships `stats.bootstrap_ci(cluster_ids=…)` but `compute_lift` never uses it.

---

## Priority order (by credibility-per-test-hour)

### G2 — Cluster-correct lift bootstrap  `[critical, M]`
**Gap:** `lift.compute_lift` resamples matches, not clusters → inflated precision.
**Design:** add `cluster_ids: Sequence | None` param to `compute_lift`/`fit_lift`. When
present, the bootstrap draws whole clusters with replacement (mirror `stats.bootstrap_ci`
cluster logic) and refits theta on the pooled rows. `RatingMatch` gains an optional
`cluster_id: str | None = None` (frozen, back-compat default). Corpus→matches wires the
cluster key = `f"{game}::{artifact_pair}"` (game × the two build-artifacts), falling back
to `game` when artifact ids absent.
**Acceptance:**
- On synthetic **correlated** data (M games per cluster, intra-cluster ρ>0), clustered CI
  is **strictly wider** than the naive per-match CI. (RED asserts widening; currently equal.)
- On i.i.d. data (1 row/cluster) clustered ≈ naive CI (within tolerance).
- `cluster_id=None` path is byte-identical to today's output (regression pin).
- Deterministic under fixed seed.
**Tests:** `tests/test_lift_clustered.py`.

### G6 — Futility / quality publication gates  `[high, S]`
**Gap:** PR17 computes `FIT_EXCLUDED`, `data_sufficiency`, `n_min_for_power` but never
**gates** publication on them. PR16 enforces `max_infrastructure_error_rate`,
`min_eligible_tasks`, `min_paired_trials_per_cell`, `max_grader_nondeterminism_rate`.
**Design:** new `src/atv_bench/gates.py` with pure `evaluate_quality_gates(stats, *, thresholds)`
→ `QualityGateReport{passed: bool, failures: [{gate, observed, threshold}]}`. Thresholds
are a frozen dataclass with documented defaults. Wire an advisory `quality_gates` block +
`publishable` boolean into `build_ratings_doc`.
**Acceptance:**
- infra-error-rate above cap → `passed=False` with the named failure.
- eligible-N below min → fail-closed.
- all-clear synthetic corpus → `passed=True`, empty failures.
- gate report is deterministic and serializable.
**Tests:** `tests/test_gates.py`.

### G5 — Winner / equivalence decision rule  `[high, M]`
**Gap:** ratings publish theta+CI but no verdict function. Can rank on noise.
**Design:** `gates.decide_contrast(diff, lo, hi, *, margin, direction_stability, n_policies)`
→ `{'verdict': 'A_wins'|'B_wins'|'equivalent'|'inconclusive', 'reason': …}`. Winner requires
CI excludes the preregistered practical `margin` AND direction stable AND ≥2 model policies
AND not FIT_EXCLUDED. `equivalent` when CI ⊂ (−margin, +margin). Else `inconclusive`.
**Acceptance:**
- CI entirely above +margin, stable, ≥2 policies → `A_wins`.
- CI straddling 0 → `inconclusive`.
- CI inside ±margin → `equivalent`.
- single policy → forced `inconclusive` regardless of CI (matches PR16 "≥2 snapshots").
**Tests:** `tests/test_decide.py`.

### G3 — Paired permutation (sign-flip) test  `[medium, S]`
**Design:** `stats.paired_permutation_test(diffs, *, n_perm, seed)` → p-value by random
sign flips. Distribution-free corroborator beside the bootstrap.
**Acceptance:** all-positive diffs → p≈0; symmetric-around-0 → p≈1; seed-deterministic;
p∈[0,1]. **Tests:** extend `tests/test_stats.py`.

### G4 — Direction-stability metric  `[medium, S]`
**Design:** `stats.direction_stability(boot_draws)` → fraction of bootstrap replicates whose
sign matches the point estimate. Feeds G5's `direction_stability` input.
**Acceptance:** all-same-sign → 1.0; 50/50 → ~0.5; range [0,1]. **Tests:** `tests/test_stats.py`.

### G1 — Paired, order/side-balanced scheduler  `[high, M]`
**Design:** `src/atv_bench/scheduler.py` `build_paired_schedule(harnesses, games, *, seed,
repeats)` → list of matches where every unordered pair plays each game with **balanced
sides** (A/B seat rotated so first-mover advantage cancels). Pure planning, no execution.
**Acceptance:** each pair appears with equal A-seat and B-seat counts per game; total =
C(n,2)·games·repeats; deterministic under seed; side-balance invariant asserted.
**Tests:** `tests/test_scheduler.py`.

### G7 — Content-addressed immutable result bundle + offline reproduce  `[high, M]`
**Design:** `src/atv_bench/bundle.py` `build_bundle(ratings_doc, matches, meta)` →
canonical-JSON bundle with a `content_id = sha256(canonical_bytes)` and a reproduction
tuple (seeds, versions, cluster policy). `verify_bundle(bundle)` recomputes the id and
re-runs the deterministic rating/lift math offline, asserting the published numbers match.
**Acceptance:** round-trip `verify_bundle(build_bundle(...))` is True; any 1-byte mutation
→ False; content_id stable across runs (canonicalization); recomputed theta == published.
**Tests:** `tests/test_bundle.py`.

### G9 — Track + trust-tier schema fields  `[medium, S]` (fold into G7 bundle)
Add `track` (`league|controlled|systems`) and `trust_tier`
(`local-self-attested|attested|reproduced`) to the ratings/bundle schema, defaulting
fail-closed to `local-self-attested` + `rankable=false` (matches PR16). **Tests:** schema test.

**Deferred (need live/Docker or large surface):** G8 protocol versioning/capability
negotiation, model-gateway attestation, OCI adapter, Ed25519 lifecycle receipts, G10
referee-determinism live gate. These yield less credibility-per-hermetic-test-hour.

---

## e2e proof obligation
After GREEN on the hermetic suite, run the real leaderboard render + agent-browser
screenshot path (`scripts/screenshot_verified_board.py` / Section 8) so the new
gated/clustered numbers appear on the board, and attach screenshots to the PR.
