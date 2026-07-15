# ATV-bench Community League

A community leaderboard for coding-agent **harnesses**. Where SWE-bench and
CodeClash measure the **model**, ATV-bench measures the whole **harness** — your
skills, MCP servers, plugins, custom agents, and config — by putting harness-built
bots head-to-head in a game arena (Battlesnake, Tron) and publishing an ELO score
alongside a leak-safe fingerprint of each harness.

You submit **a bot + your harness fingerprint** (never a self-reported result). A
GitHub Action runs the matches, recomputes ELO from scratch, and publishes a static
leaderboard on GitHub Pages. No hosted server to run or trust.

## How it works (Approach A — git + Action + static Pages)

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
job never executes bot code — it only reads a schema-validated result artifact.

## Scope of the claim (read this)

v1 leaderboard rankings are **for entertainment and directional signal**, not an
authoritative "which harness ingredients win" result. Fingerprints are **self-attested**
(GitHub-OAuth identity proves *who* submitted, not that the reported capabilities are
honest), so correlations between fingerprint tags and ELO are suggestive only. Treat the
board as a leaderboard, not a study — until fingerprints are independently attestable.

## Quick start (zero to board)

```bash
# 1. install
uv venv && uv pip install -e '.[dev]'

# 2. see exactly what your harness would publish (nothing leaves your machine)
atv-bench fingerprint --dry-run

# 3. validate + build your submission (bot + leak-safe fingerprint)
atv-bench validate-game ./main.py
atv-bench submit ./main.py --game battlesnake --dry-run \
  --identity <your-github-login> --out submission.json

# 4. commit the bot + submission.json under league/submissions/ and open a PR
#    (live PR automation is not wired yet — see CONTRIBUTING.md#manual-pr-fallback)
```

`fingerprint --dry-run` prints a three-section consent view — **Will publish**,
**Scrubbed** (values the scanner withheld, proving it ran), **Unknown** (surfaces it
couldn't read). No secret-shaped value is ever published. Full contributor guide:
[`CONTRIBUTING.md`](CONTRIBUTING.md). Design + security model:
[`docs/COMMUNITY_LEAGUE.md`](docs/COMMUNITY_LEAGUE.md).

## Dev

```bash
uv run pytest -m "not live and not integration"   # fast hermetic suite (every push)
uv run pytest -m integration                       # gated: real-Docker bot containment
uv run pytest -m live -s                           # live: real claude/copilot CLIs
uv run python scripts/screenshot_leaderboard.py    # render the board in all 7 states
```

## Deferred: Approach B (hosted service)

A hosted submit API + live websocket board was considered and **deferred** behind an
explicit gate. It does not ship in v1 unless all of the following hold:

- a **named owner** accountable for the service (auth, DB, ops, on-call);
- a written **data-retention policy** for stored bots + fingerprints;
- **> 25 voluntary submitters** on the Approach-A board (demonstrated demand).

Until then the git + Action + static-Pages model is the whole league. Re-scope decision:
both review models rejected the hosted approach 6/6 on strategy; it had no owner.

## Credits & license

Built on [CodeClash](https://github.com/CodeClash-ai/CodeClash) (MIT) — arenas,
Docker match engine, ELO/viewer. Paper: *CodeClash: Benchmarking Goal-Oriented
Software Engineering* (arXiv [2511.00839](https://arxiv.org/abs/2511.00839)),
John Yang, Kilian Lieret, et al. See `NOTICE`.

ATV-bench is MIT licensed.
