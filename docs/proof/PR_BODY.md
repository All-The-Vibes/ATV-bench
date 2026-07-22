# Real harness-vs-harness — wire the Phase 1 spine + declared-config fingerprint

Implements the locked `/office-hours` design end-to-end: a **real** harness-vs-harness
benchmark where each coding-agent harness (Claude Code, GitHub Copilot CLI) builds its
own bot **headless**, the two bots compete in a **CodeClash Docker arena**, and the
**harness** — not just the model — is captured as a declared-config fingerprint with an
explicit `unknown[]` ledger. No hand-written bots, no
faked model strings.

## Proof it's real (not mocked)

A live A/A self-play match ran end-to-end through the full spine
(`docs/proof/live_match/`):

- Two `claude-code` harnesses each built their own `main.py` **headless** via the real
  `claude` CLI — a flood-fill + Voronoi-territory + head-on-safety strategy the harness
  authored itself (see `claude-code-A_changes_r1.json`), plus a self-benchmark harness.
- The captured multi-file tree was written back into each Docker container.
- They competed 10 real lightcycles sims: **round 1 → claude-code-A won 6–4** (round 0
  identical-seed control tied 3–3).
- Model tag `claude-opus-4-8` parsed from the real run; record is `verified=false` so it
  publishes **no** ranked number (Phase-1 integration milestone, per the plan).

`atv-bench run --demo` (screenshot: `docs/proof/demo_replay.png`) replays a canned but
REAL recorded match with zero Docker/auth/network.

## What landed (all TDD, 547 unit tests green + gated live E2E)

**Gating spikes first (ENG-2 / ENG-6):**
- CodeClash isn't on PyPI → vendored at a pinned commit, `codeclash_env.py` import shim,
  API-drift smoke test. Confirmed `get_agent` is constructed **host-side** in
  `PvpTournament.__init__`; monkeypatch site = `codeclash.tournaments.pvp.get_agent`.

**Shared contracts (build step 0):**
- Corrected snapshot-diff capture (`git diff <base-tree>` ∪ untracked; ENG-1) with the
  two CRITICAL regressions (commits-its-edit, edits-in-place) + `atv-base` GC pin.
- Copilot **real-model parse** (gap #15 RESOLVED — its `--output-format json` exposes
  `assistant.message.data.model`); `--model auto` never echoes `auto`.
- Schema-v2 match record + identity key `(game_version, prompt_version, harness,
  verified_model, fingerprint_sha256, adapter_version)`; `verified=false` never publishes.

**Lane A — fingerprint moat:** `tools` + `nested_skills` added to the schema;
`probe_claude_code` recurses real plugin layouts (**214 nested skills** captured live);
per-harness tools reader with `{name, source, enabled}`; runtime honesty (real CLI
version/path/sha256 + `unknown_runtime[]`); CRITICAL completeness test per harness +
extended canary leak test.

**Lane B — the spine:** `players.HarnessPlayerCore` (snapshot capture, materialized-tree
authoritative, build-once cache keyed outside instance scope — exactly one CLI build
across N rounds); captured-tree allowlist (symlink/escape/size/secret-pattern reject);
`integration.register()` monkeypatch (idempotent, restores, harness keys → HarnessPlayer,
`dummy`/`mini` fall through).

**Lane C — the CLI:** `atv-bench run --game --a --b --model --rounds` with fail-closed
preflight (missing CLI → exit 3, never fabricates a bot), `--json` envelope, stable exit
codes (0/2/3/4/5/6/7/8/9), `--list-games`/`--list-harnesses`, `--demo` walking skeleton;
`doctor` reuses the shared preflight.

**Showcase:** `all-the-vibes/ATV-Phoenix` vs `microsoft/hve-core`, each row keyed by repo
name carrying its full leak-safe fingerprint chips (`docs/proof/showcase/`).

## Scope (per the locked plan)

Phase 1 is the **integration milestone**: real spine, real fingerprint, real match —
labeled unverified because publishable numbers need the Phase-2 Portkey gateway on an
internal-only network. Battlesnake (game #2) works via the same CodeClash reuse. Codex is
a fingerprint target only (no builder adapter yet).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
