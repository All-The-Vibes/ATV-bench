# Security Policy

## Supported status

ATV-Bench is pre-alpha. The existing offline bot arena has meaningful containment controls. Arbitrary
model-backed harness execution is not yet approved for public untrusted workloads.

## Central security invariant

Untrusted harness code never receives GitHub, cloud, registry, signing, hidden-grader, or
model-provider credentials.

Official execution must use:

- an ephemeral isolated execution cell;
- an empty home directory and explicit mounts;
- a one-trial model-gateway capability;
- model-gateway-only network egress;
- enforced time/token/cost/call/CPU/memory/disk/PID/output limits;
- hidden grading in a new networkless cell after harness termination;
- controller, gateway, evaluator, and publisher attestations.

## Reporting vulnerabilities

Do not open a public issue for vulnerabilities involving credentials, sandbox escape, hidden tests,
signing, runner isolation, or privileged workflows.

Report privately to the repository security advisory channel:

`https://github.com/All-The-Vibes/ATV-bench/security/advisories/new`

Include:

- affected commit/version;
- minimal reproduction;
- expected and actual trust boundary;
- whether credentials, hidden tests, other runs, or published scores were exposed;
- proposed remediation if known.

## Response targets

| Severity | Initial response | Target mitigation |
|---|---:|---:|
| Critical | 1 business day | 3 business days |
| High | 2 business days | 7 business days |
| Medium | 5 business days | Next patch release |
| Low | 10 business days | Planned release |

## Score integrity incidents

A security issue affecting task secrecy, execution identity, grading, artifact integrity, or analysis
triggers:

1. Freeze affected publication.
2. Preserve evidence.
3. Revoke credentials/capabilities.
4. Identify affected trials/releases.
5. Publish signed invalidation/tombstone records.
6. Rotate tasks or keys where needed.
7. Rerun only after independent review.

## Out of scope

- Social engineering unrelated to the repository.
- Denial of service against a local self-attested run.
- Findings that require a user to intentionally execute unrelated malicious code outside ATV-Bench.

These exclusions do not apply when an official runner, credential, hidden task, or score is affected.
