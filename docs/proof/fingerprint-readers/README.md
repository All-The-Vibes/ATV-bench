# Proof: fingerprint readers (codex live + claude-code real-layout parity)

Plan: `~/.gstack/projects/All-The-Vibes-ATV-bench/sschofield-main-design-20260716-220408.md`
(ENG CLEARED). Test plan: 24 gaps, 11 critical — all unit-testable, no UI/E2E scope.

## Acceptance criteria (all met, verified on the real machine)

| Success criterion (design §Success Criteria) | Result |
|---|---|
| `fingerprint --harness codex` → correct leak-free manifest (model `gpt-5.5`, real skills, `plugins=[]`) | ✓ `codex-manifest.json` — model `gpt-5.5`, 16 skills, `plugins:[]`, leak-clean |
| codex never reads provider tables / http_headers (`sk-godmode`) | ✓ grep of manifest: no `sk-godmode`/`portkey`/provider material |
| `fingerprint --harness claude-code` → no `cache`/`data`/`marketplaces` as plugins; plugins from `enabledPlugins`; nested skills counted | ✓ `claude-manifest.json` — 13 real plugins, 92 skills, no infra dirs |
| claude mcps non-empty (NEW `~/.claude.json` source, not `.mcp.json`) | ✓ `mcps: [backlog, context7, microsoft-docs]` (was `[]` before) |
| both readers pass `validate-harness` | ✓ `validate-codex.txt`, `validate-claude.txt` |
| `codex` shows **live** in `atv-bench harnesses` | ✓ `harnesses.txt` — all 3 live |
| zero CLI/validate/submit wiring change beyond the plan's named CLI fixes | ✓ (T5 detect-guard/consent/msg were in scope) |

## Tests

495 hermetic tests pass (`uv run pytest -m "not live and not integration"`), +71 over the
424 baseline. New: `read_toml` unit suite (6), non-UTF8 regression (read_json + read_toml),
codex canary + edge tests (7), claude real-layout canaries (installPath escape ×3, symlink
escape, infra-not-plugins, disabled-plugin exclusion, real mcp source, manifest guard ×4,
gstack-as-nested-skill, cross-plugin dedup), CLI detect-guard/model-consent/codex-msg (4),
validate-harness copy (2), harness-agnostic codex-live updates. Santa-loop dual-review
hardening (+28): fail-closed on empty/malformed/unreadable primary config, non-dict primary
config flagged malformed (claude + copilot), codex malformed config flags mcps unknown,
installed_plugins.json bad-internal-shape markers, multi-harness ambiguity consistency
(detect-guard vs `harnesses` text + JSON), and REASON_ABSENT vs REASON_NOT_READABLE split so
an existing-but-unreadable config fails closed while a genuinely-absent optional config does
not (config.toml / settings.json / ~/.claude.json / installed_plugins.json), and `--home
<root>` without `--harness` resolves the harness from the root basename (`.codex` → codex)
instead of $HOME auto-detect (which mis-probed a codex root as claude-code), and a
wrong-TYPE model value (`model = 123`) is flagged malformed + fails closed (distinct from an
unsafe-STRING model, which stays a scrub-not-fail consent boundary), and a dangling config
symlink (present as a link, target missing) fails closed as not_readable instead of being
skipped as absent.

## Adversarial leak-safety verification (Workflow: 3 independent skeptics)

- **codex-leak lens**: "No real secret-leak path found." Provider/header material is
  structurally unreachable — probe_codex keys into only `config['model']` and
  `[mcp_servers.*]` keys. Every emitted name passes `is_safe_name`; reads confined.
- **claude-leak lens**: "No path-traversal escape and no value-field secret leak." All the
  classic attacks (installPath=/etc, `../../.ssh`, escaping symlinks, secrets in mcpServers
  values, credential-prefixed keys) are blocked by `_within_root` + `is_safe_name`.
- **correctness lens**: PASS on all 6 dimensions (mcp source, disabled-plugin exclusion,
  dedup, codex plugins/prompts, no crash paths, manifest-shape degradation).

3 findings, all **low-severity documented consent-surface boundaries** (a user literally
naming a skill/MCP after their own short secret) — explicitly out of scope per the design's
"consent surface is the boundary for arbitrary names" (Premise 4). No new leak, no
confinement break.

## Real-machine note (codex skills symlink confinement)

The reference `~/.codex/skills/` has 32 entries that are symlinks pointing OUTSIDE `~/.codex`
(into repo `.gstack/.agents/skills/`). Confinement correctly refuses them as `symlink_escape`
in `unknown[]` rather than following a symlink out of root — the design's non-negotiable
safety property, working as intended. The 16 in-root real dirs are emitted.
