# PR fleet review — 6 open PRs, 2026-08-03

Adversarial review of every open PR against four criteria: PR-body alignment, security/code
quality, CI status, and merge conflicts. Every finding was routed through an independent
refuter before it counted, and every surviving HIGH was re-tested by hand before it was
reported to the PR.

**9 of 18 findings did not survive verification.** That ratio is the point of the exercise:
the refutation pass and the hand re-test are what separate a real defect from a plausible
one.

## Result

| PR | Body accurate | Confirmed defects | Disposition |
|---|---|---|---|
| #29 cp1252 encoding | ✗ 2 overclaims | CRLF digest divergence (HIGH), layer-1 untested (MED) | merge before #33 |
| #30 rescue artifacts | ✓ | none blocking (2 LOW) | ready |
| #31 review report | ✗ | U+2060 in a report claiming no zero-width chars (MED) | fixed by **#37** |
| #33 CRLF integrity | ✗ count only | merge-order hazard with #29 (MED) | merge after #29 |
| #34 audit cuts | ✗ "behavior-neutral" false | fractional-second truncation (HIGH) | fixed by **#38** |
| #35 path-guard scope | ✗ "no loosening" false | 3 league-guard bypass vectors (HIGH ×2) | fixed by **#36** |

All six PRs had green CI and no textual conflict with `main` at review time. `BLOCKED` on
five of them was `REVIEW_REQUIRED` from CODEOWNERS, not merge trouble.

## The finding a conflict check cannot see

`#29` and `#33` rewrite the same line of `submit.py` with incompatible semantics. Neither
conflicts with `main`, so a per-branch conflict check clears both.

| branch | copy site | CRLF bot survives |
|---|---|---|
| `main` | `write_text(read_text())` | no |
| #29 | `write_text(read_text(encoding="utf-8"), encoding="utf-8")` | **no — CRLF still lost** |
| #33 | `write_bytes(read_bytes())` | yes |

Proved from both directions:

- `git merge` #33 into #29 → **exit=1, both lines present**
- On #29's branch, `bot_sha256` declared `15efcb67…` vs committed `b2c9a252…` — issue #32
  still live

**#29 must merge before #33, resolving to `write_bytes()`.** The reverse order silently
reintroduces the issue #33 exists to close.

## Recommended merge order

```
#36 → #35 → #37 → #31 → #30 → #29 → #33 → #38 → #34
```

Each fix PR targets its parent branch, so merging the fix first makes the parent mergeable
with no force-push. Disjoint doc PRs (#31, #30) carry no ordering constraint. The
`submit.py` chain (#29 → #33) is dependency-ordered per above.

## Fixes shipped (TDD, RED first)

### #36 — league-guard bypass (2 HIGH, from #35)

#35 scoped the rename/delete gate to `league/**` using a raw string prefix against
unnormalized `git diff --name-status` output. Three spellings then slipped a gate `main`
rejects:

| name-status line | main | #35 |
|---|---|---|
| `D league/matches.jsonl` | blocked | blocked |
| `D ./league/matches.jsonl` | blocked | **BYPASS** |
| `D "league/submissions/caf\303\251/main.py"` | blocked | **BYPASS** |
| `D League/matches.jsonl` | blocked | **BYPASS** |

Reachable from CI: `ci.yml:79` pipes `git diff --name-status` straight into the guard, and
git C-quotes non-ASCII paths **by default** (`core.quotepath`). An entrant whose login
carries an accent is sufficient; the payload is deleting durable league state with the
guard reporting `ok`.

Fix: normalize once at the boundary — strip C-quoting, strip `./`, casefold. Casefolding
can only make a fail-closed guard reject *more*.

RED `3 failed, 17 passed` → GREEN `20 passed`.

### #37 — invisible codepoint (MED, from #31)

#31's body claims the report was scanned clean of zero-width characters. `REVIEW_REPORT.md`
line 217 contains a **U+2060 WORD JOINER** — on the very line reporting its own
zero-width-character finding.

It survived because the remediation the report *prescribes* is a grep for
`U+200B/C/D/FEFF`, a class that excludes U+2060. The report specified a scanner that could
not catch the character inside it.

Fix: strip the character, widen the class, and **enforce it** —
`tests/test_proof_docs_invisible_codepoints.py` scans all `docs/proof/**` and
`docs/plans/**`. A prescribed grep rots; a CI test does not.
`test_scanner_actually_detects_word_joiner` guards the guard.

RED `1 failed, 9 passed` → GREEN `10 passed`.

### #38 — timestamp truncation (HIGH, from #34)

The audit removed `if s.endswith("Z") and "+" not in s: return s` as redundant. It was
load-bearing: the `strftime` below emits whole seconds only.

| input | main | #34 |
|---|---|---|
| `2026-07-15T15:36:06.123456Z` | unchanged | `…06Z` truncated |
| `2026-07-15T15:36:06.500Z` | unchanged | `…06Z` truncated |

Reachable: `leaderboard.py:75`'s schema *explicitly* admits fractional seconds
(`(\.\d+)?Z`) and `publish build --updated-at` passes caller input through. The truncated
value is one the schema declares valid — contradicting the PR's "behavior-neutral"
headline.

1123 tests missed it because every fixture uses whole seconds and the one normalization
test asserts `.endswith("Z")`, which a truncated value still satisfies.

RED `2 failed, 4 passed` → GREEN `42 passed`.

## Findings that did not survive

Reported here because a review that only records confirmations is not auditable.

| Claim | Verdict | Why |
|---|---|---|
| #33 `.gitattributes` doesn't protect Windows contributors (HIGH) | **refuted** | Documented flow is fork→clone; a clone inherits `.gitattributes`. Verified: CRLF preserved with `core.autocrlf=true`. |
| #29 `--help` unhardened (HIGH) | **downgraded to MED** | Exits 0 under cp1252 (the PR's target) and fails identically on `main` — pre-existing, out of scope. Real only under `LC_ALL=C`. |
| #33 "tautological" source-grep tests (HIGH) | **narrowed to MED** | Does catch a real mutation. But its `not ln.endswith("(")` filter skips continuation lines — including the write carrying the bot bytes. |
| #31 `r.text` schema violation (LOW) | refuted | Schema sets no `additionalProperties: false`; falls through to `evidence`. |
| #31 line-number anchoring (LOW) | refuted | Documentation-convention opinion; `git show 2772d03:…` resolves regardless of merge order. |
| #30 self-approval in archived script (LOW) | refuted | Prompt text in a one-shot archival record, hardcoded to closed PRs; unreachable. |
| #35 docstring "outright" (LOW) | refuted | Reads correctly inside the docstring's own stated scope. |

## Environmental note

`tests/test_containment.py` shows 5 failures locally on **every** branch including clean
`origin/main` (no user namespaces available). Pre-existing and environmental; these pass on
the privileged CI runner. Not attributable to any PR under review.

## Merge gate not crossed

The only push-capable account (`stephschofield`) authored all six PRs, and CODEOWNERS
requires `@All-The-Vibes/league-maintainers` review on `/src/` and `/.github/`. Approving
with that account would be self-approval — the same automated approve-then-merge bypass
that #31's report flags as HIGH and #30's remediation closes.

Everything up to that gate is complete: fixes pushed, CI green, evidence posted. **Merging
requires a second maintainer.**

Evidence: `docs/proof/pr-review-fleet-2026-08/`
