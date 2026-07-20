# Harness Benchmarking Test Plan

Status: proposed
Parent: [`HARNESS_BENCHMARKING_BLUEPRINT.md`](HARNESS_BENCHMARKING_BLUEPRINT.md)

## Goal

Prove that ATV-Bench can execute arbitrary harnesses under equivalent, isolated, resource-bounded
conditions and produce reproducible, attested, statistically valid results.

## Test diagram

```text
manifest
  -> schema validation
  -> capability negotiation
  -> scheduler
      -> clean workspace
      -> credential broker
      -> harness runtime
          -> JSONL events
          -> artifact
      -> attestor
      -> hidden grader
      -> immutable bundle
      -> analyzer
      -> report
```

| Flow or branch | Required test type | Current coverage | Gap |
|---|---|---|---|
| Valid/invalid harness manifest | Unit + property | None | Add schema corpus |
| Protocol version mismatch | Conformance | None | Add fail-early test |
| Stdout pollution/malformed JSONL | Conformance | Last-JSON heuristic only | Replace parser |
| Committed/staged/untracked output | Unit | Covered | Add output-tree digest |
| Binary/oversized artifact | Security integration | Partial bot-size checks | Add harness artifact limits |
| Fresh workspace per trial | Integration | Bespoke comparison only | Add state-leak test |
| Ambient home/plugin leakage | Security integration | One bespoke script | General mount audit |
| Credential broker secrecy | Security integration | None | Canary-secret test |
| Model route mismatch | Integration | Parsed model only | Gateway-attestation test |
| Time/token/cost budget | Integration | Wall timeout only | Central enforcement tests |
| Descendant process timeout | Integration | None | Process-tree kill test |
| Egress allowlist | Integration | Bot network-none only | Model-gateway-only test |
| Hidden grader late mount | Security integration | None | Pre-exit access test |
| Oracle/no-op task validation | Task conformance | None | Validator |
| Alternative correct solution | Task conformance | None | Multi-solution fixtures |
| Exploit/mutation detection | Adversarial | Bot attacks only | Task grader attacks |
| Deterministic grading | Reproducibility | Arena deterministic | Generic grader replay |
| Paired randomized schedule | Unit/property | None | Balance/order tests |
| Infrastructure retry | Integration | Publish retry only | Trial retry semantics |
| Attestation verify/tamper | Security unit | Client provenance only | Runner statement tests |
| Idempotent result ingest | Integration | League covered | Eval store equivalent |
| Task-clustered bootstrap | Statistical simulation | None | Known-effect calibration |
| Equivalence/winner gate | Statistical simulation | None | No-winner fixtures |
| Bradley-Terry tournaments | Statistical fixture | Sequential Elo only | CodeClash parity fixture |
| Cost/time curves | Analysis | None | Budget fixtures |
| Release reproduction | End-to-end | Package build only | Trial-bundle replay |
| Windows symlink plugin | Cross-platform | Target-specific fix | General conformance fixture |
| Clean `uv` install with real-run extra | Packaging E2E | Fails on current public main | Fix dependency path |
| CP1252/legacy Windows CLI output | Cross-platform | Fails on current public main | Encoding-safe output test |
| Patch-file digest verification | Provenance unit | Bot hashes only | Hash written bytes |
| Repository governance settings | Policy integration | Not enabled live | API policy check |

## Unit suites

### Schema

- valid minimal manifest;
- unknown fields;
- missing required fields;
- incompatible protocol version;
- unsafe path;
- mutable image tag without digest;
- undeclared environment variable;
- invalid network policy;
- unsupported budget capability.

### JSONL protocol

- partial line;
- invalid UTF-8;
- unknown event type;
- event before `hello`;
- duplicate terminal result;
- event after result;
- cumulative usage decreases;
- artifact path traversal;
- artifact digest mismatch;
- stdout human text;
- bounded stderr.

### Scheduler

- every harness receives every task;
- order balance within blocks;
- deterministic schedule from seed;
- worker assignment balance;
- retries preserve original assignment metadata;
- cache key includes task, harness, model, budget, trial, and protocol versions.

### Statistics

- zero-effect simulation reports equivalence/inconclusive;
- known positive effect is recovered;
- nested games do not increase harness trial count;
- task bootstrap samples tasks;
- excluded infrastructure runs do not become failures;
- tie-aware tournament fixtures;
- rank stability output deterministic under fixed seed.

## Adapter conformance

Every adapter runs the same black-box suite:

1. Handshake and capability negotiation.
2. No edit.
3. Single-file edit.
4. Multi-file edit.
5. Commit edit.
6. Untracked artifact.
7. Binary artifact.
8. Nonzero exit with artifact.
9. Nonzero exit without artifact.
10. Timeout with cooperative cancellation.
11. Timeout ignoring cancellation.
12. Child-process leak.
13. Huge stdout/stderr.
14. Unknown model.
15. Multiple models/subagents.
16. Missing usage.
17. Retry disclosure.
18. Environment-secret isolation.
19. Network denied/allowed.
20. Windows paths and newlines.
21. Process/OCI canonical-result equivalence.

## Task validation pipeline

For every task:

```text
schema
  -> build image from digest
  -> oracle solution passes
  -> no-op fails
  -> regressions pass
  -> alternative solution passes
  -> exploit attempts fail
  -> mutation cases fail
  -> grader repeatability
  -> independent human review
```

Task status is `eligible` only when every gate passes.

## Security integration

### Secret isolation

- Broker injects an opaque handle.
- Harness environment contains no provider secret.
- Shell/MCP child processes contain no provider secret.
- Logs contain no provider secret.
- A planted canary secret is never observable.

### Filesystem

- Read-only task base.
- Only workspace/artifact mounts writable.
- Symlink and junction escapes denied.
- Device files and host sockets unavailable.
- Hidden grader absent during harness execution.

### Network

- DNS/HTTP to arbitrary internet denied.
- Cloud metadata denied.
- Model gateway allowed.
- Task-specific allowlist enforced.
- Network events included in trial evidence.

### Resource control

- CPU, memory, process, disk, file size, wall time, model tokens, model calls, and cost each have
  independent limit tests.
- Exceeding one limit produces a typed terminal status.
- Full process tree and containers are removed.

## End-to-end fake-model suite

Use deterministic fake providers:

- successful edit;
- no edit;
- delayed response;
- retryable provider failure;
- permanent provider failure;
- model-route mismatch;
- usage underreport;
- malformed streaming response;
- subagent call.

This suite runs on every push with no external credentials.

## Gated live adapter suite

Run locally or on a dedicated benchmark operator host. These are never ordinary
push/PR GitHub Actions and never publish scores automatically:

- Codex;
- GitHub Copilot CLI;
- Claude Code;
- generic process wrapper;
- OCI wrapper.

Each live run:

- uses a tiny private smoke task;
- verifies resolved model and usage;
- runs from a clean home;
- confirms no ambient skills/plugins/MCPs;
- uploads a private attested bundle;
- never affects public scientific scores.

## Statistical acceptance gates

Before pilot publication:

- at least 50 eligible tasks;
- at least five trials per cell;
- infrastructure error rate below 2%;
- grader nondeterminism below 0.1%;
- no task with oracle/no-op/exploit failure;
- paired schedule imbalance zero;
- bootstrap coverage validated on simulations;
- winner gate refuses a synthetic null effect;
- one external reproduction matches within preregistered tolerance.

## Commands to add

```bash
atv schema check schemas/
atv harness validate harness.yaml
atv task validate tasks/example
atv trial smoke --harness harness.yaml --task tasks/example
atv eval plan --suite pilot-v1 --harness A --harness B
atv eval run plan.json
atv eval verify bundle/
atv eval analyze results/
atv eval reproduce <trial-id>
```

Every failure must print:

1. Problem.
2. Cause.
3. Fix.
4. Evidence/artifact path.

## Automation boundary

GitHub Actions may run hermetic code tests and build the static Pages artifact. Real
harness evaluations, private tasks, paid model calls, and credentialed live-adapter
smokes run locally or on a separately operated benchmark runner. ATV League has a
separate labeled-PR Action for sandboxed submitted-bot matches; League results never
enter the harness-evaluation tracks.

| Lane | Trigger | Coverage |
|---|---|---|
| Unit | Every push | schema, protocol, scheduler, stats |
| Fake E2E | Every push | full trial lifecycle |
| Container security | Local required gate before release | mounts, network, resources, cancellation |
| Cross-platform | Every PR | Linux reference plus Windows/macOS adapter conformance |
| Live adapters | Local/manual operator run | vendor CLI compatibility |
| Task audit | Task PRs | oracle/no-op/alternative/exploit/mutation |
| Reproducibility | Local release candidate gate | rebuild and replay trial bundles |
| Packaging | Every release/PR touching deps | clean pip, uv, wheel, sdist, run extra |
| Governance policy | Local release audit | branch rules, environments, labels, SHA policy |

## Packaging and platform gates

- `pip install -e '.[dev]'`
- `uv sync`
- `uv sync --extra run`
- wheel install into an empty environment
- source-distribution install into an empty environment
- `atv --help`, `submit --help`, `doctor`, `harnesses`, and `games` under UTF-8 and CP1252
- Linux, Windows, and macOS smoke tasks
- CodeClash import and one dummy tournament from a clean checkout

CI passing without the `run` extra does not prove the benchmark path is installable.

## Completion evidence

The benchmark implementation is test-complete only when:

- all schemas have positive/negative fixture corpora;
- every protocol branch maps to a test;
- every failure mode has a typed status and user-facing error;
- security tests execute real containers/microVMs;
- statistical tests use simulation with known truth;
- at least one full official-style trial is independently reproduced;
- no critical silent failure remains.
