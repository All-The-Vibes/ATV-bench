# E2E live-CLI testing for quickstart — TDD plan

## Goal

Close three live-path gaps by TDD e2e tests that drive the REAL CLIs (no mocks for the thing
under test), using agent teams to orchestrate. Branch: `feat/quickstart-e2e` off `feat/quickstart`.

1. A real `claude-code` vs `bare:claude-code` match produces a sensible lift.
2. The codex adapter drives the REAL `codex` CLI end-to-end (real `codex exec --json` shape).
3. The interactive arrow-key picker driven in a REAL TTY (not stubbed questionary).

## Reconnaissance findings (already captured against the real CLIs)

- **All three CLIs present + authed**, Docker up, `ANTHROPIC_API_KEY` set.
- **claude adapter is CORRECT** vs real output: `claude -p ... --output-format json` emits
  `modelUsage: {"claude-opus-4-8[1m]": {...}}` → `_parse` reads `claude-opus-4-8[1m]`. ✅
- **copilot adapter is CORRECT** vs real output: emits `assistant.message` events carrying
  `"model":"claude-opus-4.8"` → `parse_copilot_model` reads it. ✅
- **codex adapter is BROKEN** vs real output — THE bug this effort exists to catch:
  real `codex exec <goal> --json --dangerously-bypass-approvals-and-sandbox` emits
  `thread.started` / `turn.started` / `item.completed{item:{type:agent_message|command_execution|file_change}}` /
  `turn.completed{usage:{input_tokens,...}}`. **There is NO `model` field anywhere in the stream.**
  So `parse_codex_model` returns `"unknown"` in production. It also DOES perform real edits
  (file_change events, exit 0). MUST FIX: get the model some other way (e.g. `codex --version` /
  a `-c model=` echo / config read) OR honestly record `unknown` and stop asserting a parsed model.

## Test taxonomy (so nothing is a false "e2e")

These tests hit real CLIs → gated `@pytest.mark.live` (the repo's existing marker for real-CLI
tests; NOT run in the hermetic lane). They require auth + network and are slow. A separate
`live-integration` CI lane already runs `-m 'integration or e2e or drift'`; we add `live` there
or a dedicated invocation. Each test SKIPS cleanly (not fails) when its CLI/auth is absent, so the
suite is runnable anywhere without going red for the wrong reason.

## Units (TDD, RED→GREEN), orchestrated by an agent team

### Unit A — codex adapter e2e (drives real `codex exec`)  [the bug-fix unit]
- `tests/test_codex_adapter_live.py` (`@pytest.mark.live`, skip if `shutil.which('codex')`/auth absent):
  - `test_codex_exec_really_edits`: run `CodexCliAdapter().run(...)` against a real temp git repo
    with a concrete edit goal; assert status EDITED and a real diff touching the target file.
  - `test_codex_model_is_resolved_not_unknown`: the KILLER test — assert the adapter reports a
    REAL model id (not `"unknown"`) for a real run. This FAILS on today's code (RED), proving the
    canned-payload gap. GREEN = fix the model resolution.
- Fix in `adapters/contract.py`: since `--json` carries no model, resolve it from a reliable
  source — try `-c model=<...>`/config, or capture the model from `codex exec` a different way
  (e.g. a `codex exec --model <m>` echoes the *requested* id which, when explicitly passed, IS the
  authoritative one; when omitted, read `~/.codex/config.toml` default via the existing fingerprint
  reader). Fail closed to `unknown` only when genuinely unknowable. Update the CANNED unit test to
  match the real event shape (thread.started/turn.completed), so the hermetic test stops asserting
  a fictional shape.

### Unit B — claude-code vs bare:claude-code real lift  [the headline e2e]
- `tests/test_quickstart_live_lift.py` (`@pytest.mark.live`, skip if no claude/docker):
  - `test_real_claude_vs_bare_produces_finite_lift`: run the quickstart engine with the REAL
    `live_match_executor` on ONE fast game (dummy or lightcycles), `repeats>=? ` small, harness =
    claude-code, baseline = bare:claude-code. Assert: matches actually ran (n_matches>0), a rating
    corpus persisted, `overall` lift is finite (not None/NaN), per-game score present, and the
    scorecard rendered. "Sensible" = finite + within [-plausible, plausible], and the bare control
    genuinely ran under a stripped HOME (manifest_is_bare on its fingerprint).
  - Keep it to 1 game × few repeats to bound cost/time; the point is the SEAM works live, not a
    powered corpus.

### Unit C — interactive picker in a real TTY
- `tests/test_interactive_tty.py` (`@pytest.mark.live` or a `pty` guard):
  - Drive `select_model` (or the `quickstart` picker path) under a real pseudo-terminal (`pty`
    + `pexpect`-style, or stdlib `pty.openpty`), send arrow-down + Enter, assert the SELECTED
    (non-default) model is returned. This proves the questionary path works in a real terminal,
    not just with `_questionary_select` stubbed.
  - Also a non-TTY assertion that the fallback still returns deterministically (already covered
    hermetically; re-assert under a real closed stdin).

### Unit D — a `live-e2e` runner script + CI wiring
- `scripts/live_quickstart_smoke.py`: runs a real `quickstart --harness claude-code --model <auto>
  --game dummy --yes --json` end-to-end and prints/persists the result — the human-runnable proof,
  and the thing the agent team executes to capture evidence.
- Wire the `live` marker into `.github/workflows/live-integration.yml` (or document the manual
  invocation) so these run where real CLIs/secrets exist, and NEVER in the hermetic lane.

## Agent-team orchestration

A workflow that fans out: (1) a codex-adapter-fixer agent (Unit A, RED→GREEN against real codex),
(2) a live-lift agent (Unit B), (3) a tty-picker agent (Unit C) — in parallel where independent —
then a synthesis/verify agent that runs the whole live suite and captures evidence, then
`/santa-loop` review. Because these mutate the same worktree, the fixer (Unit A, edits
contract.py) lands first; B/C add only new test files.

## Evidence to capture
- Real `codex exec --json` transcript (already captured) committed as a fixture.
- A real claude-vs-bare lift result (`quickstart_result.json` + scorecard).
- A TTY-driven picker transcript.
- The live-suite pass output.

## Delivery
- TDD per unit; the codex model-resolution FIX is the substantive code change. Hermetic suite stays
  green; the new `live` tests skip cleanly without auth. PR off `feat/quickstart-e2e` → santa-loop.
