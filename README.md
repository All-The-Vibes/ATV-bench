<div align="center">

# 🏁 ATV-bench — League and Harness Evaluation

### The League ranks submitted bots. The evaluation tracks measure harnesses.

Your skills. Your MCP servers. Your plugins, custom agents, and config.
Those may affect what ships, but a credible harness claim requires fresh, controlled,
attested trials—not a fingerprint attached to one frozen bot.

[![hermetic tests](https://img.shields.io/badge/tests-hermetic%20suite-6ce7be?style=flat-square)](#dev)
[![docker adjudication](https://img.shields.io/badge/arena-referee%20adjudicated-7aa2ff?style=flat-square)](#the-trust-boundary)
[![leak-safe](https://img.shields.io/badge/fingerprint-leak--safe-ffc45c?style=flat-square)](#the-credibility-gate)
[![license](https://img.shields.io/badge/license-MIT-e8ecf5?style=flat-square)](LICENSE)

https://github.com/user-attachments/assets/438771f0-4886-4185-9c75-85c8d9c35bd9

</div>

---

## What exists today

ATV-bench is intentionally split into separate products:

- **ATV League (shipping):** ranks frozen submitted bots in a trusted Lightcycles arena.
  A leak-safe harness fingerprint is descriptive metadata, not proof of how the bot was
  produced.
- **ATV Controlled:** compares harness behavior while holding model, task, tools,
  environment, budget, and schedule fixed.
- **ATV Systems:** compares complete preferred stacks, including model and tools.
- **ATV Resilience:** evaluates recovery under injected failures and pressure.

Only the League currently publishes online Elo. Controlled, Systems, and Resilience
results require fresh harness executions, hidden post-run grading, attested model and
runner identity, and task-clustered uncertainty before they can be official. See
[`docs/PRODUCTS_AND_TRACKS.md`](docs/PRODUCTS_AND_TRACKS.md) and the
[`benchmark blueprint`](docs/HARNESS_BENCHMARKING_BLUEPRINT.md).

## How it works — reviewed data + static Pages

```
 local/offline execution                   this repo (GitHub)
 ┌────────────────────────────┐  PR        ┌──────────────────────────────┐
 │ bot/harness/model execution │ ─────────▶ │ reviewed submissions/results │
 │ local or approved runner    │            │ ordinary tests only          │
 └────────────────────────────┘            └──────────────┬───────────────┘
                                                         │ protected push
                                                         ▼
                                          static leaderboard (GitHub Pages)
```

**GitHub Actions never executes submitted bots, harnesses, model calls, arenas, trials,
or benchmark evaluations.** Actions run ordinary code/security tests and rebuild GitHub
Pages after reviewed data reaches the protected default branch.

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

## The privacy gate — leak-safe harness fingerprint

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

### Experimental local harness comparison

The local `run` path invokes supported harness CLIs to build bots and then evaluates the
artifacts in a CodeClash arena. It is a development and conformance surface, not an
official scientific ranking.

The zero-to-board tool install above intentionally omits the heavier CodeClash run
dependency. For this experimental path, use a checkout so the immutable dependency and
its arena Dockerfiles are both available:

```bash
git clone https://github.com/All-The-Vibes/ATV-bench.git
cd ATV-bench
uv sync --extra run
uv run atv-bench doctor
```

`uv` retains the exact CodeClash source checkout needed for its non-wheel arena assets.
An operator-managed checkout may instead be supplied through `ATV_CODECLASH_SOURCE`; it
must resolve to the pinned commit reported by `doctor`. The first Lightcycles run builds
`codeclash/lightcycles` from a packaged digest-pinned base definition and the frozen
LightCycles revision, then verifies both image labels and the in-image Git commit.

Start with the zero-setup real recording — no Docker, no auth, no network:

```bash
atv-bench run --demo                 # replays a canned but REAL recorded match
atv-bench run --demo --json          # same, as a stable machine-readable envelope
```

Then run a live match (needs Docker + a real harness CLI authenticated — see `doctor`):

```bash
uv run atv-bench run --game lightcycles --a copilot-cli --b claude-code --model claude-opus-4.8
uv run atv-bench run --game battlesnake  --a claude-code --b claude-code --model claude-opus-4.8 --json
uv run atv-bench run --list-games --list-harnesses     # discover valid values
```

Both harnesses receive the same **requested model label**, but that alone does not prove
the same provider deployment, snapshot, parameters, retries, routing, or subagent
models. Therefore this path does **not** isolate the harness effect. Results are labeled
**unverified / local-debug** and do **not** publish a scientific rank. Official
Controlled results require gateway-observed resolved-model evidence and equal enforced
budgets. Exit codes remain stable and distinct per failure mode so agents and CI can
branch on them.

### Local benchmark workflow

Harness benchmark execution is local-only. It is not triggered by GitHub Actions and
does not upload or publish scores. League bot execution is local or performed by a
separately approved runner outside GitHub Actions.

```bash
atv-bench benchmark schema check ./schemas
atv-bench benchmark harness validate \
  ./examples/harnesses/generic-command/harness.json
atv-bench benchmark task validate ./tasks/smoke/repair_config

atv-bench benchmark eval plan \
  --task ./tasks/smoke/repair_config \
  --harness ./path/to/your-oci-protocol-harness.json \
  --out ./local-eval/plan.json

atv-bench benchmark eval run ./local-eval/plan.json --out ./local-eval/results
atv-bench benchmark eval verify ./local-eval/results
atv-bench benchmark eval analyze ./local-eval/results \
  --harness-a <id-a> --harness-b <id-b> --out ./local-eval/analysis
atv-bench benchmark eval reproduce ./local-eval/results/<trial-directory>
```

Every command emits `trust_tier=local-self-attested` and `rankable=false`. Official
status requires independently verifiable signatures, private-task operations, human
task review, and the remaining release gates; local flags cannot promote a result.

Protocol-v1 OCI manifests use a real attached
`request → hello → accepted → result` exchange. The runner inspects the live container
before exact removal, mounts hidden grader inputs only afterward, and can enforce one
Docker-backed aggregate quota across `/workspace`, `/artifacts`, and `/tmp`. Strict
evidence runs select `HARD_QUOTA`; `AUTO` may fall back to a clearly labeled bind
monitor when the engine lacks the Linux Docker quota capability.

`eval plan`, `eval run`, and `trial smoke` require digest-pinned OCI manifests.
Process manifests such as the generic-command example remain supported for validation,
adapter conformance, and `harness-run`, but are rejected before an isolated evaluation
plan is created.

### See the rankings first

Before you submit anything, look at the board — including a populated sample so you
know what you're aiming at:

```bash
atv-bench board --demo    # builds a sample leaderboard and opens it in your browser
```

The live community board is at
**https://all-the-vibes.github.io/ATV-bench/** — it ranks submitted **bots/identities**
in ATV League. Fingerprint chips describe the submitter's self-attested configuration;
they are not a harness score. To render the board locally from a checkout's store:
`atv-bench board --store league`.

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
| `bare` | A deliberately minimal reference bot: go straight, turning only to avoid crashing. It is a gameplay baseline, not a raw-model or no-harness control. |

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

### Submit a bot to ATV League (step by step)

1. **Pick a game.** See what's playable:
   ```bash
   atv-bench games        # lightcycles is live; battlesnake is planned
   ```
2. **Build a bot.** You may use any harness or workflow to produce a single-file
   `main.py` that plays the arena. The League ranks the submitted bot/identity. It does
   not verify or rank the harness that produced it.
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
6. **Open the PR**:
   ```bash
   atv-bench submit ./main.py --game lightcycles --live --identity <your-github-login>
   ```

The PR contributes reviewed submission data only. GitHub Actions will validate the
repository and may deploy Pages, but it will not run the bot. Match records must be
produced locally or by an approved external runner and enter `league/` through a separate
reviewed PR. **League history describes the bot, not general harness quality.**

`fingerprint --dry-run` prints a three-section consent view — **Will publish**,
**Scrubbed** (values the scanner withheld, proving it ran), **Unknown** (surfaces it
couldn't read). Full contributor guide: [`CONTRIBUTING.md`](CONTRIBUTING.md).
Design + security model: [`docs/COMMUNITY_LEAGUE.md`](docs/COMMUNITY_LEAGUE.md).

## Scope of the claim (read this)

ATV League is a bot competition for entertainment and community signal. Its online Elo
must not be used to declare a harness winner. Fingerprints are **self-attested**
(GitHub identity proves *who* submitted, not what executed), and one frozen artifact
cannot establish general harness quality. Official harness comparisons belong in ATV
Controlled or ATV Systems and require fresh independent trials, enforced budgets,
hidden grading, attestations, and task-clustered uncertainty. An inconclusive result is
not converted into a winner.

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

The experimental local harness runner integrates
[CodeClash](https://github.com/CodeClash-ai/CodeClash) (MIT). The public ATV League uses
ATV-bench's own trusted Lightcycles referee and viewer. CodeClash paper:
*CodeClash: Benchmarking Goal-Oriented Software Engineering* (arXiv
[2511.00839](https://arxiv.org/abs/2511.00839)), John Yang, Kilian Lieret, et al. See
[`NOTICE`](NOTICE).

The demo video's music is original, synthesized from pure numpy
([`scripts/make_demo_music.py`](scripts/make_demo_music.py)) — royalty-free, no samples.

**ATV-bench is MIT licensed.**
