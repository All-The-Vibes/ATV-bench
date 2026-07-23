# Errors & Exit Codes

Every `atv-bench` failure renders the same shape — `Problem` / `Cause` / `Fix` /
`exit N` — and `run` (plus `doctor`/`--demo`) emit stable per-failure-mode exit
codes so agents and CI can distinguish a retryable failure (timeout) from one
that needs human action (unauthenticated).

## Exit-code table

| Exit | Code (`code`)        | Meaning                                                        |
|-----:|----------------------|----------------------------------------------------------------|
| 0    | `ok`                 | Success.                                                        |
| 2    | `usage`              | Bad invocation / unknown code — usage error.                   |
| 3    | `missing_cli`        | A required external CLI (e.g. `gh`) is not installed.          |
| 4    | `unauthenticated`    | Auth is required but missing (e.g. `gh auth login`).           |
| 5    | `docker_unavailable` | Docker is required for arena adjudication but not available.   |
| 6    | `policy_denied`      | A policy/governance check denied the operation.                |
| 7    | `timeout`            | The match or step timed out (retryable).                       |
| 8    | `model_unparseable`  | The model output could not be parsed.                          |
| 9    | `codeclash_dep`      | The CodeClash git dependency is not importable — reinstall.    |

For `codeclash_dep` (exit 9), the CodeClash arena ships in the `run` extra —
install that extra to pull the git dependency (a plain reinstall of `atv-bench`
will NOT pull it):

```bash
uv tool install --reinstall --from 'atv-bench[run] @ git+https://github.com/All-The-Vibes/ATV-bench' atv-bench
```

From a source checkout: `uv pip install -e '.[run]'`,
or run `atv-bench doctor` for a full prerequisite report.
