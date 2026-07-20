# Generic harness process protocol

Status: **experimental v1 process adapter**. This makes arbitrary headless harnesses
callable without adding vendor-specific Python code. It is not attested execution and
does not by itself make two runs scientifically comparable.

## CLI

```bash
atv-bench harness-run \
  --repo ./seed-repo \
  --goal "Improve main.py so the bot wins more games" \
  --bot-file main.py \
  --model auto \
  --timeout 300 \
  --json \
  -- your-harness --headless
```

Everything after `--` is passed as an argv vector. No shell is used. Arguments may use
these placeholders:

- `{goal}`
- `{repo}`
- `{bot_file}`
- `{model}`
- `{request_path}`

The seed repository must have a readable Git `HEAD`. The adapter captures committed,
staged, unstaged, and untracked changes.

## Request

Every process receives:

| Variable | Meaning |
|---|---|
| `ATV_BENCH_REQUEST_JSON` | Complete JSON request |
| `ATV_BENCH_REQUEST_PATH` | Temporary UTF-8 JSON file containing the same request |
| `ATV_BENCH_GOAL` | Goal string |
| `ATV_BENCH_REPO` | Absolute seed repository path |
| `ATV_BENCH_BOT_FILE` | Primary bot path relative to the repository |
| `ATV_BENCH_MODEL` | Requested model label |

`--stdin-json` also sends the request JSON followed by a newline on stdin.

## Protocol-v1 OCI transport

OCI harness manifests use the canonical `atv.trial-request/v1` and JSONL event
schemas rather than the legacy optional-response object above. The attached transport:

1. sends the canonical request on container stdin;
2. accepts only one valid harness `hello`;
3. writes the controller-authored `accepted` event back to the still-running harness;
4. validates bounded harness events until one terminal `result`;
5. inspects the live container policy before cleanup;
6. force-removes that exact container and verifies absence before grading.

Harness stdout is reserved for protocol JSONL. Stderr is drained separately and both
streams are bounded. Blank lines, invalid UTF-8, pre-accept pipelining, forged
controller events, extra terminal frames, pipe leaks, cleanup failures, and runtime
inspection mismatches fail closed.

On supported Linux Docker engines, strict runs mount `/workspace`, `/artifacts`, and
`/tmp` as subpaths of one size-limited tmpfs-backed named volume. This makes the disk
limit aggregate rather than a post-run sum. The volume and subpath policy are inspected
before payload execution and the named volume is confirmed absent afterward.

Request shape:

```json
{
  "repo_path": "/workspace/seed",
  "goal": "Improve main.py",
  "model": "auto",
  "budget": {
    "max_turns": 10,
    "max_seconds": 300,
    "max_tokens": 200000
  },
  "bot_file": "main.py"
}
```

Only `max_seconds` is enforced by the generic process runner. A wrapper must disclose
whether token and turn budgets are actually enforceable.

## Optional response

A wrapper may print a final JSON object on stdout:

```json
{
  "status": "ok",
  "model": "provider/model-snapshot",
  "usage": {"tokens": 1234, "seconds": 18.2, "turns": 4}
}
```

Recognized statuses are `ok`, `no_edit`, `error`, `timeout`, `budget_exhausted`, and
`policy_denied`. A nonzero process exit is always `error`. If no response is printed,
model and usage remain unknown and status is inferred from the repository diff.

Harness logs are omitted by the CLI unless `--include-log` is passed because stdout and
stderr may contain secrets.

## Trust boundary

The process adapter runs on the local host with the caller's credentials and network.
It is **self-attested**. Do not publish its output as a verified benchmark result.

A publishable harness benchmark still needs:

1. a versioned JSONL/OCI conformance protocol;
2. explicit environment and network allowlists;
3. digest-pinned runner and harness artifacts;
4. verified model/provider identity;
5. persistent multi-round edit/competition feedback;
6. immutable trajectories, logs, diffs, and result bundles;
7. paper-compatible repeated tournaments and statistical estimation.
