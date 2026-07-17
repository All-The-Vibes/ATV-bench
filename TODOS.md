# TODOS

## Fingerprint provenance (filed by /autoplan 2026-07-16, UC1)
- [x] **Bind published fingerprint → the harness that built the bot.** SHIPPED as client
  tamper-evidence: `capture_provenance`/`verify_provenance`
  (`src/atv_bench/fingerprint/provenance.py`) bind
  `{harness, bot_sha256, fingerprint_sha256, captured_at}` into a token embedded in
  `submission.json`; `verify_submission_provenance` (submit + merge time) detects the three
  filed attacks (post-edit manifest, bot-swap, harness-swap). Unkeyed = self-attested,
  HMAC via `ATV_PROVENANCE_KEY`. See `docs/COMMUNITY_LEAGUE.md#provenance`.
  - [ ] **Follow-up (Phase 2): server-side attestation.** The client binding cannot stop a
    contributor who lies at capture time (the key lives on their machine). Upgrade a row
    from `self_attested` → `verified` by having the sandboxed match runner re-fingerprint
    the harness at run time and re-sign with a server-held `ATV_PROVENANCE_KEY`.
    `verify_submission_provenance(record, key=...)` is the seam the runner calls.
