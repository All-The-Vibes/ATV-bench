<div align="center">

# 🏁 ATV-bench — The Community League

### Everyone benchmarks the **model.** Nobody benchmarks the **harness.**

Your skills. Your MCP servers. Your plugins, custom agents, and config.
**That's what actually ships code — so that's what we rank.**

[![hermetic tests](https://img.shields.io/badge/tests-397%20passing-6ce7be?style=flat-square)](#dev)
[![docker adjudication](https://img.shields.io/badge/arena-referee%20adjudicated-7aa2ff?style=flat-square)](#the-trust-boundary)
[![leak-safe](https://img.shields.io/badge/fingerprint-leak--safe-ffc45c?style=flat-square)](#the-credibility-gate)
[![license](https://img.shields.io/badge/license-MIT-e8ecf5?style=flat-square)](LICENSE)

https://github.com/user-attachments/assets/438771f0-4886-4185-9c75-85c8d9c35bd9

</div>

---

## The pitch

SWE-bench and CodeClash measure the **model**. But you don't ship a raw model — you
ship a *harness*: a model wrapped in skills, MCP servers, plugins, custom agents, and a
pile of config. Two engineers on the same model get wildly different results because
their harnesses differ.

**ATV-bench ranks the whole thing.** You submit a bot your harness built + a leak-safe
fingerprint of that harness. A GitHub Action plays the matches in a sandboxed arena,
a trusted referee adjudicates the outcome from *real gameplay* (not the bot's word for
it), ELO is recomputed from scratch, and a static leaderboard ships to GitHub Pages.

No hosted server to run or trust. No self-reported scores. Just harnesses, head-to-head.

## How it works — Approach A (git + Action + static Pages)

```
 you                                 this repo (GitHub)
 ┌───────────────────────┐  PR       ┌───────────────────────────────┐
 │ atv-bench submit       │ ────────▶ │ match job (untrusted):        │
 │  local match           │           │   perms:{}, no token,         │
 │  fingerprint probe     │           │   egress blocked → artifact   │
 │  (leak-scrubbed)       │           ├───────────────────────────────┤
 └───────────────────────┘           │ publish job (trusted):        │
                                      │   reads artifact only,        │
                                      │   recompute ELO, build board  │
                                      └───────────────┬───────────────┘
                                                      ▼
                                       static leaderboard (GitHub Pages)
                                       row = rank · ELO · fingerprint chips
```

The two-job split is load-bearing: the job that executes an untrusted, harness-built
bot has **no** `GITHUB_TOKEN`, no Pages write, and blocked egress. The trusted publish
job never executes bot code — it only reads a schema-validated result artifact, and runs
in a fork-safe `workflow_run` context so external contributors work end-to-end.

## The trust boundary

The outcome is **arena-adjudicated**, not bot-asserted. The arena image's entrypoint is a
**trusted referee** (`python3 -m atv_bench.arena`) that runs a deterministic Tron /
lightcycles engine inside the sandbox and drives the submitted bot as a **move-only
subprocess** — one direction per turn, per-turn timeout.

| A bot that…                        | …gets                          |
|------------------------------------|--------------------------------|
| plays honestly                     | a real outcome from gameplay   |
| prints a fabricated result JSON    | an invalid move → **forfeit**  |
| hangs, crashes, or emits garbage   | scored forfeit loss + reason   |
| names a third party / fakes run_id | rebound to `CRASH` vs submitter |

The referee is baked into the image byte-identical to the tested `src/` package (drift
tripwire), so no trusted code is ever read from the untrusted mount. Proof + a rendered
match board: [`docs/proof/item1-adjudication/`](docs/proof/item1-adjudication/).

## The credibility gate — leak-safe harness fingerprint

A per-harness probe reads on-disk config and emits **one normalized, leak-safe** schema:

```json
{ "harness": "claude-code", "model": "claude-opus-4-8", "gstack": true,
  "skills": ["gstack", "office-hours"], "mcps": ["grafana", "github"],
  "plugins": ["compound-engineering"], "custom_agents_count": 7,
  "unknown": [{ "field": "cloud_settings", "reason": "not_readable" }] }
```

- **Allowlist-by-construction** — every field is built from a fixed schema, never
  copy-then-delete. A field the schema doesn't name is ignored, not passed through.
- **Per-value secret scan** — anything matching `sk-`, `ghp_`, `xox`, `AKIA`, a DSN,
  URL-with-creds, PEM, or high-entropy becomes `unknown[{field, reason}]` — never published.
- **Names only, never contents** — reads basenames/counts; never opens a file body.
- **Consent surface** — `atv-bench fingerprint --dry-run` shows the exact *Will publish*
  list + scrubbed count before anything leaves your machine.

v1 ships **live** fingerprint readers for **`claude-code`** (`~/.claude`),
**`copilot-cli`** (`~/.copilot`), and **`codex`** (`~/.codex`). `atv-bench harnesses`
lists them and auto-detects yours; `atv-bench fingerprint --harness <key>` targets one
explicitly. Adding a harness is a reader + a required canary leak-test
(see [CONTRIBUTING.md](CONTRIBUTING.md#add-a-harness-adapter) → *Which existing reader
should I copy?*).

## Quick start (zero to board)

**Install — no clone, no npm.** One command puts the `atv-bench` CLI on your PATH,
straight from the repo:

```bash
uv tool install --from git+https://github.com/All-The-Vibes/ATV-bench atv-bench
```

No `uv`? Get it at [astral.sh/uv](https://docs.astral.sh/uv/) (`curl -LsSf
https://astral.sh/uv/install.sh | sh`), or use pipx: `pipx install
git+https://github.com/All-The-Vibes/ATV-bench`. Upgrade later with
`uv tool upgrade atv-bench`; remove with `uv tool uninstall atv-bench`.

Verify your machine is ready:

```bash
atv-bench doctor          # python / harness config / gh / docker / CodeClash readiness, with fixes
```

### Real harness-vs-harness match (the spine)

The core loop: **fingerprint** each harness → the **real harness CLI** (`claude`,
`copilot`) builds its own `main.py` headless → the two bots **compete in a CodeClash
arena** (Docker) → **ELO + replay**. Nothing hand-written, no faked model string.

Start with the zero-setup real recording — no Docker, no auth, no network:

```bash
atv-bench run --demo                 # replays a canned but REAL recorded match
atv-bench run --demo --json          # same, as a stable machine-readable envelope
```

Then run a live match (needs Docker + a real harness CLI authenticated — see `doctor`):

```bash
atv-bench run --game lightcycles --a copilot-cli --b claude-code --model claude-opus-4.8
atv-bench run --game battlesnake  --a claude-code --b claude-code --model claude-opus-4.8 --json
atv-bench run --list-games --list-harnesses     # discover valid values
```

Both harnesses run on the **same model** for parity, so the result isolates the
*harness*, not the model. Phase-1 results are labeled **unverified / local-debug** and
do **not** publish a ranked number — that needs the Phase-2 gateway (a real match is
non-deterministic; only the recorded replay is reproducible). Exit codes are stable and
distinct per failure mode (`0` ok · `3` missing-cli · `5` docker · `9` codeclash-dep …)
so an agent or CI can branch on them.

### See the rankings first

Before you submit anything, look at the board — including a populated sample so you
know what you're aiming at:

```bash
atv-bench board --demo    # builds a sample leaderboard and opens it in your browser
```

The live community board is at
**https://all-the-vibes.github.io/ATV-bench/** — that's where every submitted harness
ranks. To render the *real* board locally from a checkout's store: `atv-bench board
--store league`.

### Watch a real match locally (no submission, no mocks)

Before you touch a PR, *see the game played*. `atv-bench play` runs a *real* refereed
match on your machine — the same trusted engine + referee the sandboxed arena uses, so
the outcome is adjudicated from actual gameplay, not faked — and opens an animated
replay you can scrub through.

```bash
atv-bench bots            # the opponent series you can play against
atv-bench play --player bare --opponent greedy       # watch two reference bots fight
atv-bench play --player greedy --opponent wall_hugger
```

The opponent series (`atv-bench bots`):

| bot | what it is |
|-----|------------|
| `greedy` | The trusted arena **anchor** — the yardstick every submission plays. |
| `wall_hugger` | A more aggressive space-filling strategy. |
| `bare` | The **no-harness baseline**: go straight, only turn to avoid crashing. Stands in for a raw model with no skills/MCP/agents. If your bot can't beat `bare`, your harness added nothing. |

**Watch YOUR harness-built bot play** — point `--player-bot` at the `main.py` your
harness produced and pick any opponent:

```bash
atv-bench play --player-bot ./main.py --opponent greedy
atv-bench play --player-bot ./main.py --opponent bare --game lightcycles
```

Each run prints an ASCII board with the trusted outcome and writes a self-contained
`_replay/replay.html` (frames embedded inline — no server, no network). Matches are
fully deterministic (same bots → same game every time); `--seed` only labels the replay.
Use `--no-open` to skip launching the browser.

### Run the benchmark on your harness (step by step)

1. **Pick a game.** See what's playable:
   ```bash
   atv-bench games        # lightcycles is live; battlesnake is planned
   ```
2. **Have your harness build a bot.** Point your own harness (Claude Code, Copilot CLI,
   your skills/MCP/agents — the thing being ranked) at the game and let it produce a
   single-file bot named `main.py` that plays the arena (emits one move per turn). That
   bot *is* your harness's entry — the whole thesis is that your harness, not a raw
   model, wrote it.
3. **Watch it play before you submit** (catch a broken bot in seconds):
   ```bash
   atv-bench play --player-bot ./main.py --opponent greedy
   ```
4. **See exactly what your harness fingerprint would publish** (nothing leaves your
   machine):
   ```bash
   atv-bench harnesses          # claude-code, copilot-cli, codex are all live (auto-detects yours)
   atv-bench fingerprint --dry-run   # add --harness <key> to target a specific one
   ```
5. **Validate + build your submission:**
   ```bash
   atv-bench validate-game ./main.py
   atv-bench submit ./main.py --game lightcycles \
     --identity <your-github-login> --dry-run --out submission.json
   ```
6. **Open the PR** (live automation behind the preflight):
   ```bash
   atv-bench submit ./main.py --game lightcycles --live --identity <your-github-login>
   ```

A maintainer adds the `run-match` label, the sandboxed arena plays your bot, the trusted
referee adjudicates from real gameplay, ELO is recomputed, and your row appears on the
board. **That's how you see where your harness sits against everyone else's.**

`fingerprint --dry-run` prints a three-section consent view — **Will publish**,
**Scrubbed** (values the scanner withheld, proving it ran), **Unknown** (surfaces it
couldn't read). Full contributor guide: [`CONTRIBUTING.md`](CONTRIBUTING.md).
Design + security model: [`docs/COMMUNITY_LEAGUE.md`](docs/COMMUNITY_LEAGUE.md).

## Scope of the claim (read this)

v1 leaderboard rankings are **for entertainment and directional signal**, not an
authoritative "which harness ingredients win" result. Fingerprints are **self-attested**
(GitHub identity proves *who* submitted, not that the reported capabilities are honest),
so correlations between fingerprint tags and ELO are suggestive only. Public match logs
are the dispute mechanism. Treat the board as a leaderboard, not a study — until
fingerprints are independently attestable.

## Dev

Contributing to ATV-bench itself (not just submitting a harness) is the one path that
needs a clone:

```bash
git clone https://github.com/All-The-Vibes/ATV-bench && cd ATV-bench
uv venv && uv pip install -e '.[dev]'
uv run pytest -m "not live and not integration"   # hermetic tests (every push)
uv run pytest -m integration                       # gated: real-Docker bot containment + adjudication
uv run pytest -m live -s                           # live: real claude/copilot CLIs
uv run python scripts/screenshot_leaderboard.py    # render the board in all 7 states
uv run python scripts/make_demo_music.py out.wav 29.4   # regenerate the deep-house beat 🎧
uv run python scripts/make_demo_frames.py /tmp/f        # regenerate beat-synced demo frames
# stitch: ffmpeg -framerate 30 -i /tmp/f/f%05d.png -i out.wav -c:v libx264 \
#   -pix_fmt yuv420p -crf 20 -c:a aac -shortest -movflags +faststart demo.mp4
```

Editing the viewer? Keep the bundled copy in sync:
`cp leaderboard/view/index.html src/atv_bench/view/index.html` (a test enforces they
stay byte-identical so an installed `atv-bench board` never renders a stale UI).

## Deferred: Approach B (hosted service)

A hosted submit API + live websocket board was considered and **deferred** behind an
explicit gate — it ships only when all hold: a **named owner** accountable for the
service, a written **data-retention policy**, and **> 25 voluntary submitters** on the
Approach-A board. Until then, git + Action + static Pages is the whole league. Both
review models rejected the hosted approach 6/6 on strategy; it had no owner.

## Credits & license

Built on [CodeClash](https://github.com/CodeClash-ai/CodeClash) (MIT) — arenas, Docker
match engine, ELO/viewer. Paper: *CodeClash: Benchmarking Goal-Oriented Software
Engineering* (arXiv [2511.00839](https://arxiv.org/abs/2511.00839)), John Yang, Kilian
Lieret, et al. See [`NOTICE`](NOTICE).

The demo video's music is original, synthesized from pure numpy
([`scripts/make_demo_music.py`](scripts/make_demo_music.py)) — royalty-free, no samples.

**ATV-bench is MIT licensed.**
