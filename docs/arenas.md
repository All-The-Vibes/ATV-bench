# CodeClash Arena Protocol Census

A per-arena classification of every game shipped under
`vendor/CodeClash/codeclash/arenas/`, pinned at CodeClash commit
`f0694c64ecf6abfca2bc867bad2de9333fef5be8`.

This census exists to decide which arenas ATV-bench can adjudicate under its own
harness contract: **a single `main.py` bot the referee drives turn-by-turn**
(`src/atv_bench/players.py::edit_turn`, see `src/atv_bench/games.py` for the live
set). An arena is only `supported` if a submission can be expressed as that
one-file, per-turn, 1‑v‑1, stdin/stdout Python contract. Every classification
below is read off the arena's own module (`<game>.py`: class attributes,
`execute_round`, `validate_code`, and the entry-point signature it enforces) —
not guessed.

## Columns

- **protocol** — how the bot is exercised:
  - `one-shot`: the whole submission (a compiled binary, engine, warrior, or
    event-driven class) is run once per match; no host-driven per-turn Python loop.
  - `iterative`: the bot is polled once per turn/tick and returns a move for the
    current state (`do_turn`, `get_move`, `robot(state, unit)`, engine frame loop).
  - `simultaneous`: every player is polled on the same tick and actions resolve
    together (multi-agent / order-book / negotiation models).
- **io** — how moves cross the boundary: `stdin-stdout`, `socket`, `http`, or
  `files` (compiled artifacts / directories the engine consumes).
- **support** — `supported` / `unsupported` / `experimental` against the
  `edit_turn`/`main.py` contract.
- **notes** — the specific code reason.

## Census

| game | protocol | io | support | notes |
|------|----------|-----|---------|-------|
| ants | iterative | stdin-stdout | supported | `submission="main.py"`; `validate_code` requires `def do_turn(obs)`. `engine.py` drives one long-lived bot process, polling per turn — same shape as lightcycles. |
| battlecode23 | one-shot | files | unsupported | `submission="src/mysubmission"`, Java bot compiled and run by the BattleCode 2023 real-time engine. Not a per-turn `main.py`; engine-/JVM-driven RTS. |
| battlecode24 | one-shot | files | unsupported | `submission="src/mysubmission"`, Java bot for BC2024 real-time engine. Same JVM/engine model as battlecode23 — no per-turn Python contract. |
| battlecode25 | one-shot | files | unsupported | `submission="src/mysubmission"`, Python bot but real-time engine-driven RTS (Soldiers/Moppers/Splashers + towers). Move model is engine-tick real-time, not an `edit_turn`/`main.py` poll. |
| battlesnake | iterative | http | unsupported | Bot is an HTTP **server**: `execute_round` starts it and the engine hits `http://localhost:<port>/`. Not a `main.py` the referee calls; it's a long-running web service. (Also `planned` in `games.py`.) |
| bomberland | simultaneous | socket | unsupported | `submission="bomberland_agent.py"`; multi-agent Bomberman via `runtime/run_bomberland.py`. All agents act per tick over the Coder One runtime socket protocol — cannot be a 1‑v‑1 per-turn `main.py`. |
| bridge | iterative | stdin-stdout | unsupported | `submission="bridge_agent.py"`, 4-player team card game (`get_bid`/`play_card`), run with a `ThreadPoolExecutor`. Team-of-four, not 1‑v‑1; no single `main.py` edit contract. |
| chess | one-shot | files | unsupported | `submission="src/"`; the bot is a compiled engine (`kojiro`) recompiled per round and run engine-vs-engine. A compiled binary speaking its own move protocol, not a per-turn Python `main.py`. |
| corewar | one-shot | files | unsupported | `submission="warrior.red"`; a Redcode assembly warrior executed inside the MARS VM. No code-turn contract at all — it's assembly, not a bot loop. |
| cyborg | simultaneous | socket | unsupported | `submission="cyborg_agent.py"`; CAGE‑3 DroneSwarm cyber-defense sim via `runtime/run_cyborg.py`. Multi-agent simultaneous environment, referee-/env-initiated — not `edit_turn`/`main.py`. |
| dummy | iterative | stdin-stdout | supported | `submission="main.py"`; `engine.py`-driven test arena that polls the bot per round. Infra smoke-test game, fits the per-turn `main.py` contract. |
| figgie | simultaneous | stdin-stdout | unsupported | `submission="main.py"` with `def get_action(state)`, but the description's own "Simultaneous Tick" model polls ALL 4–5 players each tick and resolves in random order. Multi-player simultaneous — not 1‑v‑1 `edit_turn`. |
| gomoku | iterative | stdin-stdout | supported | `submission="main.py"`; `validate_code` requires `def get_move(board, color)`. Alternating turn-based 1‑v‑1, polled per turn — a clean fit for the contract. |
| halite | iterative | stdin-stdout | unsupported | `submission="submission"` (a folder), multi-language bot compiled per round, driven by the Halite frame protocol with N>2 players. Not a single Python `main.py`; multiplayer compiled. |
| halite2 | iterative | stdin-stdout | unsupported | `submission` is `main.<ext>` in C++/Haskell/OCaml/Rust, compiled and run over the Halite II frame protocol. Non-Python compiled, multiplayer — outside the contract. |
| halite3 | iterative | stdin-stdout | unsupported | `submission="submission"`, compiled bot over the Halite III frame protocol. Same compiled/multiplayer shape as halite/halite2. |
| huskybench | one-shot | files | unsupported | `submission="client/player.py"`; poker sim run with `--sim --sim-rounds` over multiple players. Multi-player poker adjudicated by the sim, not a 1‑v‑1 per-turn `main.py`. |
| lightcycles | iterative | stdin-stdout | supported | `submission="main.py"`; `engine.py` polls each bot per tick for one of N/S/E/W. **The shipped live arena** (`games.py`) — the reference `edit_turn`/`main.py` contract. |
| paintvolley | iterative | stdin-stdout | supported | `submission="main.py"`; per-turn action (`LEFT/RIGHT/JUMP/...`), `engine.py`-driven 1‑v‑1. Matches the per-turn `main.py` contract. |
| robocode | one-shot | files | unsupported | `submission="robots/custom/"`; a Java class extending `robocode.Robot` whose `run()`/`onScannedRobot` callbacks ARE the tank. Event-driven JVM bot, not a per-turn stdin `main.py`. |
| robotrumble | iterative | files | unsupported | `submission="robot.js"`, Python **or JS** `robot(state, unit)` called **per unit per turn** over a 100-turn match. Downgraded from `experimental` in Wave C (see below): the per-turn shape looked close, but `robot(state, unit)` drives a *team of units* (many-vs-many within one function surface), not a 1‑v‑1 per-turn decision. A single-unit adapter would field one unit against a full team — an unfair, undefined-scoring match — so it is not honestly adjudicable. |
| scml | simultaneous | socket | unsupported | `submission="scml_agent.py"`; ANAC SCML OneShot supply-chain **negotiation** over `runtime/run_scml.py`. Concurrent simultaneous negotiations, env-initiated — not expressible as an `edit_turn`/`main.py` turn. |

## Summary

- **supported (5):** ants, dummy, gomoku, lightcycles, paintvolley — all
  `main.py`, single-player-per-turn, stdin/stdout, `engine.py`-driven. All five are
  wired live in `games.py` (Wave A). They are the complete set of arenas that fit the
  1‑v‑1 per-turn `edit_turn`/`main.py` contract.
- **experimental (0):** none. robotrumble was the sole experimental arena; Wave C
  adversarial verification downgraded it to `unsupported` (see Wave C below).
- **unsupported (17):** everything whose move model is compiled-binary
  (chess, corewar, robocode, battlecode*, halite*), an HTTP/socket server
  (battlesnake, bomberland, cyborg, scml), simultaneous / multi-player polling
  (figgie, bridge, huskybench), or many-vs-many team control (robotrumble). None can be
  expressed as a 1‑v‑1 per-turn `edit_turn`/`main.py` contract without a new referee.

## Wave C — verification of the 17 non-supported arenas

Waves C1/C2/C3 asked whether any of the 17 non-live arenas could be made live by
generalizing the harness driver beyond the single-`main.py` contract. Each was
re-read against the **actual** driver requirement (the harness edits a file *tree* in
the arena's Docker workdir; the arena's own referee adjudicates; the match must be a
strict 1‑v‑1 for pairwise Bradley-Terry) — a laxer bar than the single-`main.py`
census — and every superficially-plausible candidate was then handed to an independent
adversary tasked with refuting the "could go live" claim. **All three candidates were
refuted with concrete code evidence; the live set stays at 5.**

| candidate | initial read | adversarial verdict | refuting code fact |
|-----------|--------------|---------------------|--------------------|
| robotrumble | achievable (single-unit adapter) | **unsupported** | `robot(state, unit)` is called per *unit* per turn — a team of units, not one 1‑v‑1 decision. A single-unit adapter fields 1 unit vs a full team: unfair, undefined scoring. |
| robocode | live-now (source-editable `robots/custom/`) | **unsupported** | `execute_round` has **no** `len(agents)==2` assert and the bot is an event-driven JVM `Robot` subclass (`run()`/`onScannedRobot` callbacks), not a per-turn stdin loop the driver polls. |
| cyborg | achievable (add 1‑v‑1 assert) | **unsupported** | Each agent independently controls all 18 drones and is scored by **absolute reward**; `max(scores)→winner` is not a decisive A-beats-B outcome, so it cannot feed Bradley-Terry even with a 2-player assert. |

The remaining 14 (chess, corewar, battlecode23/24/25, halite/2/3, battlesnake,
bomberland, scml, figgie, bridge, huskybench) were unsupported on first read and not
contested: compiled non-source artifacts, HTTP/socket servers, or N>2 / simultaneous /
team play. See `tests/test_wave_c_arenas.py` for the pinned invariant.

**Conclusion:** every remaining arena would require authoring a *new referee* (or a new
rating model), which is explicitly out of scope (`IMPLEMENTATION_PLAN.md` → "New arenas
beyond battlesnake + lightcycles" is NOT in v1) and would violate the honest-adjudication
thesis. Wave C's deliverable is this verified negative result, not a set of fake-live games.
