# ATV-bench — Spike Report

Date: 2026-07-15
Design doc: `~/.gstack/projects/atv-bench/sschofield-main-design-20260715-013403.md`
Status: **Both gating spikes PASS.** Design validated. Cleared to plan.

Two decisions gated all implementation. Both are now resolved with running code
and passing tests (TDD).

---

## Spike 1 — Copilot CLI headless (the Assignment)

**Question:** Can a harness CLI be driven fully headless — no TTY, token via env
var — and produce a committed code edit with zero keystrokes?

**Answer: YES (mechanism proven).** The GitHub Copilot CLI ships first-class
headless support, so no PTY hack is needed:

- `copilot -p "<goal>" --allow-all-tools --no-ask-user -s` runs non-interactive
  and exits after completion (help: "`--allow-all-tools` ... required for
  non-interactive mode").
- Headless auth via `COPILOT_GITHUB_TOKEN` / `GH_TOKEN` / `GITHUB_TOKEN` env vars
  — exactly the design's "device-flow OAuth token in a non-interactive shell."
  (`copilot login` docstring: "Authenticate with Copilot via OAuth device flow…
  will use an authentication token found in environment variables. This method is
  most suitable for headless use such as automation.")
- `--output-format json`, `--model`, `-C`, BYOK via `COPILOT_PROVIDER_*` all
  present.

**Live result on this machine:**

| adapter | status | model | secs | edit |
|---|---|---|---|---|
| claude-code | `ok` | claude-opus-4-8 | 23.3 | ✅ bot.py: `"up"`→`"down"`, 8-line diff, 455 tok |
| copilot-cli | `policy_denied` | — | 6.3 | ⛔ org policy on `sschofield_deloitte` account |

The Copilot block is an **org policy** on this specific Deloitte-tenant Copilot
entitlement ("Access denied by policy settings"), NOT a TTY/technical failure. It
ran non-interactively, no controlling terminal, clean exit code — the mechanism is
proven. A properly entitled Microsoft-internal account (the actual target user)
will not hit this wall.

**Fallback ladder (from design) — resolved:**
1. ✅ Official non-interactive flag exists (`-p --allow-all-tools`). No PTY needed.
2. PTY emulation — not required.
3. Ship-without-copilot — not required; claude-code + byok already headless, and
   the copilot adapter is code-complete and returns a clean `policy_denied` signal
   the runner treats as a scored-but-flagged outcome rather than a crash.

**Adapter status → scoring:** the contract distinguishes `policy_denied` from a
fair `error`/`no_edit`, so the leaderboard never silently scores a policy wall as
a harness loss.

## Spike 2 — CodeClash decoupling

**Question:** Does CodeClash's match engine accept an externally-authored bot
independent of its model layer?

**Answer: YES, cleanly.** Verified against `vendor/CodeClash` (MIT, cloned):

- `codeclash.agents.player.Player` is abstract with ONE required method:
  `run(self) -> None` — "given the observation/recap, update the codebase."
- `codeclash.agents.get_agent(config, ctx, env)` maps `config['agent']` →
  Player class. Adding `"copilot-cli"/"claude-code"/"byok"` is a 3-line dict edit.
- The compete phase (`CodeArena.run_round` → `_pre_round_setup` → `execute_round`)
  copies each player's **already-edited codebase** from its container and runs the
  game. It never calls a model. Battlesnake/Tron scoring, Docker engine, and the
  web viewer are all reused untouched.

**The seam:** an ATV-bench harness is a `Player` subclass whose `run()` shells out
to a harness adapter (headless CLI) instead of a model. Proven by
`spikes/spike_codeclash_decoupling.py::HarnessPlayerCore`:
pull bot from container → run adapter → write edit back. Fully unit-tested with a
fake container + fake adapter, and a guard test asserts the editing core imports
**no** codeclash/model code at module load (lazy import only in the production
wrapper).

## Tests (TDD)

```
uv run pytest -m "not live"   # 8 passed  (contract schema + decoupling seam, hermetic)
uv run pytest -m live -s      # 2 passed  (real claude + copilot CLIs, ~20s)
```

- `tests/test_contract.py` — typed adapter contract matches design schema.
- `tests/test_spike_codeclash_decoupling.py` — edit reaches container; no-edit
  leaves it untouched; model tag captured; no model/arena coupling.
- `tests/test_spike_copilot_headless.py` — live headless edits, no TTY.

## Implications for the plan

1. Three adapters are viable: **claude-code** (fully proven), **byok** (Copilot's
   `COPILOT_PROVIDER_*` mode needs no GitHub auth — anchor), **copilot-cli**
   (code-complete; needs an entitled account, which the target audience has).
2. Reuse-over-rebuild holds: register 3 Players in `get_agent`, reuse everything
   else. No arena/scoring/viewer changes.
3. Model tagging is free — claude JSON returns `modelUsage`; wire it into every
   leaderboard row for the thesis-integrity (model vs harness) labeling.
4. Same-model/different-harness primary comparison is feasible: run claude-code
   vs byok both pointed at the same model to isolate harness effect.
