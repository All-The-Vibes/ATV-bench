# Wave C — live-match evidence (PR #19 follow-up 4)

`matrix.json` is the reproducible evidence backing the `live=True` flags in
`src/atv_bench/games.py`. It is produced by a REAL end-to-end live matrix — for every
CodeClash arena: Docker image build + a live `claude-code` harness editing the arena's own
submission + real arena adjudication — one scored match per arena.

## Result: 20 / 22 arenas produce a real scored match

| Class | Arenas | Live? |
|---|---|---|
| **Pass** (scored, non-crash match) | ants, battlecode23, battlecode24, battlesnake, bomberland, bridge, chess, corewar, cyborg, dummy, figgie, gomoku, halite, halite2, halite3, huskybench, lightcycles, paintvolley, robotrumble, scml | ✅ `live=True` (20) |
| **Upstream-blocked** | robocode, battlecode25 | ❌ `live=False` |

The two blocked arenas fail with the SAME documented upstream CodeClash bug —
`ValueError: max() iterable argument is empty` (`get_results` does an unguarded
`max(scores)` on a round with no decisive sim). This is not an ATV architectural mismatch;
their siblings battlecode23/24 guard the empty case. They stay `live=False` until upstream
guards it.

**Zero drift:** every arena flagged `live=True` in `games.py` has a passing row here, and
neither blocked arena is flagged live. `tests/test_wave_c_evidence.py` enforces this — it
fails if any `live=True` arena lacks a passing proof row.

## Methodology note (why a re-run layer exists)

A single batch `--all` run degrades the Docker daemon under sustained load (RWLayer-nil
storage races, OOM exit 137, transient "No such container"), so a FAIL in one batch pass is
NOT a verdict. The authoritative verdict for any arena that failed transiently is an ISOLATED
re-run (`scripts/rerun_failed_arenas.py`) with a docker prune + settle between arenas; those
overrides supersede the batch result during consolidation. Every one of the 14 arenas that
failed the batch on infrastructure noise passed cleanly in isolation — only the 2
upstream-bug arenas remained red.

Two packaging fixes were required for full coverage: the CodeClash package must be installed
so its per-arena `*.Dockerfile` and `configs/mini/*.yaml` data resolve (editable install from
`vendor/CodeClash`), otherwise ants/figgie/bridge fail on missing data files unrelated to the
arena logic.

## Regenerate

```bash
python scripts/e2e_arena_matrix.py --all                 # batch, writes _e2e/<arena>/verdict.json
python scripts/rerun_failed_arenas.py <failed arenas>    # isolated re-runs -> _e2e/rerun/<arena>/
python scripts/consolidate_wave_c_proof.py               # -> docs/proof/wave-c/matrix.json + drift report
```
