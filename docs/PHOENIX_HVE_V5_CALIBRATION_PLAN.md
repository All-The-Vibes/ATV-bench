# Phoenix versus hve-core v5 evaluator calibration plan

Frozen on Monday, July 20, 2026, before any v5 calibration attempt.

## Purpose

V4 proved that 60 credits is sufficient for both harnesses to produce valid
artifacts, but it did not calibrate whole-match runtime. V5 calibrates the complete
public path: harness completion, artifact validation, and a bounded side-swapped
Lightcycles pair.

Calibration remains non-scored.

## Frozen cell

| Field | Value |
|---|---|
| Phoenix commit | `233e8e1e968bbc0b1dc446d7830efa82489bf118` |
| hve-core commit | `5c15a03c78da2408527693e0fc3b3e387bf99cb2` |
| Copilot CLI | `GitHub Copilot CLI 1.0.72-1` |
| Model | explicit `gpt-5.4` |
| AI-credit budget | 60 |
| Harness timeout | 1,200 seconds |
| Board profile | `compact` |
| Maximum game turns | 40 |
| Per-turn timeout | 3.0 seconds |
| Per-match timeout | 60.0 seconds |
| Public calibration seed | 42, played with sides swapped |
| Required attempts | 2 |
| Required pass rate | 100% |

## Frozen implementation identities

| Component | SHA-256 |
|---|---|
| `scripts/compare_phoenix_hve.py` | `227a4da5a6e3c50031d4d0e0e63799f1afac2ece4f53e26ffdd6126da59a4366` |
| `scripts/summarize_phoenix_hve_calibration.py` | `6384a1cfbe0a43e8c69cf15994901fb12bf1dd72461723cb9f24012b4c4af175` |
| `src/atv_bench/comparison.py` | `183f93036d947601ad3ea7b5c3ec50a31d6720a342b31147f24a5f8525301ed2` |
| `src/atv_bench/arena/engine.py` | `c6eeb8ceea85af433ba8274c7c16c8d7c3444a070b60b17d61cb5ed1047839f3` |
| `src/atv_bench/arena/referee.py` | `4a2523cb6335562f4ecd1e1d9a8ac252b5fef3e42ed97a7cc133773f860ca60c` |

Any identity change creates a new cell.

## Pass criteria

Each attempt passes only when:

1. both model receipts pass;
2. both harness executions terminate successfully;
3. both `main.py` artifacts compile and pass smoke validation;
4. both public side-swapped games finish without a bot forfeit;
5. neither public game reaches `MATCH_TIMEOUT`.

If both attempts pass, freeze a new v5 evaluation plan using the same runtime
contract. If either fails, do not launch evaluation; change the task contract only in
a separately versioned calibration cell.

