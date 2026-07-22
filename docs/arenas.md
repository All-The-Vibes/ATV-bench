# CodeClash Arena Protocol Census

A per-arena classification of every game shipped under
`vendor/CodeClash/codeclash/arenas/`, pinned at CodeClash commit
`f0694c64ecf6abfca2bc867bad2de9333fef5be8`.

This census decides which arenas ATV-bench can adjudicate. **The referee already exists**
for every arena — it ships in CodeClash. ATV-bench does not write referees; it reuses
them. The real contract (read from `vendor/CodeClash/codeclash/arenas/arena.py::run_round`
and `tournaments/pvp.py`, and confirmed by a live end-to-end matrix) is:

- The ATV-bench harness (`claude-code` / `copilot-cli`) edits the arena's **submission** —
  source in any language (`main.py`, a `src/` C++ tree, `robots/custom/` Java, a
  `submission/` folder, `warrior.red` Redcode, `*_agent.py`) — inside the arena's Docker
  workdir. The arena's own image compiles/interprets it (`make native`, `javac`, etc.).
- CodeClash's `run_round` validates each submission, runs the arena's `execute_round`, and
  its `get_results` reduces the match to a single decisive `RoundStats.winner`.
- The tournament passes **exactly 2 players** for a 1‑v‑1 (ATV `build_pvp_config`), or the
  arena's required N players (4–5 for figgie/bridge, filled with harness + bare-model
  seats). Either way the referee returns one winner.

An arena is `supported` only if a **real end-to-end match actually scored** — a Docker
build + live harness bots + arena adjudication producing a non-crash `RoundStats` with
validated submissions and a decisive/scored round. This is a higher bar than "the referee
looks reusable": it was verified by running one live match per arena (see the committed proof
`docs/proof/wave-c/matrix.json` — 20/22 arenas scored — and § "Wave C — end-to-end
verification").

> **History:** an earlier revision of this census wrongly concluded that all 17 non-Wave-A
> arenas were "unsupported / would need a new referee". That was based on a too-strict
> "single stdin `main.py`" reading and analysis without running anything. The live matrix
> disproved it: 15 of the 17 score real matches by reusing CodeClash's existing referees.

## Columns

- **protocol** — how the bot is exercised: `one-shot` (compiled/engine-driven bot run per
  match), `iterative` (polled per turn/tick), `simultaneous` (all players act per tick).
- **io** — how moves cross the boundary: `stdin-stdout`, `socket`, `http`, or `files`.
- **support** — `supported` (a real e2e match scored) / `unsupported` (did not).
- **notes** — the submission the harness edits + how the referee adjudicates + e2e result.

## Census

| game | protocol | io | support | notes |
|------|----------|-----|---------|-------|
| ants | iterative | stdin-stdout | supported | `submission="main.py"`, `def do_turn(obs)`. `engine.py` polls the bot per turn. Wave A. |
| battlecode23 | one-shot | files | supported | `submission="src/mysubmission"`, Java bot compiled in-arena and driven by the BC2023 engine. `get_results` counts engine wins → decisive winner. **e2e: PASS.** |
| battlecode24 | one-shot | files | supported | `submission="src/mysubmission"`, Java bot for the BC2024 engine, compiled in-arena. **e2e: PASS.** |
| battlecode25 | one-shot | files | unsupported | Reusable referee, but CodeClash's `get_results` does `max(scores)` with **no empty guard** (unlike bc23/24); a round with no decisive sim leaves `scores` empty → `ValueError`. **e2e: FAIL (upstream crash).** Not an architectural block — awaiting an upstream one-line fix. |
| battlesnake | iterative | http | supported | `submission="main.py"` (or source + `run.sh`). The **arena** starts the bot's HTTP server from committed source (`PORT=… python main.py &`), plays the games, and `get_results` picks the winner by wins. The harness never hosts a server. **e2e: PASS.** |
| bomberland | simultaneous | socket | supported | `submission="bomberland_agent.py"` (`def next_actions(game_state)`). The arena's `runtime/run_bomberland.py` hosts the socket env and drives the submitted agent. **e2e: PASS.** |
| bridge | iterative | stdin-stdout | supported | `submission="bridge_agent.py"`, 4‑player partnership card game (`get_bid`/`play_card`). Runs with 4 seats filled by harness + bare-model variants; `get_results` scores NS vs EW. **e2e: PASS.** |
| chess | one-shot | files | supported | `submission="src/"` C++ (Kojiro). `validate_code` compiles it in-arena with `make native`; `fastchess` plays engine-vs-engine and `get_results` counts wins. **e2e: PASS.** |
| corewar | one-shot | files | supported | `submission="warrior.red"` Redcode, executed in the MARS VM (`pmars`). `get_results` tallies battle wins → decisive winner. **e2e: PASS.** |
| cyborg | simultaneous | socket | supported | `submission="cyborg_agent.py"` (`def decide(obs, action_space)->int`). `runtime/run_cyborg.py` hosts the CAGE sim; `get_results` `max(scores)` over 2 agents IS a decisive pairwise winner. **e2e: PASS.** |
| dummy | iterative | stdin-stdout | supported | `submission="main.py"`; `engine.py`-driven smoke arena. Wave A. |
| figgie | simultaneous | stdin-stdout | supported | `submission="main.py"` (`def get_action(state)`). Requires 4–5 players; run with 4 seats filled by harness + bare-model variants. `get_results` `max(scores)` → decisive winner. **e2e: PASS.** |
| gomoku | iterative | stdin-stdout | supported | `submission="main.py"`, `def get_move(board, color)`. Alternating 1‑v‑1. Wave A. |
| halite | iterative | stdin-stdout | supported | `submission="submission"` (a folder), compiled-from-source bot over the Halite frame protocol. `get_results` picks the rank‑1 winner. **e2e: PASS.** |
| halite2 | iterative | stdin-stdout | supported | `submission="submission"`, compiled multi-language bot (OCaml/C++/…) over the Halite II protocol; inherits `HaliteArena.get_results`. **e2e: PASS.** |
| halite3 | iterative | stdin-stdout | supported | `submission="submission"`, compiled bot over the Halite III protocol; `get_results` delegates to `HaliteArena`. **e2e: PASS.** |
| huskybench | one-shot | files | supported | `submission="client/player.py"` poker bot; the sim runs the bots and `get_results` `max(scores)` picks the chip winner. **e2e: PASS.** |
| lightcycles | iterative | stdin-stdout | supported | `submission="main.py"`; `engine.py` polls per tick. The reference Wave A arena. |
| paintvolley | iterative | stdin-stdout | supported | `submission="main.py"`, `def get_action(obs)`. `engine.py`-driven 1‑v‑1. Wave A. |
| robocode | one-shot | files | unsupported | Reusable JVM referee (`robots/custom/` Java, `./robocode.sh`), but `get_results` does `max(scores)` with **no empty guard**; a round with no parseable battle result leaves `scores` empty → `ValueError`. **e2e: FAIL (upstream crash).** Awaiting an upstream fix. |
| robotrumble | iterative | files | supported | `submission="robot.py"` (or `robot.js`), `def robot(state, unit)` — one bot commanding a team of units (like ants). `assert len(players)==2`; `get_results` picks Blue/Red by wins. **e2e: PASS.** |
| scml | simultaneous | socket | supported | `submission="scml_agent.py"` supply-chain negotiation (`decide(observation)`). `runtime/run_scml.py` hosts the negotiation; `get_results` scores profit. **e2e: PASS.** |

## Summary

- **supported (20):** the 5 Wave A arenas (ants, dummy, gomoku, lightcycles, paintvolley)
  plus 15 Wave C arenas proven by a real end-to-end scored match — corewar, robotrumble,
  battlesnake, huskybench, scml, chess, halite, halite2, halite3, cyborg, bomberland,
  battlecode23, battlecode24, figgie, bridge. All are live in `games.py`.
- **unsupported (2):** robocode, battlecode25 — reusable referees blocked ONLY by an
  upstream CodeClash bug (`get_results` does `max(scores)` with no empty guard; their
  siblings battlecode23/24 guard it). They crash on a no-decisive-sim round. Not an
  architectural mismatch — a one-line upstream fix would flip them supported.

## Wave C — end-to-end verification

Waves C1/C2/C3 asked whether the 17 non-Wave-A arenas could be made live. The answer,
proven by running real matches rather than by analysis:

1. **Reassessment** against the actual driver contract (harness edits source; the arena's
   own image compiles/runs it; CodeClash's `run_round` returns a decisive winner; the
   tournament supplies the required player count) showed 15 of 17 are reuse-candidates —
   the "compiled binary", "socket server", "N>2", and "multi-unit team" objections all
   fail once you read CodeClash's own referee code.
2. **A live end-to-end matrix** then ran one real match per arena — Docker build + live
   `claude-code` bots (and, for the 4‑player games, a mix of bare-model `mini` seats and
   harnessed seats) + real arena adjudication. Result across all 22 arenas: **20 PASS, 2
   FAIL** (committed proof: `docs/proof/wave-c/matrix.json`). Only robocode and battlecode25
   failed, both on the identical upstream unguarded-`max(scores)` crash.

Getting there surfaced (and fixed) seven real integration defects that each would have
produced a false "unsupported" under analysis alone:

| fix | defect the live run exposed |
|-----|------------------------------|
| capture allowlist | rejected multi-language bot source (only `.py`/text were allowed) |
| build context | `docker build` ran from the wrong cwd for `runtime/`-COPY arenas (cyborg, bomberland) |
| replay/log skip | a 6MB Halite `.hlt` replay tripped the per-file size cap |
| git origin | `git init` arenas (cyborg, bomberland) had no `origin` for CodeClash's `git fetch` |
| capture bounds | 668-file multi-language SDK seed trees exceeded the file-count cap |
| scoped scan | the scan re-audited the trusted seed (a vendored library's sample DKIM key) instead of only the harness's changes |
| bare-seat routing | the 4‑player games' bare `mini` seats needed the host's Portkey gateway headers |

The two unsupported arenas keep a real referee and a real reason (the upstream crash), so a
future CodeClash bump that guards the empty case flips them to supported with only a
`games.py`/census update — no new referee. Wave C added **no** bespoke referee to
`src/atv_bench/arena/` (still lightcycles only); every live arena reuses CodeClash's own.
