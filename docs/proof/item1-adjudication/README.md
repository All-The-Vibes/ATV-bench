# Proof: Item 1 — Trusted adjudicated match outcome (RESOLVED)

This directory is the evidence that the CodeClash arena now **referees** the match and
emits the outcome from real gameplay inside the sandbox, instead of trusting the bot's
stdout for win/loss/draw. It closes the last open trust-boundary follow-up.

## What changed

- **Before:** `arena/Dockerfile` had `ENTRYPOINT ["python3"]`. The match job ran
  `/work/main.py` and passed **the bot's own stdout** through as the match result. A bot
  could print `{"status":"ok","outcome":"a_wins"}` and be believed.
- **After:** the ENTRYPOINT is the **trusted referee** (`python3 -m atv_bench.arena`).
  It runs a deterministic lightcycles/Tron engine (`src/atv_bench/arena/engine.py`),
  spawns the mounted bot as a **move-only subprocess** (one direction token per turn,
  per-turn timeout), plays it against a trusted in-process anchor, and prints the
  **adjudicated** result. A bot's stdout is only ever parsed as a move; anything else is
  an invalid move and forfeits.

## Evidence (produced under the exact production sandbox flags)

`docker run --network none --memory 512m --cpus 1 --pids-limit 128 --read-only
--user 65534:65534 --cap-drop ALL --security-opt no-new-privileges ...`

| Bot | File | Outcome | Meaning |
|-----|------|---------|---------|
| Honest move-player | `honest_result.json` | `draw` | Real game; both cyclists meet head-on |
| **Malicious result-faker** (prints `"outcome":"b_wins"`) | `malicious_result.json` | **`forfeit_b`** | Fabricated win discarded; submitter LOSES |
| Wall-diver | `suicide_result.json` | `a_wins` | Submitter crashes; trusted anchor WINS |

The malicious bot printed a fully-formed winning result every turn and still **lost** —
that string was consumed as an invalid move, not as an outcome. The bot cannot inject a
win.

## Visual

`board_render.png` / `board_render.txt` render a refereed match's real trail geometry
(blue = trusted anchor, orange = submitter). The outcome is derived from where the
cyclists actually collided, not from anything either bot claimed.

## Reproduce

```bash
# hermetic unit + tripwire tests (every push)
uv run pytest -m "not live" tests/test_arena_engine.py tests/test_arena_referee.py \
  tests/test_arena_entrypoint.py tests/test_arena_image.py

# end-to-end under the real Docker sandbox (integration lane)
uv run pytest -m integration tests/test_arena_adjudication.py
```
