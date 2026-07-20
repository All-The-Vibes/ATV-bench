# Versioning and Reproduction

An ATV-bench result is identified by the complete evaluation tuple, not by a mutable
harness name or leaderboard row:

```text
track
benchmark release
protocol version
task id and task digest
runner version and image digest
harness manifest and executable/image digest
model policy and resolved model evidence
budget policy
grader version and digest
analysis version
report version
trial id and seed
```

Changing any load-bearing element creates a new result identity. Correcting a task or
grader never silently rewrites an old score; affected results are retracted or marked
superseded and recomputed under a new version.

## Compatibility policy

- Schemas use explicit versioned identifiers such as `atv.harness/v1`.
- Readers reject unsupported major versions before execution.
- Additive compatible changes may use a minor benchmark release.
- Semantic or scoring changes require a new benchmark release.
- Task content, runner images, harness artifacts, graders, and result bundles are
  addressed by cryptographic digest rather than mutable tags.

## Reproduction requirements

An official trial bundle must contain or reference by immutable digest:

- the normalized request and complete JSONL trajectory;
- accepted stdout/stderr excerpts under documented size limits;
- task and base-tree identities;
- harness and runner identities;
- gateway-observed model calls and usage;
- enforced budget state and terminal reason;
- output-tree manifest and digest;
- hidden-grader result;
- runner, model-gateway, and grader attestations;
- the canonical result and bundle digest.

`atv eval verify` must detect tampering without network access. `atv eval reproduce`
must reconstruct the declared environment, rerun the grader against the immutable
artifact, and explain any mismatch. An official public launch additionally requires at
least one independent operator to reproduce the pilot within preregistered tolerance.
