# Contributing to ATV-bench Community League

Two ways to contribute: **submit a harness** (enter the league) or **extend the
ecosystem** (add a harness adapter or a game). Both are validated locally before you
open a PR — the validators reuse the exact leak-safe scanner and sandbox the
production path uses, so nothing weaker runs in review.

## Prerequisites

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).
- The [GitHub CLI](https://cli.github.com) (`gh`) for `atv-bench submit`.
- Docker (only for running matches locally / the gated integration tests).

```bash
uv venv && uv pip install -e '.[dev]'
uv run pytest -m "not live and not integration" -q   # should be all green
```

## Authentication

`atv-bench submit` opens a PR on your behalf via `gh`. Authenticate once:

```bash
gh auth login   # choose GitHub.com, HTTPS
```

If `gh` is installed but not logged in, submit stops at preflight with an actionable
message pointing here.

## Submitting your harness (entering the league)

1. Run a local match so your harness produces a bot (e.g. `main.py`).
2. Preview exactly what your fingerprint will publish — **do this before submitting**:
   ```bash
   atv-bench fingerprint --dry-run
   ```
   You'll see three sections: **Will publish** (the names that go public),
   **Scrubbed** (values the scanner withheld — proof it ran), and **Unknown**
   (surfaces that couldn't be read). No secret-shaped value is ever published; a
   value that looks secret-like is withheld and only its field is named.
3. Validate and submit:
   ```bash
   atv-bench validate-game ./main.py
   atv-bench submit ./main.py --game battlesnake --dry-run   # preflight, no PR
   atv-bench submit ./main.py --game battlesnake             # opens the PR
   ```

### Clean branch

Submit needs a clean working tree so the PR carries only your bot + fingerprint.
Commit or `git stash` first.

### Forking

If you don't have a fork yet, `submit` offers to create one (`gh repo fork`). You can
also fork manually and push a branch.

### Bot shape

A bot is a **single small text file** (≤ 256 KiB) with the arena's expected entrypoint
(e.g. `main.py` for Battlesnake). `validate-game` enforces this before submission and
the sandbox enforces it again before execution.

### Manual PR fallback

If `gh` isn't available or the automated flow fails, open the PR by hand: fork
`All-The-Vibes/ATV-bench` and add two files under `league/submissions/<your-identity>/`:

- `main.py` — your bot (a single text file ≤ 256 KiB).
- `<your-identity>.json` — the submission record produced by
  `atv-bench submit --dry-run` (identity, game, bot_sha256, bot_filename, pr_url,
  logs_url, fingerprint). This is the exact shape the league store ingests
  (`LeagueStore.add_submission`); do not hand-edit the fingerprint.

Then open a PR. A maintainer reviews it and adds the `run-match` label to trigger the
match job. (The label is the trust boundary that gates untrusted bot execution, so only
maintainers can add it.)

## What happens after you submit

```
PR opened → (first-timer: maintainer approves the run) → match job runs your bot
          → result artifact → publish job recomputes ELO → leaderboard updates
```

First-time contributors need a maintainer to approve the workflow run before the
untrusted bot executes (a GitHub environment gate). Expect a short wait the first time.

## Extending the ecosystem

### Add a harness adapter

v1 fingerprints **claude-code**. To add copilot/codex/another harness:

1. Implement a reader that returns the fixed fingerprint schema (see
   `src/atv_bench/fingerprint/probe.py::FINGERPRINT_SCHEMA_KEYS`). Read **names and
   counts only — never file contents**. Route anything unreadable to
   `unknown[{field, reason}]`.
2. Run the required leak check:
   ```bash
   atv-bench validate-harness
   ```
3. **Add a canary leak-test** for your reader modeled on
   `tests/test_fingerprint_leak.py`: a synthetic config stuffed with secrets, asserting
   zero canaries reach the manifest or log. This test is **required** — a harness
   reader without one will not be merged. The credibility of the whole league rests on
   it.

### Add a game

Reuse a CodeClash arena. A game contribution needs: the arena, a bot entrypoint
convention, and a `validate-game`-compatible shape check. Bots run in the locked-down
sandbox (`--network none`, memory/pid/time caps, non-root read-only) — see
`.github/workflows/league.yml` and `tests/test_action_malicious_bot.py`.

## Security model (why the two-job Action matters)

The match job that runs your untrusted bot has **no** `GITHUB_TOKEN`, no Pages write,
and blocked egress; it writes only a result artifact. The trusted publish job never
executes bot code. See `docs/COMMUNITY_LEAGUE.md`. The isolation is asserted on every
push by `tests/test_action_isolation.py` and proven against real Docker by the gated
`tests/test_action_malicious_bot.py`.
