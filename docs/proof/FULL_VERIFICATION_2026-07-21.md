# Full local verification — July 21, 2026

Verified commit: `6cfba6362a418b19e582a6f681cb205edb833531`

## Result

- Fixed-plan commands: **29 passed**
- Failed: **0**
- Skipped commands: **0**
- Timed out: **0**
- Evidence gates: **28 passed, 2 blocked**
- Official run claimed: **false**
- Launch ready: **false**

The two blocked local-proof gates are intentionally not inferred:

1. `launch.contamination_retraction` requires independent policy review.
2. `release.security.filesystem` requires Linux-only FIFO and unprivileged-symlink
   confinement evidence that the Windows local run skipped. Linux GitHub CI for this
   commit passed, but the local proof does not import CI evidence.

The broader launch audit still requires official model attestations, public signatures,
independent task/security/statistical review, external reproduction, live default-branch
governance after merge, and an immutable release.

## Content-addressed evidence

| Artifact | File SHA-256 | Canonical SHA-256 |
|---|---|---|
| Manifest | `9307944ea47fa421ead83b28788004a26012f1674e34a23c851758d6dfea4472` | `7edae2475348c6e1500fda7bf344c4aca54d0497e5adf280ccfc3aab1f9c4770` |
| Proof | `371e56edb03317d72f9b8ba000bd63b875f02efa457e0c51e31420418e4036fd` | `5c5fda7e2da5d5a9fed1636485f345c98eb69fdd962223c3c0af4ed8e65f3891` |
| Launch audit | `b526bab32430b61f7b4a057fa2a6a99762e46068da5e2fdddc48648f33b66c90` | n/a |

Repository state recorded by the manifest:

- clean worktree;
- HEAD tree `69643b75e2090c65d803ac5fcbaf20ba5301d40d`;
- repository tree digest
  `7e06dd4f4b2244774714253e633c50ab41c47cad120e8dd46f167d00083f808e`.

