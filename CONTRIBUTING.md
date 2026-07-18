# Contributing to ATV-bench Community League

Two ways to contribute: **submit a harness** (enter the league) or **extend the
ecosystem** (add a harness adapter or a game). Both are validated locally before you
open a PR — the validators reuse the exact leak-safe scanner and sandbox the
production path uses, so nothing weaker runs in review.

## Prerequisites

**Just submitting a harness?** You don't need to clone this repo. Install the CLI
directly and skip to [Submitting your harness](#submitting-your-harness-entering-the-league):

```bash
uv tool install --from git+https://github.com/All-The-Vibes/ATV-bench atv-bench
atv-bench doctor    # verify python / harness config / gh / docker
```

**Extending the ecosystem** (adding a harness adapter or a game) needs a dev checkout:

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).
- The [GitHub CLI](https://cli.github.com) (`gh`) for `atv-bench submit`.
- Docker (only for running matches locally / the gated integration tests).

```bash
git clone https://github.com/All-The-Vibes/ATV-bench && cd ATV-bench
uv venv && uv pip install -e '.[dev]'
uv run pytest -m "not live and not integration" -q   # should be all green
```

## Authentication

`atv-bench submit --dry-run` runs a `gh`-based preflight (checking you're authenticated
and the league repo is reachable) and writes your submission record. `atv-bench submit
--live --identity <you>` runs the same preflight and, if it passes, opens the PR
end-to-end (fork → clone → branch → stage → commit → push → `gh pr create`, then backfills
the real PR URL into the committed record). A missing fork is bootstrapped automatically,
so first-time contributors need no manual setup. If you'd rather open the PR yourself, use
`--dry-run` and the [Manual PR fallback](#manual-pr-fallback). Authenticate `gh` once
either way:

```bash
gh auth login   # choose GitHub.com, HTTPS
```

If `gh` is installed but not logged in, submit's preflight flags it with an actionable
message pointing here.

## Submitting your harness (entering the league)

1. Have your harness build a bot for a live game (`atv-bench games` — `lightcycles` is
   the playable arena; it produces a single `main.py` that emits one move per turn).
2. Preview exactly what your fingerprint will publish — **do this before submitting**:
   ```bash
   atv-bench fingerprint --dry-run
   ```
   You'll see three sections: **Will publish** (the names that go public),
   **Scrubbed** (values the scanner withheld — proof it ran), and **Unknown**
   (surfaces that couldn't be read). No secret-shaped value is ever published; a
   value that looks secret-like is withheld and only its field is named.
3. Validate and build your submission record:
   ```bash
   atv-bench validate-game ./main.py
   atv-bench submit ./main.py --game lightcycles --dry-run \
     --identity <your-github-login> --out submission.json
   ```
   `--dry-run` runs preflight and writes `submission.json` (the store-ingestable
   record). Then either open the PR automatically with `atv-bench submit ./main.py
   --game lightcycles --live --identity <your-github-login>`, or commit the bot + record
   under `league/submissions/<identity>/` and open a PR yourself (see **Manual PR
   fallback** below).

### Clean branch

Submit needs a clean working tree so the PR carries only your bot + fingerprint.
Commit or `git stash` first.

### Forking

If you don't have a fork yet, `submit` offers to create one (`gh repo fork`). You can
also fork manually and push a branch.

### Bot shape

A bot is a **single small text file** (≤ 256 KiB) with the arena's expected entrypoint
(e.g. `main.py` for lightcycles). `validate-game` enforces this before submission and
the sandbox enforces it again before execution.

### Test your bot locally before submitting

Don't submit a bot you haven't watched play. `atv-bench play` runs a real refereed match
locally (same engine + referee as the arena) against a chosen opponent and opens an
animated replay:

```bash
atv-bench bots                                        # list the opponent series
atv-bench play --player-bot ./main.py --opponent bare    # sanity floor: must beat this
atv-bench play --player-bot ./main.py --opponent greedy  # the arena anchor
```

A bot that forfeits (hangs, crashes, or emits garbage instead of a direction word) shows
up immediately as a forfeit loss in the local match — fix it before you open a PR.

### Manual PR fallback

If `gh` isn't available or the automated flow fails, open the PR by hand. Run
`atv-bench submit ./main.py --game lightcycles --dry-run --identity <you> --out submission.json`
to produce the record, then fork `All-The-Vibes/ATV-bench` and add exactly two files
**in one directory named for your identity**:

- `league/submissions/<your-identity>/main.py` — your bot (single text file ≤ 256 KiB).
- `league/submissions/<your-identity>/submission.json` — the `submission.json` from
  `--dry-run` (identity, game, bot_sha256, bot_filename, pr_url, logs_url, fingerprint).
  This is the exact nested shape the league store ingests (`LeagueStore.load_submissions`
  reads `league/submissions/<identity>/submission.json` and anchors identity to the
  directory name); do not hand-edit the fingerprint — the publish job re-scans it for
  secret-shaped values and drops any it finds.

Then open a PR. A maintainer reviews it and adds the `run-match` label to trigger the
match job. (The label is the trust boundary that gates untrusted bot execution, so only
maintainers can add it.)

## What happens after you submit

```
PR opened → (first-timer: maintainer approves the run) → match job runs your bot
          → result + trusted-meta artifacts → league-publish (workflow_run) recomputes
            ELO, persists the store, and deploys the leaderboard
```

First-time contributors need a maintainer to approve the workflow run before the
untrusted bot executes (a GitHub environment gate). Expect a short wait the first time.

## Seeing where you rank

The live board is at **https://all-the-vibes.github.io/ATV-bench/** — every merged
harness ranks there. Locally:

```bash
atv-bench board --demo            # populated sample board, opens in your browser
atv-bench board --store league    # render the real board from a checkout's store
```

`board` renders the exact static site the Action publishes; the viewer is bundled in the
package, so `--demo` works from the installed CLI with no clone.

**Fork-safe by design.** The match job that runs your bot holds no token (a fork PR's
`GITHUB_TOKEN` is read-only anyway) and only uploads two artifacts: the bot's result and
a *trusted* meta record (your GitHub identity + the run id + the bot's byte hash, built
from GitHub context, never from bot output). A separate trusted workflow
(`league-publish.yml`) then runs in the base repo on `workflow_run`, where it has the
write access needed to persist the store and deploy Pages — without ever checking out or
executing your PR code. This is what lets **fork** submissions score end-to-end, not just
same-repo branches.

## Extending the ecosystem

### Add a harness adapter

The CLI is harness-agnostic; v1 ships **live** fingerprint readers for `claude-code`
(`~/.claude`), `copilot-cli` (`~/.copilot`), and `codex` (`~/.codex`)
(`atv-bench harnesses` lists them). To make another harness live:

1. **Register it** in `src/atv_bench/harnesses.py` (or flip an existing planned entry's
   `live=True`): set its `key`, `title`, and `config_root` (the dir under `$HOME` its
   config lives in).
2. Implement a reader that returns the fixed fingerprint schema (see
   `src/atv_bench/fingerprint/probe.py::FINGERPRINT_SCHEMA_KEYS`). Read **names and
   counts only — never file contents**. Route anything unreadable to
   `unknown[{field, reason}]`. Wire it into `probe.py::_READERS[<key>]` so
   `atv-bench fingerprint --harness <key>` dispatches to it.
3. Run the required leak check:
   ```bash
   atv-bench validate-harness --harness <key>
   ```
4. **Add a canary leak-test** for your reader modeled on
   `tests/test_fingerprint_leak.py`: a synthetic config stuffed with secrets, asserting
   zero canaries reach the manifest or log. This test is **required** — a harness
   reader without one will not be merged. The credibility of the whole league rests on
   it.

#### Which existing reader should I copy?

The three live readers diverge on config format and plugin layout — pick the closest
starting point:

| If the harness's config is… | Copy | Why |
|---|---|---|
| **flat TOML** (one `config.toml`, no plugin tree) | `probe_codex` | TOML via `reader.read_toml`; model is a single top-level key; MCPs are `[mcp_servers.*]` table keys; no nested plugin walk. |
| **JSON with a nested plugin tree** (skills/agents live *inside* installed plugins) | `probe_claude_code` | Manifest-driven walk: parse the plugin manifest, iterate `installPath`, read `installPath/{skills,agents}` confined under the config root. Enabled/disabled comes from a truthy filter. |
| **JSON with a flat `installed-plugins/<mkt>/<plugin>/` tree** + explicit disabled lists | `probe_copilot_cli` | Directory walk over the marketplace tree; disabled skills/MCPs are subtracted from explicit denylists in `settings.json`. |

All three share the same discipline: allowlist-emit field by field, every name through
`scan.is_safe_name`, all reads confined to the config root, and **only top-level / named
keys are read** — never a provider/credential-bearing subtable (see `probe_codex`, which
reads *only* `config.toml["model"]` and never `[model_providers.*]` / `http_headers`).

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
