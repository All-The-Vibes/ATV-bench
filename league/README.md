# League store

The committed data the publish job builds the leaderboard from.

- `submissions/<identity>.json` — one per entrant: bot metadata + harness fingerprint
  (added by a merged submission PR).
- `matches.jsonl` — append-only match history; the match job appends a validated
  result via `python -m atv_bench.publish ingest`.

The publish job recomputes ELO from full history on every build (deterministic,
order-independent), so this store is the single source of truth for the board.
