# ATV-bench Community League — Approach A (active v1)

The shipping v1. Supersedes the local-harness plan in `IMPLEMENTATION_PLAN.md`
(retained for context). Re-scoped by a 4-phase dual-voice review (2026-07-15):
both models 6/6 rejected the hosted Approach B on strategy; it had no owner.

## The mechanic

1. A contributor runs a local match with their harness, producing a **bot file** the
   harness edited (e.g. `main.py` for Battlesnake) + a **harness fingerprint**.
2. `atv-bench submit` opens a **PR** to this repo carrying the bot + fingerprint JSON.
   The contributor never reports their own win/loss (forgeable) — only the artifact.
3. A **GitHub Action** runs on merge:
   - **match job (untrusted):** executes the bot in the CodeClash Docker arena against
     the stored roster with fixed seeds. Runs with `permissions: {}`, no `GITHUB_TOKEN`,
     no Pages token, egress blocked, resource caps, non-root read-only container. Writes
     only a schema-validated **result artifact**.
   - **publish job (trusted):** reads the artifact (never executes bot code), recomputes
     ELO from full history (deterministic), builds the static leaderboard, deploys Pages.
4. The **static leaderboard** publishes each row: rank · ELO · fingerprint chips.

## Attribution (eng T13 — no client-side crypto)

Attribution = **the PR author's GitHub identity**. There is no client-side signing key,
no PKI, no `fingerprint.sig`. Under a serverless git+Action model the PR author is already
authenticated by GitHub; a client-generated signature would verify nothing the platform
doesn't already prove and is security theater. Removed from scope.

Fingerprint **honesty** is still trust-based (Premise 4): GitHub identity proves *who*
submitted, not that the reported skills/MCPs/plugins are truthful. Public match logs are
the dispute mechanism. See the "Scope of the claim" section in the README.

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

v1 fingerprint parity target = **claude-code only**. copilot (CLI/VS Code) and codex
probes are fast-follow; their surfaces emit as `unknown[]` until implemented.

### The consent surface is the boundary for arbitrary names

The scanner provably blocks secret-*shaped* values: known token shapes, credential
prefixes (`sk-`, `ghp`, `xox`, `AKIA`…), credentials-in-URL, PEM blocks, credential
keywords, unicode/zero-width tricks, and high-entropy blobs. A red-team fan-out
(5 independent adversaries, two rounds) drove these fixes and now reports the scanner
leak-safe against secret-shaped input.

What no string scanner can do is distinguish a *benign* low-entropy slug from a
*secret* that happens to look like one — if a user literally names a skill
`prod-db-name`, the probe faithfully emits the name the user chose. That is not a
secret leak; it is the user's public skill name. The defense is the **consent
surface**: `atv-bench fingerprint --dry-run` shows the exact "Will publish" list and
the count of scrubbed values before anything is submitted, so the user approves
publication of their own names. Fingerprint honesty remains trust-based (Premise 4);
public match logs are the dispute mechanism.

## ELO (deterministic, forfeit-safe, variance-gated)

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
