# Official Benchmark Release Checklist

No official harness leaderboard may be published until every required item is checked.

## Repository and supply chain

- [ ] Default branch protected.
- [ ] Required CI/security/task/reproducibility checks enabled.
- [ ] CODEOWNERS review enforced.
- [ ] Protected official-evaluation environment configured.
- [ ] Third-party Actions pinned to full commit SHAs.
- [ ] Release tag signed.
- [ ] Wheel and source distribution install from clean environments.
- [ ] `uv sync --extra run` succeeds from a clean checkout.

## Protocol and adapters

- [ ] Schemas are versioned and published.
- [ ] Process and OCI adapters pass the same conformance suite.
- [ ] A third-party adapter integrates without ATV-Bench core-code changes.
- [ ] Unknown protocol versions fail closed.
- [ ] Cancellation kills all descendants.

## Security

- [ ] Harness receives no provider/GitHub/cloud/signing credential.
- [ ] Only model-gateway egress is allowed.
- [ ] Hidden tests are unavailable during harness execution.
- [ ] Symlink/junction/hardlink/special-file escapes fail.
- [ ] Resource and output bombs remain contained.
- [ ] Attestation signatures and workload identities verify.
- [ ] Independent security review completed.

## Tasks

- [ ] Every task passes oracle/no-op/regression/alternative/exploit/mutation gates.
- [ ] Graders replay deterministically.
- [ ] Task author and independent reviewer approved.
- [ ] Public/private/rotation split recorded.
- [ ] Contamination review completed.

## Experiment

- [ ] Fresh trial is the independent unit.
- [ ] Harness order and worker assignment are paired/randomized.
- [ ] Required trial count or precision target met.
- [ ] Exact model policy and budgets frozen.
- [ ] Infrastructure failures excluded/requeued by policy.
- [ ] Human baseline included where required.

## Analysis

- [ ] Task-clustered uncertainty computed.
- [ ] Equivalence margin preregistered.
- [ ] Winner rule enforced automatically.
- [ ] Category/model/budget sensitivity reported.
- [ ] Accepted/excluded trial set content-addressed.
- [ ] Independent statistics review completed.

## Publication and operations

- [ ] Raw sealed evidence retention configured.
- [ ] Sanitized bundles scanned and published.
- [ ] Reproduction command succeeds.
- [ ] One external reproduction completed.
- [ ] Appeals contact and 14-day window published.
- [ ] Incident/retraction log reviewed.
- [ ] Cost/capacity forecast approved.
