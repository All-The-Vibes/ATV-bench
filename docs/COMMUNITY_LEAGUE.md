# ATV-bench Community League — Approach A (active v1)

The shipping v1. Supersedes the local-harness plan in `IMPLEMENTATION_PLAN.md`
(retained for context). Re-scoped by a 4-phase dual-voice review (2026-07-15):
both models 6/6 rejected the hosted Approach B on strategy; it had no owner.

## The mechanic

1. A contributor runs a local match with their harness, producing a **bot file** the
   harness edited (e.g. `main.py` for lightcycles) + a **harness fingerprint**.
2. `atv-bench submit --dry-run` builds the submission record (bot + fingerprint JSON);
   the contributor commits it under `league/submissions/<identity>/` and opens the PR —
   either automatically with `atv-bench submit --live --identity <login>` (fork → clone →
   branch → push → `gh pr create`, first-timer fork bootstrapped) or by hand (manual
   fallback in CONTRIBUTING.md). The contributor never reports their own win/loss
   (forgeable) — only the artifact.
3. A **GitHub Action** runs when a maintainer adds the `run-match` label to the PR
   (the label is the trust boundary gating untrusted bot execution):
   - **match job (untrusted):** executes the bot in the CodeClash Docker arena against
     the stored roster with fixed seeds. Runs with `permissions: {}`, no `GITHUB_TOKEN`,
     no Pages token, egress blocked, resource caps, non-root read-only container. Writes
     only a schema-validated **result artifact**.
   - **publish job (trusted, on `workflow_run`):** reads the artifact (never executes bot
     code), recomputes ELO from full history (deterministic), re-scans every merged
     fingerprint for secret-shaped values (leak-safety on the publish path, not just at
     probe time), and persists the match to the store on the default branch. It holds
     `contents:write` only — no Pages scope.
   - **deploy job (trusted, on `push` to the default branch — `league-deploy.yml`):** the
     store commit triggers a rebuild + GitHub Pages deploy from that exact settled head
     (a `pages` concurrency group makes the newest deploy win, so the board never
     regresses to a stale snapshot). A merged submission PR triggers the same path, so a
     new entrant's row appears on merge.
4. The **static leaderboard** publishes each row: rank · ELO · fingerprint chips.

**Onboarding timing (by design):** the publish job builds from the submissions committed
on the default branch. A brand-new entrant's `submission.json` lands on the default branch
only when their PR **merges**, so a pre-merge labeled `run-match` records the match into
history but the new entrant's row appears once the PR is merged. Matches are never lost
(recompute-from-history is durable); only the row's first appearance waits for merge. A bot
whose match is recorded but whose submission is not yet merged contributes to ELO history
without a visible row until then.

## Attribution (eng T13 — no client-side crypto)

Attribution = **the PR author's GitHub identity**. There is no client-side signing key,
no PKI, no `fingerprint.sig`. Under a serverless git+Action model the PR author is already
authenticated by GitHub; a client-generated signature would verify nothing the platform
doesn't already prove and is security theater. Removed from scope.

Fingerprint **honesty** is still trust-based (Premise 4): GitHub identity proves *who*
submitted, not that the reported skills/MCPs/plugins are truthful. Public match logs are
the dispute mechanism. See the "Scope of the claim" section in the README.

## Match-result trust boundary (what the Action does and does NOT prove)

The untrusted bot runs in the sandboxed match job and its stdout becomes a result
artifact. The trusted publish job **binds** that artifact to a workflow-issued match spec
(`MatchSpec`: submitter = PR author's GitHub login, opponent = the roster anchor,
match_id = the stable `github.run_id`) before anything enters permanent ELO history:

- **Identity is trusted, not bot-asserted.** An `ok` artifact's `player_a`/`player_b`
  must be exactly the two issued participants and its `match_id` the issued one. A bot
  that names a third party, fabricates a match_id (replay/injection), or claims it played
  itself is **rebound to a `CRASH` forfeit against the submitter** — never trusted, never
  dropped (a dropped match skews everyone's ELO). Stored identities are canonicalized
  from the spec, so no bot-chosen string ever lands in an identity field. Enforced on
  every push by `tests/test_match_binding.py` + the `league.yml` tripwire in
  `tests/test_action_isolation.py`.
- **Outcome is now ARENA-ADJUDICATED (trust boundary CLOSED).** The win/loss/draw is no
  longer bot-asserted. The arena image's ENTRYPOINT is a **trusted referee**
  (`python3 -m atv_bench.arena`) that runs a deterministic lightcycles/Tron game inside
  the sandbox, drives the submitted bot as a **move-only subprocess** (one direction per
  turn, per-turn timeout), and **authors** the outcome from real gameplay. A bot that
  prints a fabricated result to stdout is emitting an invalid move and forfeits — it can
  never inject an outcome. The opponent is a fixed baseline **anchor**, now a real
  in-process reference player, still **pinned at 1500 and excluded from ELO updates**
  (`elo.compute_leaderboard(anchors=[...])`, plan #11/#12), so it remains a fixed
  yardstick every entrant is scored against. See `docs/FOLLOW_UPS.md` item 1 (RESOLVED),
  `src/atv_bench/arena/`, and the proof artifacts in `docs/proof/item1-adjudication/`.
  Public match logs remain the fingerprint-honesty dispute mechanism (Premise 4).

## Required repository configuration (the fork-PR governance layer)

GitHub runs a `pull_request`-triggered workflow from the **PR's own copy** of the workflow
file. That is fundamental to the fork-PR model and means a malicious submission PR could
rewrite the `pr-path-guard` gate or the `league.yml` scorer to no-op itself. No workflow can
close this alone (the PR can rewrite that workflow too), so the deployment MUST set:

1. **Branch protection on the default branch** with *Require status checks to pass* →
   `hermetic` and `pr-path-guard` as **required** checks, *Require a pull request before
   merging*, *Require review from Code Owners*, and *Do not allow bypassing the above
   settings* (include administrators).
2. **`.github/CODEOWNERS`** (in-repo) makes every trust-critical path — `.github/**` (the
   workflows/gate/scorer), `league/matches.jsonl` (the durable store), and `src/**` (the
   trusted publish/scoring code) — require an explicit maintainer approval. A PR that edits
   any of them cannot merge without a code owner, which is exactly the manual inspection
   GitHub's own fork-PR security guidance depends on. Community submission PRs (only
   `league/submissions/<author>/{main.py,submission.json}`) match none of these and merge
   through the automated gate alone.
3. **Defense in depth in the trusted publisher.** `league-publish.yml` runs on
   `workflow_run` from the default branch (a PR *cannot* rewrite it) and independently
   re-resolves the triggering PR's author via the GitHub API, failing closed if the match
   artifact's `submitter` does not match. So even a rewritten scorer that forges a
   `match-meta.json` submitter is caught before it reaches permanent ELO history.

Enforced/asserted by `tests/test_action_isolation.py`
(`test_codeowners_protects_trust_critical_paths`,
`test_publish_job_cross_checks_submitter_against_pr_author`) + `.github/CODEOWNERS`.

## Harness fingerprint (the credibility gate)

A per-harness probe reads on-disk config and emits ONE normalized, **leak-safe** schema:

```json
{
  "harness": "claude-code",
  "model": "claude-opus-4-8",
  "gstack": true,
  "skills": ["gstack", "office-hours"],
  "mcps": ["grafana", "github"],
  "plugins": ["compound-engineering"],
  "custom_agents_count": 7,
  "unknown": [{ "field": "cloud_settings", "reason": "not_readable" }]
}
```

Non-negotiable safety properties (enforced by the canary leak-test):

- **Allowlist-by-construction:** the emitter builds each field from a fixed schema. It
  never copies a parsed config and deletes secrets (denylist can't guarantee leak-free).
  A config field the schema doesn't name is ignored, not passed through.
- **Per-value secret scan:** every value that would enter the manifest is rejected if it
  matches `sk-`, `ghp_`, `xox`, `AKIA`, a DSN, a URL-with-creds, a PEM block, or scores as
  high-entropy. A failing value becomes `unknown[{field, reason:"name_failed_safety_scan"}]`.
- **Names only, never contents:** the probe reads directory basenames/counts. It never
  opens a `SKILL.md` or agent file body.
- **Error paths never crash or silently drop:** permission-denied / malformed JSON /
  0-byte / symlink-outside-`~/.claude` → `unknown[{field, reason}]` with a reason enum;
  never `except: pass`, never raise.

The CLI is **harness-agnostic**: `atv-bench harnesses` lists what's fingerprintable and
auto-detects the local harness; `atv-bench fingerprint [--harness <key>]` probes it. v1
ships **live fingerprint readers for `claude-code` (`~/.claude`), `copilot-cli`
(`~/.copilot`), and `codex` (`~/.codex`)** — each with an allowlist-emit reader + canary
leak-test. The CLI still fails closed (an actionable message, never an empty fingerprint)
for any unknown or not-yet-live harness. Adding a harness reader flips its status in
`src/atv_bench/harnesses.py` and nothing else in the CLI changes.

### The consent surface is the boundary for arbitrary names

The scanner provably blocks secret-*shaped* values: known token shapes, credential
prefixes (`sk-`, `ghp`, `xox`, `AKIA`…), credentials-in-URL, PEM blocks, credential
keywords (`password`, `secret`, `pass`, `pwd`…), common weak secrets/defaults
(`hunter2`, `admin`, `root`… and their suffixed variants), long all-digit strings,
unicode/zero-width tricks, and high-entropy blobs. Independent red-team and dual-review
rounds drove these fixes and now report the scanner leak-safe against secret-shaped
input.

What no string scanner can do is distinguish a *benign* low-entropy slug from a
*secret* that happens to look like one — if a user literally names a skill
`prod-db-name`, the probe faithfully emits the name the user chose. That is not a
secret leak; it is the user's public skill name. The defense is the **consent
surface**: `atv-bench fingerprint --dry-run` shows the exact "Will publish" list and
the count of scrubbed values before anything is submitted, so the user approves
publication of their own names. Fingerprint honesty remains trust-based (Premise 4);
public match logs are the dispute mechanism.

### Provenance (UC1 — binding the fingerprint to the bot it was captured with)

Fingerprint readers are table stakes; on their own they prove nothing about whether the
*published* manifest is the harness/config that actually built the *submitted* bot. Two
attacks the readers alone don't stop: fingerprint a fat config then run a lean one, or
present a `claude-code` fingerprint for a `codex`-built bot.

The provenance binding closes the post-capture gap. At build time `capture_provenance`
binds the facets — `{version, harness, bot_sha256, fingerprint_sha256, captured_at, signed}`
— into a token that carries two layers: `signature`, an unkeyed salted-SHA-256 digest over
the whole payload that ANY verifier (even the keyless Phase-1 board) checks so a naive
post-capture edit to *any* facet — incl. `captured_at` — fails closed; and, when
`ATV_PROVENANCE_KEY` is set, `hmac`, the anti-forgery layer that grants the signed tier. The
token ships inside `submission.json` under `"provenance"`. `verify_submission_provenance`
(and the trusted board build in `LeagueStore`) recompute each facet from the record's *own*
re-hashed bot bytes + manifest and re-derive both layers; any post-capture edit to the
manifest, a swapped bot, or a swapped harness fails closed with a named reason.

Trust level is explicit and honest:

- **Unkeyed (default): tamper-evident, self-attested.** The `signature` is a salted SHA-256
  digest over every facet. It detects hand-edits and swaps, but runs entirely on the
  contributor's machine, so a determined attacker who recomputes the whole token can defeat
  it. These rows are labelled **self-attested**.
- **Keyed (`ATV_PROVENANCE_KEY` set): HMAC-signed.** Adds an `hmac` layer — anti-forgery on
  the token itself. A row is only truly **verified** once a trusted sandbox re-fingerprints
  the harness at match time and re-signs with a server-held key — deferred to Phase 2 (the
  containerized runner). A keyed token still publishes on the keyless Phase-1 board (as
  self-attested, its unkeyed `signature` checked); a key-holding verifier upgrades it to
  **signed**. The verify RESULT (not the token's own `signed` bit) drives the leaderboard's
  verified/self-attested labelling.

This is deliberately client tamper-evidence, not anti-forgery: it makes lying *evident*
(edits break verification, logs remain the dispute mechanism) without over-claiming a
guarantee the client trust boundary can't provide.

## ELO (deterministic, forfeit-safe, variance-gated)

- **Row identity is the GitHub login, by design.** This is a per-contributor / per-harness
  league: a row's ELO is the recompute-from-full-history of every match that login has
  played, so an entrant *improves their bot over time* and their rating reflects that arc —
  it is deliberately NOT reset per bot edit. What IS bound to the exact bytes: (a) each
  scored match is spec-bound to the bot_sha256 the sandbox actually ran (a forged/ swapped
  bot for a given match is rejected), and (b) the row's *published* bot_sha256 is
  re-derived at load from the committed `main.py`, so the displayed hash always matches the
  current on-disk bot, never a self-attested claim.
- **Recompute from full match history** on every publish (not incremental). Same history →
  byte-identical ELO JSON, order-independent — no flapping board on CI re-runs.
- **Zero-opponent provisional:** the first-ever submitter gets `elo=1500, rated=false`,
  UI "waiting for opponent". No NaN, no crash.
- **Forfeit = loss + reason enum** (`TIMEOUT|INVALID_DIFF|NO_OP|MODEL_UNREACHABLE|AUTH_FAILED|CRASH`),
  never dropped (a dropped forfeit skews ELO — a real v1 bug).
- **A/A variance gate with numeric teeth:** identical bots over seeded matches must produce
  no publishable ranked delta. Below a minimum match/seed count or above a maximum CI width,
  the public number is suppressed ("insufficient signal") rather than shown as real.

## Leaderboard JSON contract

Locked, versioned schema the Action writes and the viewer validates on load:
`schema_version, rank, elo, rated, match_count, ci{lo,hi}, identity, harness_name,
fingerprint_summary, details{skills[], mcps[], plugins[], unknown[{field,reason}]},
bot_sha256, fingerprint_probe_version, pr_url, logs_url, updated_at (ISO-8601 UTC)`.

## Approach B gate (deferred, CEO T2)

Approach B (hosted submit API + live websocket board) ships only when ALL hold:

1. **Named owner** — a specific person accountable for auth, DB, ops, and on-call.
2. **Data-retention policy** — written policy for stored bots + fingerprints (how long,
   who can delete, privacy of harness-authored code).
3. **Adoption threshold** — **> 25 voluntary submitters** on the Approach-A board.

Until all three hold, Approach A is the entire league.
