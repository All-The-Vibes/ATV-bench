# ATV-Bench: Blueprint for a Credible Harness Benchmark

Status: proposed architecture and execution plan
Audit date: July 19, 2026
Audited branch: `codex/legitimacy-harness-generalization` at base commit `8f71eaf` plus the current uncommitted worktree

## Executive verdict

ATV-Bench is currently two useful prototypes sharing one name:

1. A **community bot league** whose execution is local/external and whose reviewed data
   is published statically.
2. A local **harness experiment runner** that can invoke arbitrary commands and compare generated bots.

Neither is yet a credible general harness benchmark.

The repository stores frozen submitted bots, but GitHub Actions does not execute them. The local
comparison runner executes harnesses, and one harness invocation can produce many downstream games.
Those games measure the strength of one generated bot; they are not independent observations of
harness quality. The current sequential 1500/K=32 Elo and heuristic interval are suitable for an
entertainment league, not a scientific ranking.

The repo should not try to stretch the league into a research benchmark. Keep the league as a
separate product surface and build a new, versioned evaluation system around fresh harness trials,
trusted hidden graders, reproducible task environments, explicit resource budgets, attested
execution, and task-clustered statistics.

The primary benchmark estimand should be:

> The probability that a fresh harness execution solves a previously unseen task under a fixed,
> declared resource budget.

Secondary metrics should explain that outcome: cost, latency, valid-artifact rate, recovery,
verification behavior, security-policy compliance, and downstream artifact quality.

## Repository state and divergence

The current worktree is not based on the current public branch.

| Surface | Commit/state | Important contents |
|---|---|---|
| Local worktree HEAD | `8f71eaf` | Original community league |
| Local uncommitted work | dirty, untracked | Generic process adapter, comparison runner, audits, artifacts |
| Public `origin/main` | `107341de` | Merged PR #14 host harness runner and CodeClash integration |

`origin/main` is 26 commits and 65 files ahead of local HEAD. It now includes:

- `atv-bench run`;
- CodeClash integration and host-side Claude/Copilot adapters;
- repository fingerprinting and expanded runtime metadata;
- a `players.py` build-once cache;
- proof artifacts and a showcase board.

Public-state verification at the audited July 19 base snapshot:

- GitHub CI on `107341de` passed **723 tests, 15 skipped, 12 deselected** using
  `pip install -e '.[dev]'`.
- `uv sync` and `uv sync --extra run` fail because `run = ["codeclash"]` cannot resolve
  CodeClash and the documented `vendor/CodeClash` tree is absent.
- CP1252 Windows terminals crash on `submit --help`, `doctor`, `harnesses`, and `games`
  because user-facing Unicode arrows/checkmarks are written without a fallback.
- `main` had no branch protection or ruleset.
- The documented `league-match` protected environment and `run-match` label did not exist.
- GitHub Actions allowed all actions and did not require SHA pinning.
- There are no releases or tags.
- The public leaderboard remains empty.

The local worktree independently changed several of the same modules. The first implementation phase
must therefore be **reconciliation**, not new runner development. Start from `origin/main`, port only
the validated local changes, and rerun the full audit. Landing the dirty branch directly would likely
revert public-main work or create a hybrid whose behavior no review has validated.

## Current maturity score

These are heuristic engineering scores, not measured benchmark outputs.

| Dimension | Score | Current evidence |
|---|---:|---|
| Claim honesty | 6/10 | README now distinguishes the league from the CodeClash paper |
| Actual harness execution | 4/10 | Public main has an unverified host runner; the public league still runs bots |
| Task diversity | 1/10 | One live Lightcycles arena |
| Experimental design | 2/10 | Side swapping and fresh workspaces exist in one script; no task-by-trial design |
| Statistical rigor | 2/10 | Wilson interval for bot games; scientific leaderboard still uses sequential Elo |
| Reproducibility | 4/10 | Source commits, hashes, diffs, and raw games exist for one comparison |
| Provenance and attestation | 3/10 | Client-side binding exists; trusted harness attestation does not |
| Bot-execution security | 8/10 | Strong untrusted-bot sandbox and privileged workflow separation |
| Harness-execution security | 2/10 | Local process runner inherits host credentials and network |
| Benchmark governance | 2/10 | No immutable releases, task review board, contamination policy, or retraction process |
| Developer experience | 3/10 | Useful CLI shape, but fresh `uv` install and common Windows output are blocked |

Scientific benchmark maturity: **approximately 3/10**.
Community demonstration maturity: **approximately 7/10**.

## Premise challenge

### Premise 1: downstream games equal harness trials

They do not. A harness invocation produces an artifact. Twenty games using that artifact give a
better estimate of the artifact's game strength, but they remain nested under one harness trial.
Treating those games as independent harness observations understates uncertainty.

Correct hierarchy:

```text
task
└── fresh harness trial
    └── generated artifact
        └── tests / games / simulations / rounds
```

For a CodeClash-style iterative task, the tournament is the trial. Its dependent rounds stay nested.

### Premise 2: a fingerprint proves which harness produced the artifact

It does not. The existing fingerprint is self-attested configuration metadata. A user can capture one
configuration and run another, modify the harness after capture, route through a different model, or
omit runtime components the reader does not know about.

### Premise 3: the same requested model string isolates the harness effect

It does not. A model label does not prove the same provider, deployment, snapshot, system prompt,
sampling parameters, context tier, retry policy, routing gateway, or subagent models.

### Premise 4: more skills and agents imply a stronger harness

They do not. Capability only matters when the task activates it. A narrow one-file game can favor a
minimal implementation agent over a richer verification or long-horizon harness.

### Premise 5: one global ranking can compare every harness

It cannot without conflating distinct claims. ATV-Bench needs at least two separate tracks:

- **Controlled harness-effect track:** hold model, task, tools, environment, and budget fixed.
- **Full-system track:** allow each harness to use its preferred stack and label the result as system performance.

These tracks must never share one ranking.

### Premise 6: public tasks can remain a frontier benchmark

They cannot indefinitely. Public tasks become training data, reference solutions leak, and agents
learn benchmark-specific strategies. Public development tasks and private rotating evaluation tasks
must be separate.

## What already exists

| Existing component | Evidence | Disposition |
|---|---|---|
| Trusted Lightcycles engine/referee | `src/atv_bench/arena/` | Keep as one competitive task family |
| Local untrusted bot sandbox | `arena/` + local integration tests | Keep local; never invoke from GitHub Actions |
| Static Pages publication | `.github/workflows/league-deploy.yml` | Keep push-only and data-only |
| Leak-safe local config readers | `src/atv_bench/fingerprint/` | Keep as descriptive metadata only |
| Client provenance binding | `fingerprint/provenance.py` | Keep as tamper evidence; do not call it attestation |
| Generic command adapter | `adapters/contract.py` | Keep as development transport; replace with versioned protocol for scored runs |
| Public host harness runner | `origin/main:src/atv_bench/runner.py` | Reuse orchestration concepts; replace host trust boundary |
| CodeClash integration seam | `origin/main:src/atv_bench/integration.py` | Keep behind a clean adapter boundary |
| Build-once player cache | `origin/main:src/atv_bench/players.py` | Remove for iterative track; redesign cache key for frozen-artifact tasks |
| Committed/staged/untracked diff capture | `capture_repo_diff` | Reuse with output-tree hashing and size limits |
| Side-swapped deterministic bot series | `comparison.py` | Keep as nested artifact evaluation |
| Repository comparison artifacts | `comparisons/` | Keep as case-study evidence, not leaderboard evidence |
| Strong regression suite | `tests/` | Reuse patterns; add protocol, sandbox, task, and statistical conformance suites |
| Static leaderboard viewer | `leaderboard/` | Keep for league; scientific reports need uncertainty and task breakdowns |
| Old local-harness plan | `IMPLEMENTATION_PLAN.md` | Supersede; several assumptions are contradictory or stale |

## Current architecture

Public main now has two separate execution paths.

```text
Local unverified harness path
  claude/copilot CLI on host
  full host environment/network
  build one bot per player
  process-wide cache replays frozen artifact for every configured round
  CodeClash arena evaluates bots
  result forced verified=false

Public community league path
Contributor machine
  fingerprint config
  build bot externally
  submit bot + self-attested metadata
             |
             v
GitHub pull request
             |
             v
Ordinary CI/security tests only
  never run bot/harness/model/evaluation
             |
             v
Reviewed submission/result data on main
             |
             v
Push-only Pages workflow
  recompute static board from committed data
```

The local path executes harnesses but is self-attested, host-coupled, and not paper-faithful: its
`--rounds` setting does not cause fresh edit-feedback cycles because the player cache replays one
artifact. The public repository path no longer executes bots at all; any League execution remains
outside GitHub Actions and must enter the store as reviewed data.

## Current-state gap matrix

| Area | Current state | Credible target | Severity |
|---|---|---|---|
| Unit of evaluation | Submitted bot or one local build | Fresh harness trial from clean snapshot | Critical |
| Tracks | League and research claims overlap | Controlled and full-system tracks, plus separate league | Critical |
| Harness protocol | Python dataclasses plus environment variables | Versioned manifest, request, JSONL events, result schema | Critical |
| Adapter neutrality | Public runner hard-codes Claude Code and Copilot CLI | Third-party manifest integrates without core-code changes | Critical |
| Runtime isolation | Local host process | Ephemeral container or microVM with explicit policy | Critical |
| Host environment | Full process environment inherited by command adapter | Empty home plus explicit environment allowlist | Critical |
| Model identity | Requested label or parsed event | Provider-attested resolved model/deployment plus request IDs | Critical |
| Credentials | Ambient local authentication | Short-lived brokered credentials scoped to one trial | Critical |
| Network | Host network or bot `--network none` | Per-task allowlist with auditable gateway | Critical |
| Task suite | One game | Multiple independent task families | Critical |
| Hidden grading | None for harness tasks | Trusted grader mounted only after harness exit | Critical |
| Task validation | Game engine tests | Oracle, no-op, exploit, mutation, and human review | Critical |
| Repetition | One or two harness builds | Minimum five fresh trials per cell | Critical |
| Randomization | Partial side swapping | Paired randomized blocked scheduling | High |
| Budgets | Mostly wall timeout; token fields declarative | Enforced time, token, cost, calls, CPU, memory, storage | Critical |
| Adaptation | Frozen artifact | Persistent multi-round workspace and trusted feedback where task requires it | High |
| Public-main rounds | `--rounds` with build-once replay | Explicit frozen-build mode or real edit-feedback rounds | Critical |
| Cache identity | Omits model, budget, source tree, harness config, adapter version | Full trial identity or no cross-trial cache | Critical |
| Public-main change detection | Plain `git diff` can miss committed/untracked work | Snapshot/output-tree capture | Critical |
| Installability | `run = ["codeclash"]` while CodeClash is not on PyPI and no vendor tree is tracked | Digest-pinned, installable dependency or isolated runner image | Critical |
| Provenance | Client digest/HMAC | in-toto/SLSA-style runner attestation | Critical |
| Result storage | Git JSON and local files | Immutable content-addressed trial bundles | High |
| Artifact capture | Untracked paths read directly and may follow links | Descriptor-based regular-file capture with link/special-file rejection | Critical |
| Privileged publisher | Proceeds when PR-author API lookup is unavailable | Identity and executor evidence must fail closed | Critical |
| Supply chain | Actions referenced by movable major tags | Full action commit SHAs plus policy check | High |
| Statistics | Sequential Elo and game-level Wilson interval | Task-clustered paired effects; hierarchical models; BT for tournaments | Critical |
| Winner rule | Highest Elo or raw wins | Preregistered practical margin and uncertainty gate | Critical |
| Contamination | Public code and canary-like fingerprints | Private rotating eval tasks, incident and retraction policy | Critical |
| Versioning | No release/tag | Immutable benchmark, runner, task, and protocol versions | High |
| Leaderboard identity | GitHub login accumulates changing bots/configs | Immutable harness digest + adapter + model policy | Critical |
| Governance | Maintainer discretion | Task review, conflicts, appeals, audit log, score retraction | High |
| DX | Bespoke setup scripts | `atv harness validate/run` with one manifest and actionable errors | High |
| Portability | Windows fixes are reactive | Linux reference runner plus Windows/macOS conformance lanes | Medium |
| Fresh installation | Public `uv sync` cannot resolve `codeclash` | Released runner installs from a clean checkout | Critical |
| Windows CLI | Several normal commands crash under CP1252 | ASCII/encoding-safe output covered in CI | Critical |
| Live governance | Written controls are not enabled in GitHub settings | Protected main, required checks, protected environment, review policy | Critical |

## Parallel review consensus

Four independent research lanes reviewed the repo: current-state/code, methodology/statistics,
security/provenance, and product/DX/governance.

| Dimension | Current-state | Methodology | Security | Product/DX | Consensus |
|---|---|---|---|---|---|
| Is this a credible harness benchmark today? | No | No | No | No | Confirmed |
| Keep the bot league separate? | Yes | Yes | Yes | Yes | Confirmed |
| Fresh harness trial is the unit? | Yes | Yes | Yes | Yes | Confirmed |
| Current host runner is safe for public scoring? | No | No | No | No | Confirmed |
| One Lightcycles task supports broad claims? | No | No | No | No | Confirmed |
| Sequential Elo is valid scientific ranking? | No | No | No | No | Confirmed |
| Trusted hidden grading is required? | Yes | Yes | Yes | Yes | Confirmed |
| Current provenance proves execution/model identity? | No | No | No | No | Confirmed |
| Current governance is launch-ready? | No | No | No | No | Confirmed |
| First implementation step is reconciliation? | Yes | Compatible | Compatible | Yes | Confirmed |

## Specific contradictions and evidence defects

### 1. Public `--rounds` is not iterative harness adaptation

`origin/main` caches one generated artifact by `(player_id, game, prompt_version)` and replays it for
later round callbacks. The cache omits model, budget, source tree, harness configuration, and adapter
version. The CLI presents rounds as tournament rounds, but they do not create repeated
edit-competition-feedback cycles.

### 2. The real-run installation path is broken

`pyproject.toml` declares `run = ["codeclash"]` and comments that CodeClash is vendored under
`vendor/CodeClash`. No such tracked tree exists. `uv sync` resolves all project extras and fails before
creating a usable environment. CI passes because it uses `pip install -e '.[dev]'`, which never tests
the documented real-run path.

### 3. Same-model parity is unproven

Public tournament summarization labels the requested model string as `parsed`. It does not prove the
resolved provider deployment, snapshot, sampling policy, retries, or subagent models.

### 4. Public comparison artifacts do not currently verify

The local comparison hashes the in-memory LF patch, then writes the patch through platform text-mode
newline conversion. All non-empty saved patch files have a different SHA-256 than the recorded
`diff_sha256`. The summarizer also mutates original run documents when adding expanded games.

Disposition: label the Phoenix/hve material an experimental case study, fix the evidence format, and
never ingest it into an official benchmark.

### 5. Actions must not become the benchmark runner

Repository governance now treats Actions as test and static-publication infrastructure only.
Submitted bots, harnesses, model calls, trials, and benchmark evaluations belong in explicit local
or separately approved runners, never in pull-request or chained Actions workflows.

### 7. Evidence and UI claims drift

- Documentation says insufficient signal is suppressed, while the viewer still renders rank/Elo with a warning.
- Documentation calls public logs the dispute mechanism, but the workflow retains only result/meta artifacts for
  seven days and `logs_url` may point to the repository homepage.
- The public README still says "that's what we rank" even though the production league runs frozen bots.
- League docs still describe CodeClash reuse while NOTICE correctly describes a separate implementation.

## Target product model

ATV-Bench should expose four clearly separated products:

### 1. ATV League

- Existing public bot competition.
- Entertainment and community signal.
- Online Elo allowed.
- No claim that it measures harness quality.

### 2. ATV Controlled

- Scientific harness-effect comparison.
- Same model snapshot, task, budget, tools, environment, and schedule.
- Primary score: fresh-trial task success.
- Pairwise task-clustered effects with uncertainty.

### 3. ATV Systems

- Full preferred harness stack.
- Model, tools, subagents, memory, and policy may vary.
- Report system performance, cost, and latency.
- Never interpreted as a causal harness-only effect.

### 4. ATV Resilience

- Injected failures, tool denial, malformed feedback, flaky tests, context pressure, and recovery.
- Measures self-healing, verification, rollback, retry discipline, and silent-failure avoidance.

## Reference benchmark lessons

| Project | Pattern to adopt | Pattern to avoid |
|---|---|---|
| CodeClash | Persistent edit-compete-feedback tournaments; randomized positions; Bradley-Terry analysis | Treating dependent rounds as independent |
| SWE-bench | Immutable base commits; fail-to-pass and pass-to-pass tests; human-validated task tiers | Assuming a one-time verification permanently solves contamination/test quality |
| Terminal-Bench + Harbor | Small agent API; task/environment/trial abstraction; hidden grader after execution; multiple attempts | Public permanent graders as the only anti-contamination control |
| RE-Bench | Multiple time budgets; continuous/partial credit; human baselines; best-of-k under fixed total budget | One arbitrary timeout as the capability claim |
| HELM | Separate scenarios, adaptations, and metric dimensions; transparent multi-metric reporting | One opaque aggregate that hides tradeoffs |
| Inspect AI | Structured transcripts, extension packages, sandbox abstraction, scanners | Vendor-specific orchestration embedded in benchmark core |
| lm-evaluation-harness | Small adapter surface and config-defined tasks | Requiring core-code edits for each harness |
| LiveCodeBench | Rolling time-bounded releases and explicit versions | Permanent static public task pool |

Operationally, Harbor is the closest model for ATV Controlled: task, agent, environment, and trial are
first-class objects. CodeClash is the closest model for iterative competitive tasks. Neither alone
covers recovery, verification, and long-horizon harness behavior, so ATV Resilience remains a
distinct task family.

## Dream-state delta

Today, ATV-Bench asks users to trust that a bot and fingerprint came from the claimed harness. The
dream state allows any evaluator to verify:

1. The exact harness source and executable/image.
2. The exact model policy and gateway-observed calls.
3. The exact task, prompt, workspace, tools, budget, and execution order.
4. The complete normalized trajectory and output-tree digest.
5. The trusted hidden-grader result.
6. The analysis code and accepted trial set that produced the report.

The delta is not primarily more games or a prettier leaderboard. It is moving the harness, model
gateway, grader, and evidence into one verifiable trial graph.

## Target architecture

```text
                         TRUSTED CONTROL PLANE

Benchmark registry
  task versions
  harness manifests
  model/budget policies
          |
          v
Randomized scheduler ---------------> Trial ledger
          |                            run id / order / worker / seed
          v
Credential broker
  one-trial token
  model gateway route
          |
          v
+-----------------------------------------------------------------------+
| EPHEMERAL UNTRUSTED TRIAL ENVIRONMENT                                 |
|                                                                       |
| read-only task image + clean repo snapshot                            |
| writable workspace                                                    |
| harness adapter/runtime                                               |
| declared tools and MCP servers                                        |
| allowlisted model-gateway egress only                                 |
| resource controller: time/tokens/cost/calls/cpu/memory/storage        |
|                                                                       |
| request -> JSONL trajectory -> output artifact                        |
+-----------------------------------------------------------------------+
          |
          | artifact + trajectory + runtime metadata
          v
Attestor
  base image digest
  harness image/binary digest
  task/base-tree/prompt/policy digests
  output-tree/artifact digests
          |
          v
Trusted hidden grader
  private tests mounted after harness exit
  no harness credentials
          |
          v
Immutable result store
  content-addressed trial bundle
          |
          v
Analyzer
  task-level paired effects
  reliability/cost/recovery metrics
  bootstrap/hierarchical analysis
          |
          v
Versioned report + leaderboard
```

## Benchmark tracks and estimands

### Controlled harness-effect track

Design:

```text
task × harness × model snapshot × budget × independent trial
```

Hold constant:

- exact model provider, deployment, and snapshot;
- model parameters and context tier;
- task prompt and repository snapshot;
- tool availability and network policy;
- task image and hardware class;
- wall-time, model-token, model-call, and cost budget;
- scheduling block and hidden grader.

Only intrinsic harness behavior may differ: orchestration, prompt strategy, memory, verification,
recovery, tool selection, subagent coordination, and internal policies.

### Full-system track

Allow preferred:

- model and provider;
- plugins, skills, MCPs, and subagents;
- memory and context systems;
- retries and model routing;
- task-specific setup.

The result means "this complete system performed this well at this cost." It does not isolate a
harness effect.

## Submission and trust tiers

1. **Local self-attested:** unlimited, useful for development, never ranked.
2. **Community reproducible:** complete public bundle and replay, visibly unofficial.
3. **Official attested:** benchmark-run, signed, eligible for scientific reports.
4. **Independently reproduced:** official bundle rerun by a separate operator.

Official and self-hosted results must never share the same rank table.

### Iterative adaptation track

For CodeClash-style tasks:

- persistent codebase for one tournament;
- fresh model context each round unless the harness explicitly provides memory;
- trusted competition feedback written between rounds;
- randomized positions;
- tournament is the independent trial;
- rounds are nested trajectory evidence.

## Experimental unit and scheduling

One trial is:

> One new harness process in one clean writable workspace, starting from the same immutable task
> snapshot, producing one candidate artifact and one trusted grader result.

Minimum credible pilot:

- at least 50 independent tasks for broad claims;
- at least five fresh trials per task/harness/model/budget cell;
- at least two exact model snapshots;
- paired harness execution on every task;
- randomized order within task/model/budget blocks;
- interleaved execution across time and workers;
- no shared caches, conversations, workspaces, or generated artifacts.

The task/trial counts are operational starting points, not universal laws. Run a pilot, estimate
between-task and within-cell variance, preregister the desired interval width or equivalence margin,
then size the final study from that precision target.

Research-grade target:

- 10 to 20 trials per cell or sampling until a preregistered precision target;
- three or more model families;
- multiple budget regimes;
- independent replication by a second runner/operator.

## Task portfolio

The suite must exercise capabilities that distinguish real harnesses.

| Category | Capability activated | Example grader |
|---|---|---|
| Localized repair | diagnosis and precise editing | hidden fail-to-pass + pass-to-pass tests |
| Cross-file feature | planning, implementation, integration | private functional and regression tests |
| Ambiguous repository task | context gathering and assumption control | rubric plus hidden behavioral tests |
| Test/build debugging | log interpretation and iteration | reproducible broken build with private checks |
| Long-horizon refactor | planning, memory, progress tracking | structural checks + regression suite + partial credit |
| Dependency/environment failure | tool use and environment repair | container state and executable checks |
| Injected tool failure | retry, fallback, recovery | fault controller verifies recovery path |
| Verification discipline | objective completion signals | hidden trap where plausible output is wrong |
| Context pressure | retrieval and memory | large repo with hidden dependency-sensitive tests |
| Security-constrained task | policy compliance | success plus zero forbidden actions |
| Terminal/operations | shell and service orchestration | Harbor-style container grader |
| Competitive optimization | multi-round adaptation | tournament result with trusted feedback |

No single category should dominate the overall score. Publish category-specific effects before any
aggregate.

## Task package contract

Proposed layout:

```text
task/
  task.yaml
  prompt.md
  public/
    repo.bundle
    visible-tests/
  trusted/
    grader-image.lock
    hidden-tests/
    oracle.patch
    exploit-cases/
  metadata/
    license.json
    provenance.json
    contamination.json
    reviewers.json
```

Required `task.yaml` fields:

- `schema: atv.task/v1`
- immutable task id and semantic version;
- category and difficulty;
- source repository and base-tree digest;
- task image digest and architecture;
- prompt digest;
- allowed tools and network policy;
- output artifact contract;
- wall/token/cost/call/CPU/memory/storage limits;
- deterministic grader command;
- partial-credit schema if applicable;
- oracle and no-op expected results;
- minimum supported harness protocol;
- license and redistribution posture.

Task acceptance requires:

1. Oracle passes.
2. No-op fails.
3. Existing regressions stay green.
4. At least one alternative correct solution passes.
5. Exploit agents cannot pass without satisfying intent.
6. Grader is deterministic across repeated runs.
7. Task author and independent reviewer agree the specification and grader align.
8. Hidden tests remain inaccessible during the harness phase.
9. Mutation tests demonstrate the grader catches representative wrong solutions.

## Harness protocol

The existing process adapter is a useful prototype, but scored runs need a versioned protocol.

### Harness manifest

```yaml
schema: atv.harness/v1
id: example-harness
version: 1.2.3

runtime:
  kind: oci
  image: ghcr.io/example/harness@sha256:...
  entrypoint: ["/opt/harness/bin/run"]

protocol:
  version: 1
  input: stdin-json
  output: stdout-jsonl

capabilities:
  workspace_edit: true
  model_selection: true
  token_budget: observed
  cost_budget: enforced
  subagents: true
  resumable: false

security:
  env_allowlist: [MODEL_BROKER_TOKEN]
  network: model-gateway-only
  writable_paths: [/workspace, /artifacts]
```

### Trial request

Required fields:

- protocol and benchmark version;
- trial, task, harness, model-policy, and schedule ids;
- clean workspace and base-tree digest;
- task prompt and digest;
- exact budget;
- allowed tools, paths, and network destinations;
- random seed and order assignment;
- expected output artifact contract;
- credential handles, never secret values.

### JSONL events

Required event types:

- `hello`: protocol and capability negotiation;
- `status`: lifecycle transitions;
- `model_call`: provider request id, resolved model, usage, retry;
- `tool_call`: normalized tool name, timing, outcome, policy decision;
- `checkpoint`: optional resumable state;
- `artifact`: path, media type, size, digest;
- `usage`: cumulative observed/enforced budget;
- `error`: typed failure and recovery state;
- `result`: terminal status and output-tree digest.

Stdout contains protocol events only. Human-readable logs go to stderr and are stored separately.

### Result statuses

At minimum:

- `success`
- `no_edit`
- `invalid_artifact`
- `task_timeout`
- `model_unreachable`
- `auth_failed`
- `policy_denied`
- `budget_exhausted`
- `harness_crash`
- `grader_failed`
- `infrastructure_error`
- `cancelled`

Infrastructure errors must not count as harness losses.

## Isolation and trust boundaries

### Zone A: untrusted task and harness execution

- single-use microVM for hostile public workloads; rootless container/gVisor can serve lower-trust local tiers;
- immutable image digest;
- non-root user;
- read-only root;
- explicit writable mounts;
- CPU, memory, process, file-size, disk, and wall limits;
- process-tree cancellation;
- no host Docker socket;
- no cloud metadata endpoint;
- no ambient home directory;
- no inherited plugins, skills, hooks, or MCP servers;
- egress denied except an authenticated model gateway and task-specific allowlist.

### Zone B: credential and model gateway

- short-lived trial-scoped token;
- provider secret never enters the harness environment;
- request id and resolved model recorded;
- token/cost/call budgets enforced centrally;
- retry and routing policy fixed by the benchmark track;
- payload retention and privacy policy explicit.

### Zone C: trusted grader

- runs after harness credentials are destroyed;
- reads output artifact as data;
- mounts private tests only after harness exit;
- never executes submission-provided grader code with privilege;
- emits schema-validated signed result.

### Zone D: analyzer and publisher

- consumes only attested trial bundles;
- recomputes aggregates from immutable raw trials;
- never trusts caller-supplied score, identity, model, or `verified` fields;
- publishes benchmark version, exclusions, and uncertainty.

Official evaluation intake must fail closed on identity ambiguity and require an approved executor
signature, run nonce, task digest, and attempt id. GitHub Actions is not that executor.

## Provenance and attestation

Client HMAC binding is not enough. A credible result needs a trusted runner statement whose subject is
the output artifact or output-tree digest.

Use an in-toto/SLSA-style predicate with:

### Build definition

- benchmark protocol version;
- task id/version and task-image digest;
- harness manifest/version and runtime digest;
- model policy id;
- prompt and base-tree digests;
- allowed tools, network, and resource policy;
- scheduler order, seed, and worker class;
- resolved dependencies.

### Run details

- trusted runner identity and version;
- start/end timestamps;
- trial id;
- actual runtime image and binary digests;
- resolved model/provider/request ids;
- usage and budget enforcement;
- retries and cancellations;
- output-tree and artifact digests;
- trajectory and log digests;
- grader image/version/result digest;
- infrastructure error classification.

Artifact capture must reject symlinks, junctions, hardlinks to undeclared inodes, device files,
sockets, absolute paths, and `..` traversal. The current local diff capture reads untracked paths
with `Path.read_bytes()`, which can follow a link outside the workspace.

Trust tiers:

- `local-self-attested`
- `community-reproducible`
- `official-attested`
- `independently-reproduced`

Never accept a submitter-provided `verified: true`.

## Metrics

### Primary metric

Single-run task success at a fixed, declared budget.

For partial-credit tasks, use a preregistered task-specific score normalized to `[0, 1]`.

### Reliability metrics

- valid artifact rate;
- no-edit rate;
- harness crash rate;
- task timeout rate;
- infrastructure error rate;
- regression rate;
- successful recovery after injected failure;
- silent-failure rate.

### Efficiency metrics

- wall time;
- model input/output/cached tokens;
- model calls;
- dollar cost;
- tool calls;
- subagent count;
- CPU time and peak memory;
- bytes read/written;
- network requests.

### Process metrics

- verification actions;
- tests run before completion;
- rollback/retry count;
- context retrieval volume;
- policy denials;
- trajectory length.

Process metrics explain outcomes. They do not replace correctness.

## Statistical analysis

### Minimum credible v1

For two harnesses:

1. Compute success within each task across fresh trials.
2. Compute paired task-level differences.
3. Bootstrap tasks, not games, rounds, tool calls, or tests.
4. Report effect and 95% interval.
5. Use an exact paired permutation or sign test as a secondary check.
6. Predefine a practical equivalence margin.
7. Report `inconclusive` when uncertainty crosses the equivalence region.

### Research-grade v2

Use a hierarchical model:

```text
success ~ harness + model + budget
        + harness:model
        + harness:task_category
        + execution_order + worker + day
        + task random effect
```

For tournaments, use Bradley-Terry or a tie-aware extension with task/arena/side effects and
tournament-clustered bootstrap.

Publish:

- pairwise effects and intervals;
- per-category effects;
- cost and time curves;
- rank probability;
- bootstrap rank distribution;
- pairwise-order stability;
- sensitivity by tasks, models, budgets, dates, and exclusion rules.

### Winner rule

Name a winner only when:

1. The controlled-track effect exceeds a preregistered practical margin.
2. The uncertainty interval excludes zero and the equivalence region.
3. Direction is stable under task resampling.
4. The result is not explained only by crashes/no-edits.
5. Direction persists on at least two model snapshots.
6. No unresolved contamination or grader incident exists.

Otherwise publish one of:

- statistically indistinguishable;
- more reliable, but not higher-quality;
- better in this category/budget only;
- inconclusive due to infrastructure or sample size.

Sequential Elo remains available only for ATV League and must be labelled as online league Elo.

## Benchmark versioning and contamination

Every public result must name:

- benchmark version;
- task-set version;
- protocol version;
- runner version;
- harness version/digest;
- model policy/version;
- evaluation date.

Version policy:

- immutable patch versions for task/grader corrections;
- old results never silently rewritten;
- corrected versions get a new leaderboard;
- invalidated tasks and scores remain in an incident log;
- public development tasks never count toward the official private score;
- private tasks rotate on a declared cadence;
- canaries are supplemental, not the primary defense;
- contamination reports trigger investigation and possible score retraction.

Recommended task split for a release:

- 25% public development tasks;
- 60% private evaluation tasks;
- 15% newly rotated time-based tasks.

Retire and publish old hidden tasks after two release cycles, then exclude them from future official
scores.

## Governance

Required roles:

- benchmark maintainer;
- task reviewer;
- security reviewer;
- statistics reviewer;
- release manager;
- appeals/reproduction reviewer.

Required policies:

- contributor conflict disclosure;
- task author cannot be sole reviewer;
- harness maintainers can challenge grader behavior;
- raw attested trial bundle available for disputes;
- time-bounded appeal process;
- public incident and score-retraction log;
- reproducible release archive;
- independent rerun before major leaderboard claims;
- no maintainer-owned harness receives private task access.

Suggested operating rules:

- maximum two official submissions per harness per release;
- reruns only for confirmed infrastructure faults;
- two reviewers for new tasks and adapters;
- top three plus a random 10% of submissions receive manual audit;
- raw sealed evidence retained 180 days;
- sanitized public evidence retained indefinitely;
- 14-day dispute window;
- initial response within five business days;
- independent reviewer and one appeal;
- quarterly benchmark releases with a published cost/capacity forecast.

## Developer journey

Primary personas:

1. Harness author adding an adapter.
2. Benchmark maintainer adding tasks.
3. Researcher running controlled experiments.
4. Engineer reading or reproducing a score.

| Stage | Current experience | Target |
|---|---|---|
| Discover | README mixes league and benchmark concepts | Choose League, Controlled, Systems, or Resilience |
| Install | Clone, uv, Docker, external CLIs, bespoke setup | `uvx atv-bench doctor` plus one runner image |
| Describe harness | Reader code or command argv | One `atv.harness/v1` manifest |
| Validate | Fingerprint validation only | `atv harness validate` runs full conformance |
| Run smoke | Demo bot or custom script | `atv trial smoke --harness X --task Y` |
| Run benchmark | Target-specific Python scripts | `atv eval run --suite pilot-v1 --harness X` |
| Diagnose | Raw CLI output and local files | Typed failure + problem/cause/fix + artifact link |
| Publish | Reviewed data + push-only Pages | Signed result bundle upload or official runner |
| Reproduce | Manual repo checkout | `atv eval reproduce <trial-id>` |

Current estimated time-to-first-real-comparison: **30 to 90 minutes**.
Target for local smoke: **under 5 minutes**.
Target for official run submission: **under 10 minutes excluding queue time**.

Developer empathy narrative:

> I should not need to understand ATV-Bench internals, copy my personal config, or edit Python
> registries just to evaluate a harness. I want one manifest, one validation command, one smoke task,
> and an artifact that tells me exactly what failed and how to reproduce it.

DX implementation checklist:

- [ ] One manifest creates a process or OCI adapter.
- [ ] No core-code edit is required for a new harness.
- [ ] Every error includes problem, cause, fix, and evidence path.
- [ ] Clean Windows/Linux/macOS smoke runs are documented and tested.
- [ ] Official vs local trust tier is visible in every command and report.
- [ ] `eval reproduce` is one command.
- [ ] Upgrade/migration policy exists for every protocol version.

## DX scorecard

| Dimension | Current | Target |
|---|---:|---:|
| Installation | 5/10 | 9/10 |
| Time to hello world | 4/10 | 9/10 |
| CLI discoverability | 6/10 | 9/10 |
| Adapter contribution | 4/10 | 9/10 |
| Error actionability | 6/10 | 9/10 |
| Reproduction workflow | 3/10 | 9/10 |
| Version/upgrade safety | 2/10 | 9/10 |
| Evidence transparency | 6/10 | 10/10 |

## Error and rescue registry

| Failure | Classification | Score impact | Required rescue |
|---|---|---|---|
| Model provider outage | Infrastructure | None | Retry under preregistered policy or reschedule |
| Credential broker failure | Infrastructure | None | Destroy trial and requeue |
| Harness process crash | Harness | Failure | Preserve logs, exit/signal, artifact state |
| Task timeout | Harness or policy | Failure | Kill full process tree, record budget state |
| Grader crash | Infrastructure | None | Rerun grader against same immutable artifact |
| Invalid artifact | Harness | Failure | Schema error with path and fix |
| Budget telemetry missing | Harness/protocol | Flagged | Mark usage unknown; exclude cost ranking |
| Resolved model mismatch | Policy | Invalid trial | Abort before grading |
| Task image mismatch | Infrastructure | Invalid trial | Refuse run |
| Hidden grader leak | Benchmark incident | Invalidate | Retract affected tasks/results and rotate |
| Untrusted artifact parse exploit | Security | None | Quarantine, patch parser, rerun unaffected trials |
| Runner attestation missing | Trust | Unofficial only | Never publish as official |

## Failure modes registry

| Codepath | Failure mode | Test exists now? | Error handling now? | Silent? | Target |
|---|---|---:|---:|---:|---|
| Process adapter | Child descendants survive timeout | Yes - process-tree and OCI timeout tests | Full-tree kill plus absence verification | No | Keep cross-platform timeout tests required |
| Process adapter | Huge diff/stdout exhausts memory | Yes - streamed bounded-tail and artifact-cap tests | Bounded streaming and typed rejection | No | Keep limits in conformance |
| Process adapter | Full inherited environment exposes host secrets | Yes - deny-by-default canary tests | Empty environment plus explicit allowlist | No | Keep canary regression |
| Protocol | Harness pollutes stdout | Yes - JSONL and interactive adversarial tests | Strict framing, limits, and fail-closed cleanup | No | Keep protocol conformance |
| Model identity | Requested label differs from actual route | Yes - broker and attestation tamper tests | Trial invalid/unofficial unless gateway evidence verifies | No | Live provider evidence remains a launch gate |
| Budget | Token/cost fields declared but unenforced | Yes - typed terminal and atomic reservation tests | Central broker enforcement | No | Keep concurrency tests |
| Workspace | Harness reads ambient home/config | Yes - environment and OCI mount-policy tests | Empty home/config plus exact mount inspection | No | Keep runtime inspection |
| Plugin package | Windows symlink pointer disables hooks | Yes - symlink escape and manifest confinement tests | Reject or materialize before execution | No | Keep Windows regression |
| Scheduler | Provider drift correlates with harness order | Yes - paired randomized scheduler tests | Balanced blocked schedule | No | Keep immutable schedule evidence |
| Task grader | Reference-specific tests reject valid solution | Yes - alternative/exploit/mutation/no-op suite | Task is ineligible when any gate fails | No | Human review remains separate |
| Hidden tests | Harness reads grader before completion | Yes - unit and real-Docker late-mount tests | Separate grader starts only after verified harness removal | No | Keep lifecycle receipt check |
| Provenance | Submitter forges runtime/model metadata | Yes - DSSE role/binding/tamper tests | Official admission independently verifies signed bindings | No | Official signed run remains separate |
| Publisher | PR author lookup fails but artifact is accepted | Yes - explicit fail-open regression tests | Publication stops on lookup failure or mismatch | No | Keep required workflow check |
| Supply chain | Action major tag moves to compromised code | Yes - workflow supply-chain audit | Only approved full commit SHAs accepted | No | Keep repository Actions policy |
| Statistics | Nested games counted as independent trials | Yes - construction and analysis tests | Trial schema and clustered analysis reject nesting | No | Keep report gate |
| Results | Corrected task silently changes old score | Yes - immutable identity and bundle rewrite tests | New task version plus append-only incident/retraction path | No | Live release evidence remains separate |
| Credentials | Secret leaks through logs/tool environment | Yes - broker, environment, and canary tests | Opaque handles, bounded evidence, and fail-closed scans | No | Keep live secret scan |
| Publisher | Untrusted bundle influences privileged code | Yes - bundle/store/export tamper tests | Content-addressed parsing and independent verification | No | Keep official admission boundary |

Critical gaps are the rows with no test, no complete error handling, and a silent failure path.

## Test strategy

The detailed test plan is in
[`HARNESS_BENCHMARKING_TEST_PLAN.md`](HARNESS_BENCHMARKING_TEST_PLAN.md).

Required test layers:

1. Schema and protocol unit tests.
2. Black-box adapter conformance.
3. Task/grader validation.
4. Sandbox and credential isolation.
5. Scheduler randomization and retry semantics.
6. Provenance and attestation verification.
7. Statistical calibration and clustered-bootstrap tests.
8. End-to-end fake-model trials.
9. Gated live vendor-adapter trials.
10. Adversarial exploit and prompt-injection tests.
11. Cross-platform packaging tests.
12. Reproduction from released trial bundles.

## Phased roadmap

### Phase -1: reconcile with public main

Human team: 2 to 5 days. AI-assisted: 1 to 2 days.

Deliverables:

- Create a fresh branch from `origin/main@107341de`.
- Port the blueprint, test plan, generic protocol concepts, and comparison case study deliberately.
- Do not overwrite public-main runner/integration code with stale local versions.
- Resolve overlapping CLI, adapter, fingerprint, README, and test changes.
- Fix comparison patch hashes and mark case-study bundles immutable.
- Verify the actual install path for the CodeClash dependency.
- Fix CP1252/legacy-Windows output and add encoding CI.
- Enable and verify branch protection, required checks, action policy, and the league-match environment.
- Re-run hermetic, integration, live-smoke, security, and packaging checks.

Exit gate:

- The analysis describes one coherent current tree.
- No local feature is lost and no public-main feature is silently reverted.
- `atv-bench run` installs and starts from a clean checkout.

### Phase 0: separate the products and freeze claims

Human team: 1 week. AI-assisted: 1 to 2 days.

Deliverables:

- Rename current board to ATV League.
- Add explicit Controlled, Systems, and Resilience product definitions.
- Freeze the current online Elo schema as `league/v1`.
- Archive `IMPLEMENTATION_PLAN.md` as historical.
- Publish benchmark charter, estimands, winner rule, and NOT-in-scope list.
- Make privileged identity verification fail closed.
- Pin third-party GitHub Actions to full commit SHAs.

Exit gate:

- No public page calls a submitted-bot score a harness benchmark.
- Every score names its track and version.

### Phase 1: protocol and local conformance

Human team: 2 to 4 weeks. AI-assisted: 4 to 7 days.

Deliverables:

- `atv.harness/v1`, `atv.task/v1`, event, result, and trial-bundle JSON Schemas.
- Manifest loader and capability negotiation.
- Strict JSONL parser.
- Process and OCI adapter implementations.
- Black-box conformance suite.
- `atv harness validate`, `atv trial smoke`, and actionable errors.

Exit gate:

- A new harness integrates without modifying ATV-Bench Python.
- Process and OCI adapters produce equivalent canonical results.

### Phase 2: trusted runner, broker, and attestation

Human team: 4 to 6 weeks. AI-assisted: 1 to 2 weeks.

Deliverables:

- Rootless container or microVM runner.
- Model credential broker and gateway.
- Central time/token/cost/call budget controller.
- Egress allowlist.
- Process-tree cancellation and cleanup.
- Content-addressed trial bundle.
- in-toto/SLSA-style attestation.
- Trusted post-run hidden grader.

Exit gate:

- Harness never receives provider secret.
- Hidden tests are inaccessible during the harness phase.
- Official results cannot be forged from client fields.

### Phase 3: credible pilot task suite

Human team: 6 to 10 weeks. AI-assisted: 2 to 4 weeks plus human review.

Deliverables:

- At least 50 independently reviewed tasks.
- At least five task categories.
- Oracle/no-op/alternative/exploit/mutation validation.
- Public development and private evaluation splits.
- Fault-injection Resilience subset.
- Optional CodeClash tournament subset.

Exit gate:

- Every task passes the validation checklist.
- Independent reviewers approve specification/grader alignment.

### Phase 4: scheduler, analysis, and reports

Human team: 3 to 5 weeks. AI-assisted: 1 to 2 weeks.

Deliverables:

- Paired randomized blocked scheduler.
- Five fresh trials per cell minimum.
- Task-clustered bootstrap and paired tests.
- Hierarchical analysis package.
- Cost/time curves and failure taxonomy.
- Versioned report UI with raw bundle links.
- Online Elo confined to League.

Exit gate:

- Synthetic calibration datasets recover known effects.
- Ranking stability and uncertainty are visible.
- Winner rule is enforced automatically.

### Phase 5: governance and public launch

Human team: 4 to 8 weeks plus ongoing operations.

Deliverables:

- Task review board and conflict policy.
- Benchmark release process.
- Contamination, incident, appeal, and retraction policies.
- Independent reproduction.
- Maintainer runbooks and capacity/cost model.

Exit gate:

- At least one external team reproduces the pilot.
- Security and statistics reviews are signed off.
- No critical gap remains in the launch checklist.

## Implementation backlog

| ID | Priority | Work | Depends on | Acceptance evidence |
|---|---|---|---|---|
| RECON-001 | P0 | Rebase/port onto current public main | - | One coherent reviewed tree |
| RECON-002 | P0 | Fix comparison patch digests and immutable bundles | RECON-001 | Hash verifier green |
| RECON-003 | P0 | Make CodeClash runner installable from clean checkout | RECON-001 | Clean install + smoke |
| SPEC-001 | P0 | Define tracks and estimands | - | Charter approved |
| SPEC-002 | P0 | Define trial as independent unit | SPEC-001 | Schema and analysis use trial id |
| SPEC-003 | P0 | Define winner/equivalence rule | SPEC-001 | Automated report gate |
| PROTO-001 | P0 | Harness manifest schema | SPEC-001 | Invalid manifests rejected |
| PROTO-002 | P0 | Trial request/result schemas | PROTO-001 | Round-trip fixtures |
| PROTO-003 | P0 | Strict JSONL event protocol | PROTO-002 | Pollution/malformed tests |
| PROTO-004 | P1 | Capability negotiation | PROTO-001 | Unsupported capability fails early |
| ADAPT-001 | P1 | Process adapter v1 | PROTO-003 | Conformance green |
| ADAPT-002 | P1 | OCI adapter v1 | PROTO-003 | Equivalent canonical result |
| TASK-001 | P0 | Task package schema | SPEC-001 | Schema validation |
| TASK-002 | P1 | Oracle/no-op validator | TASK-001 | Reference and no-op gates |
| TASK-003 | P1 | Alternative/exploit/mutation validator | TASK-002 | Adversarial corpus green |
| RUN-001 | P0 | Trial scheduler and ledger | SPEC-002 | Deterministic schedule fixture |
| RUN-002 | P0 | Ephemeral workspace runner | RUN-001 | No state survives trial |
| RUN-003 | P0 | Process-tree timeout/cancel | RUN-002 | Descendant kill integration test |
| RUN-004 | P0 | Resource controller | RUN-002 | Enforced time/token/cost/call limits |
| SEC-001 | P0 | Credential broker | RUN-002 | Secret never visible in harness |
| SEC-002 | P0 | Model gateway and route attestation | SEC-001 | Resolved model evidence |
| SEC-003 | P0 | Egress policy | RUN-002 | Exfiltration tests |
| SEC-004 | P0 | Late-mounted hidden grader | TASK-001,RUN-002 | Pre-exit access fails |
| SEC-005 | P0 | Fail-closed intake identity and executor verification | RUN-001 | Forged/ambiguous bundles rejected |
| SEC-006 | P0 | Pin Actions and build dependencies by digest/SHA | - | Supply-chain policy green |
| PROV-001 | P0 | Content-addressed trial bundle | PROTO-002 | Bundle digest verifies |
| PROV-002 | P0 | Runner attestation | PROV-001,SEC-002 | Tamper tests fail |
| STORE-001 | P1 | Immutable object/result store | PROV-001 | Idempotent ingest |
| STAT-001 | P0 | Task-clustered paired bootstrap | SPEC-002 | Simulation calibration |
| STAT-002 | P1 | Hierarchical model | STAT-001 | Known-effect recovery |
| STAT-003 | P1 | Bradley-Terry tournament analysis | SPEC-002 | CodeClash fixture parity |
| REPORT-001 | P1 | Multi-dimensional report schema | STAT-001 | Accuracy/reliability/cost sections |
| REPORT-002 | P1 | Uncertainty-aware UI | REPORT-001 | No winner when gate fails |
| GOV-001 | P0 | Semantic benchmark versioning | SPEC-001 | Immutable release test |
| GOV-002 | P0 | Incident/retraction policy | GOV-001 | Published runbook |
| GOV-003 | P1 | Appeals and reproduction workflow | STORE-001 | Trial bundle replay |
| DX-001 | P1 | `harness validate` | PROTO-001 | TTHW fixture |
| DX-002 | P1 | `trial smoke` | ADAPT-001,TASK-001 | Under 5 minutes |
| DX-003 | P1 | `eval run/reproduce` | RUN-001,STORE-001 | Copy-paste examples |
| SUITE-001 | P1 | 10-task internal alpha | TASK-003,RUN-004 | Five trials per cell |
| SUITE-002 | P1 | 50-task credible pilot | SUITE-001 | Independent review complete |

## Parallelization

| Lane | Work | Modules | Depends on |
|---|---|---|---|
| A | Schemas and protocol | `schemas/`, `protocol/` | SPEC |
| B | Task package and validator | `tasks/`, `grader/` | SPEC |
| C | Runner and sandbox | `runner/`, `sandbox/` | Protocol request draft |
| D | Broker and attestation | `security/`, `provenance/` | Runner interfaces |
| E | Statistics and report schema | `analysis/`, `reports/` | Estimand and trial schema |
| F | CLI and documentation | `cli/`, `docs/` | Stable protocol commands |

Execution:

1. Complete reconciliation first.
2. Launch A + B + E in parallel.
3. Start C once request/event schemas stabilize.
4. Start D against C's interfaces.
5. Start F with mocks, then integrate after A/C.
6. Merge all lanes before SUITE-001.

Conflict flags:

- A and C both define trial lifecycle types.
- C and D both touch runtime metadata.
- E and REPORT-001 must share one canonical result schema.

## NOT in scope

- Replacing model benchmarks such as SWE-bench.
- Claiming one universal best harness.
- Perfect cross-provider dollar normalization in v1.
- Supporting GUI-only harnesses without a headless adapter.
- Publishing private evaluation tasks.
- A hosted multi-tenant SaaS before the local/CI runner is credible.
- Human baselines for every short deterministic task in v1.
- Training or fine-tuning harnesses from benchmark trajectories.
- Cost-adjusted single-number ranking.

## Decision audit trail

| # | Decision | Classification | Principle | Rationale | Rejected |
|---:|---|---|---|---|---|
| 1 | Keep ATV League separate | Architecture | Claims follow evidence | Existing league is useful but measures bots | Stretch league into research benchmark |
| 2 | Create Controlled and Systems tracks | Product | Avoid confounding | Harness-only and full-stack effects are different | One global leaderboard |
| 3 | Make fresh harness trial the unit | Methodology | Statistical validity | Nested games are not independent | Count every game as harness evidence |
| 4 | Require private post-run grading | Security | Trust boundaries | Public grader enables gaming | Give hidden tests to harness |
| 5 | Use a credential broker | Security | Least privilege | Provider secrets must not enter untrusted runtime | Mount user auth |
| 6 | Use task-clustered paired statistics | Methodology | Complete evidence | Tasks are the comparison units | Sequential Elo for scientific rank |
| 7 | Require at least five trials per cell | Methodology | Account for nondeterminism | Single runs are unstable | One-shot leaderboard |
| 8 | Publish uncertainty and inconclusive outcomes | Product | Honest communication | Winner claims need precision | Always rank |
| 9 | Start with 50-task pilot | Scope | Credible minimum | One game cannot support broad claims | Launch with game-only suite |
| 10 | Defer hosted SaaS | Scope | Simplest credible system | Trust and methodology come first | Build service before benchmark |

## Cross-phase themes

### Evidence boundary

Product, engineering, security, and statistics all identify the same problem: evidence begins after
the harness has already acted. The harness must move inside the trusted trial boundary.

### Separation of concerns

The league, controlled benchmark, full-system benchmark, and resilience suite answer different
questions. Combining them creates misleading scores and harder architecture.

### Versioned reproducibility

Task, runner, protocol, harness, model policy, and report versions are all load-bearing. Reproducing
only the source repository is insufficient.

### Task diversity before leaderboard polish

The benchmark becomes credible by adding validated independent tasks and repeated fresh trials, not
by adding more viewer features or fingerprint chips.

## Autoplan completion summary

- Step 0: Scope Challenge - scope split into League, Controlled, Systems, and Resilience.
- CEO/strategy review - 9 major premise and governance issues identified.
- Design review - skipped; no visual-design implementation is proposed.
- Architecture review - trusted runner, gateway, grader, attestation, and store required.
- Code quality review - current runner/protocol/cache/evidence responsibilities are coupled.
- Test review - test diagram produced; 30+ protocol/security/statistical gaps identified.
- Performance review - cost, concurrency, output bounds, nested simulation cost, and task scale addressed.
- NOT in scope - written.
- What already exists - written with keep/replace dispositions.
- TODOS.md updates - P0/P1 upgrade backlog added.
- Failure modes - critical silent gaps flagged.
- Outside voices - four independent parallel research lanes completed.
- Parallelization - six implementation lanes after reconciliation.
- Completeness decision - chose the full evidence architecture, not a leaderboard-only patch.

## Credibility launch gates

Do not launch an official harness leaderboard until all are true:

- [ ] Controlled and Systems tracks are separated.
- [ ] Trial is the independent unit in schema and analysis.
- [ ] Harness protocol and task schema are versioned.
- [ ] Process and OCI adapters pass conformance.
- [ ] Runner is ephemeral and resource-bounded.
- [ ] Provider secrets never enter harness runtime.
- [ ] Resolved model identity is attested.
- [ ] Hidden grader is inaccessible before harness exit.
- [ ] Trial bundle is content-addressed and signed.
- [ ] At least 50 reviewed tasks across five categories exist.
- [ ] At least five fresh trials per cell run.
- [ ] Scheduling is paired and randomized.
- [ ] Task-clustered uncertainty is published.
- [ ] Winner/equivalence rule is automated.
- [ ] Contamination and retraction policies are published.
- [ ] One independent reproduction succeeds.
- [ ] No critical silent failure remains in the registry.

## Sources

Primary sources used for this blueprint:

- CodeClash paper: <https://arxiv.org/html/2511.00839v2>
- CodeClash repository: <https://github.com/CodeClash-ai/CodeClash>
- SWE-bench FAQ and versioning: <https://www.swebench.com/SWE-bench/faq/>
- SWE-bench repository: <https://github.com/SWE-bench/SWE-bench>
- Terminal-Bench 2 repository and FAQ: <https://github.com/laude-institute/terminal-bench-2>
- Terminal-Bench 2 paper: <https://arxiv.org/html/2601.11868v1>
- Harbor: <https://harborframework.com/>
- RE-Bench paper: <https://arxiv.org/html/2411.15114v3>
- METR RE-Bench report: <https://metr.org/blog/2024-11-22-re-bench/>
- HELM: <https://crfm.stanford.edu/helm/>
- Inspect AI: <https://inspect.aisi.org.uk/>
- lm-evaluation-harness: <https://github.com/EleutherAI/lm-evaluation-harness>
- LiveCodeBench: <https://github.com/LiveCodeBench/LiveCodeBench>
- SLSA provenance: <https://slsa.dev/spec/v1.2/provenance>
- in-toto attestations: <https://in-toto.io/>
- OCI image specification: <https://github.com/opencontainers/image-spec>
- GitHub Actions untrusted PR guidance:
  <https://securitylab.github.com/resources/github-actions-preventing-pwn-requests/>
- JSON Schema 2020-12: <https://json-schema.org/draft/2020-12>
- JSON Lines: <https://jsonlines.org/>
- OpenTelemetry generative AI conventions:
  <https://opentelemetry.io/docs/specs/semconv/gen-ai/>
- OpenAI evaluation best practices:
  <https://platform.openai.com/docs/guides/evaluation-best-practices>
- OpenAI agent evaluations:
  <https://platform.openai.com/docs/guides/agent-evals>

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|---|---|---|---:|---|---|
| CEO Review | `/plan-ceo-review` | Scope and strategy | 1 | ISSUES OPEN | Separate products, estimands, and launch claims |
| Codex Review | `/codex review` | Independent second opinion | 0 | - | Parallel research agents used instead |
| Eng Review | `/plan-eng-review` | Architecture and tests | 1 | ISSUES OPEN | Critical runner, protocol, task, security, provenance, and statistics gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | SKIPPED | No visual design scope |
| DX Review | `/plan-devex-review` | Developer experience gaps | 1 | ISSUES OPEN | TTHW, adapter contribution, reproduction, and versioning need redesign |

- **UNRESOLVED:** implementation choices within the proposed phases remain to be executed.
- **VERDICT:** analysis complete; benchmark implementation is not yet cleared for a scientific leaderboard.
