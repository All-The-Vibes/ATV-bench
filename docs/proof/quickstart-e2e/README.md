# Quickstart live e2e — proof the real CLIs are driven end-to-end

This directory holds the evidence + the runnable proofs that `atv-bench quickstart` works against
the **real** coding-agent CLIs (not mocks). These tests are gated `@pytest.mark.live` (and, for the
Docker match, `@pytest.mark.e2e`) — deselected from the hermetic lane; they need real CLI auth,
network, and Docker, and **skip cleanly** when those are absent so the suite stays portable.

## Run the live suite

```bash
# all live e2e (needs claude/copilot/codex auth + docker + vendored CodeClash)
uv run pytest -m live -s

# just the codex real-CLI adapter test
uv run pytest tests/test_codex_adapter_live.py -m live -s

# the real claude-vs-bare Docker lift (slow: real API + arena builds)
uv run pytest tests/test_quickstart_live_lift.py -m live -s

# the interactive picker under a real pty
uv run pytest tests/test_interactive_tty.py -m live -s

# a human-runnable full smoke that prints the scored result + scorecard link
uv run python scripts/live_quickstart_smoke.py --harness claude-code --model sonnet \
    --game dummy --game lightcycles --repeats 3
```

## What each gap-closing test proves

| Gap | Test | Proves |
|---|---|---|
| codex adapter never driven against the real CLI (canned payload only) | `test_codex_adapter_live.py` | real `codex exec --json` edits a repo AND the adapter reports a **real model id** (not `unknown`) |
| claude-vs-bare lift never run for real | `test_quickstart_live_lift.py` | real Docker arena + real `claude` CLI + bare control → finite lift, per-game scores, rendered scorecard |
| picker only ever tested with questionary stubbed | `test_interactive_tty.py` | the actual arrow-key TUI under a real pty: arrow-down+Enter selects the 2nd model |

## Bugs the live e2e caught (that hermetic/stub testing missed)

Live testing earned its keep — it surfaced **two real production bugs** invisible to the
canned/stub tests:

1. **codex model always `unknown`.** The real `codex exec --json` stream (codex-cli 0.130.0)
   carries NO model field — see `codex-exec-json-transcript.txt`. The canned unit test fed a
   fictional `session.created{model}` event, so the adapter looked correct but returned `unknown`
   in production. Fixed: `_resolve_codex_model` resolves from the explicit `-m` (authoritative —
   codex hard-errors on a bad model) else `~/.codex/config.toml`.

2. **`bare:claude-code` broke git branch creation.** CodeClash names a per-player git branch
   after the player name, and the colon in `bare:claude-code` is an illegal git refname
   (`fatal: '...bare:claude-code' is not a valid branch name`) — so EVERY bare-control match
   crashed before scoring. Fixed: `_branch_safe_name` sanitizes the player name (keeping the
   `agent` routing key intact); `summarize_tournament` maps the sanitized winner back to the
   harness key.

## Fixtures / evidence

- `codex-exec-json-transcript.txt` — a real `codex exec --json` transcript (the model-less shape).
- `live-lift-result.json` — a real claude-vs-bare `quickstart_result.json` (added when the live
  lift run completes).
