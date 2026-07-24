# Real fixture contract (captured from /tmp/atv-quickstart-2a03gky9)

Implement parsers against THESE shapes, not the plan's prose (the plan guessed
`width/height/rocks`; reality differs per game). Fixtures live in
`tests/fixtures/rounds/*.tar.gz`. Each tar has members under `0/`:
`sim_N.json` (ants/lightcycles), `match_N.pgn` (chess), and `results.json`.

## ants  (tests/fixtures/rounds/ants-0_round_0.tar.gz)
sim_*.json top-level: `rows` (int), `cols` (int), `num_players`, `water` (list),
`frames` (list), `names` (["claude-code","bare-claude-code"]), `winner` (int index).
frame keys: `t` (turn int), `ants` [[x,y,player],...], `hills` [[x,y,player],...],
`food` [[x,y],...]. ~501 frames.

## lightcycles  (tests/fixtures/rounds/lightcycles-2_round_0.tar.gz)
sim_*.json top-level: `width`, `height`, `num_players`, `rocks` (list), `frames`,
`names`, `winner`. frame keys: `t`, `heads` (per-player head positions; derive
trails by accumulating heads over frames). ~205 frames.

## chess  (tests/fixtures/rounds/chess-1_round_0.tar.gz)
`match_N.pgn` standard PGN. Headers: White/Black = claude-code / bare-claude-code,
Result "1-0"|"0-1"|"1/2-1/2". Parse with python-chess -> FEN per ply.
`results.json` keys: round_num, winner, details, scores, player_stats.

## Seat convention (drives D1 winner-color + D3 legend)
Player index 0 = `claude-code` = HARNESS = blue (--a). Index 1 =
`bare-claude-code` = CONTROL = red (--b). `winner` int in sim/results indexes
`names`. Map winner->seat color for the round strip. Bare name is
`bare-claude-code` on disk (hyphen), display as `bare:claude-code`.
