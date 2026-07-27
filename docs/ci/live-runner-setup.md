# Live-integration self-hosted runner setup

The `live-integration` job in `.github/workflows/live-integration.yml` runs the **real**
CodeClash arena: Docker-sandboxed matches and live harness (LLM) matches. Its fail-closed
preflight (`src/atv_bench/runner.py::preflight_or_raise`) **requires the harness CLIs on
PATH and a working Docker daemon** — it deliberately refuses to fabricate a match when they
are absent. GitHub-hosted runners have neither, so this job targets a **self-hosted runner**
labeled `atv-live`.

> The pre-merge `import-smoke` job stays on GitHub-hosted runners and needs none of this.
> Only the push-to-main / manual `live-integration` job needs the provisioned runner.

## 1. Provision a machine

A Linux host (Ubuntu 22.04/24.04) with:

- **Docker Engine** running, and the runner's user in the `docker` group
  (`sudo usermod -aG docker "$USER"` then re-login; verify `docker info` works without sudo).
- **Python 3.12** available.
- Outbound network egress to the model providers (Anthropic / GitHub / OpenAI) — live LLM
  adapters run with egress permitted; they must reach their provider.
- The three harness CLIs installed and on PATH:
  - `claude`  — Claude Code CLI
  - `copilot` — GitHub Copilot CLI
  - `codex`   — OpenAI Codex CLI

Verify: `for b in docker claude copilot codex; do command -v "$b" || echo "MISSING $b"; done`

## 2. Register the runner with the repo

GitHub → **Settings → Actions → Runners → New self-hosted runner**, follow the download +
`./config.sh` steps for `https://github.com/All-The-Vibes/ATV-bench`. When prompted for
labels (or via `--labels`), add **`atv-live`** (the `self-hosted` label is implicit). The
workflow targets `runs-on: [self-hosted, atv-live]`.

Run it as a service so it survives reboots:

```bash
sudo ./svc.sh install
sudo ./svc.sh start
```

### Security note (self-hosted runners)
Self-hosted runners execute arbitrary workflow code. **Do not** attach this runner at the
org level where untrusted fork PRs could target it. Keep it repo-scoped, and because
`live-integration` only runs on `push` to a protected branch and `workflow_dispatch`
(never on `pull_request`), fork PRs cannot schedule it. Run the host in a disposable VM if
possible; the arena already sandboxes bot code in Docker (non-root, read-only rootfs,
cap-drop), but the runner host itself still runs trusted repo code.

## 3. Configure auth secrets

The workflow surfaces these repo (or org) **Actions secrets** as env for the harness
adapters. Add whichever harnesses you want to exercise live:

| Secret | Harness | Used by |
|--------|---------|---------|
| `ANTHROPIC_API_KEY` | `claude-code` | `adapters/contract.py` ClaudeCodeAdapter |
| `COPILOT_GITHUB_TOKEN` | `copilot-cli` | CopilotCliAdapter (also accepts `GH_TOKEN`/`GITHUB_TOKEN`) |
| `OPENAI_API_KEY` | `codex` | CodexCliAdapter |

```bash
gh secret set ANTHROPIC_API_KEY    --repo All-The-Vibes/ATV-bench
gh secret set COPILOT_GITHUB_TOKEN --repo All-The-Vibes/ATV-bench
gh secret set OPENAI_API_KEY       --repo All-The-Vibes/ATV-bench
```

Alternatively, log the CLIs in interactively on the runner host once (`claude` /
`copilot login` / `codex login`) — the adapters use an existing login if the env var is
absent. Secrets are the reproducible, headless path and are preferred for CI.

> If a secret is unset **and** the CLI isn't logged in, that harness's *live* match forfeits,
> but the deterministic sample-bot arena tests and the Docker-containment tests still pass —
> so a partially-provisioned runner degrades gracefully rather than hard-failing everything.

## 4. Verify

Trigger manually and watch the preflight step:

```bash
gh workflow run live-integration.yml --repo All-The-Vibes/ATV-bench --ref main
gh run watch "$(gh run list --workflow=live-integration.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
```

The **"Preflight — verify the runner is actually provisioned"** step fails loud with the
exact missing binary/daemon if the host isn't ready, instead of the opaque mid-suite
`claude not found on PATH`. Once green, the push-to-main red-X on `live-integration` is
resolved.

## Cost / maintenance

- The runner incurs live LLM API cost on every push to `main` (real matches). If that's too
  frequent, change the `push` trigger to a nightly `schedule:` + `workflow_dispatch` and keep
  `import-smoke` as the per-PR tripwire.
- Keep the harness CLIs updated on the host; a CLI major-version bump can change headless flags.
