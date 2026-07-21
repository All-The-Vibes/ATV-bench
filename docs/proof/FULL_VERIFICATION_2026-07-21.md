# Full local verification — July 21, 2026

Verified commit: `6d19f9270ac180a60cb3bbaff52dfd98ea6b1c20`

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
| Manifest | `0de3de69631a221a6dbd6453a8875a34d7ce40fdaa285d24b45cc24691246167` | `64ea38c3289bcd22848aa79228bde54c33c7ce724ab2d6467bfbe5bb2c560d29` |
| Proof | `ce077d2756e2aea30172efafd973f6350f8ee23bc610fd5fd800aac60cc4ea86` | `58f4c93dabf7f70ca5e156648cc14ac6a8c27861befbe5c92aabedd946b25f41` |
| Launch audit | `d97d9c7efafffabbb13095bc2ff0b4458643120e018f1bad1c7fad97e3aa30e1` | n/a |

Repository state recorded by the manifest:

- clean worktree;
- HEAD tree `b1f166ef0bd7e1199a116b530937e32a171a1cff`;
- repository tree digest
  `f30a638a59299a59a7deca39092517fab370503c2deb5626d615bdf4b36f59f4`.
