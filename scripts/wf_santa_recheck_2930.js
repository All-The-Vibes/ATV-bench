export const meta = {
  name: 'santa-recheck-2930-r4',
  description: 'Re-run Claude + codex santa-loop against the corrected PR #29/#30 review report',
  phases: [{ title: 'Santa' }],
}

const REPO = '/home/sschofield/repos/atv-bench'
const PROOF = `${REPO}/docs/proof/pr-review-2930`
const reportPath = `${PROOF}/REVIEW_REPORT.md`
const SHOT = `${REPO}/scripts/shot_terminal.py`

const VERDICT = {
  type: 'object',
  required: ['verdict', 'blocking', 'notes'],
  properties: {
    verdict: { type: 'string', enum: ['NICE', 'NAUGHTY'] },
    blocking: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
}

const prior = `
ROUND 3 — both reviewers returned NAUGHTY and INDEPENDENTLY converged on the SAME single blocker:

  §4's PR #30 check table spliced two different CI runs while claiming to reflect only the
  post-2772d03 run. When the evidence was recaptured, ONLY the hermetic row was updated
  (1m49s -> 1m42s); import-smoke was left at 1m3s and pr-path-guard at 25s, which are the
  PRE-remediation 521559f run's durations. Both contradicted ci-evidence-2930.png and did not
  reproduce from the four commands §4 names as its evidence.

  -> FIXED: import-smoke 1m3s -> 55s, pr-path-guard 25s -> 20s, matching live 'gh pr checks 30'
     and the regenerated screenshot exactly.

  -> ALSO SWEPT THE CLASS, not just the instance (this is the third round in a row where a fix
     was applied to one row/section and its siblings were left stale):
       * #29's table re-checked against live 'gh pr checks 29' — hermetic 1m42s, import-smoke 50s,
         pr-path-guard 20s, windows-console-encoding 1m18s — all already correct, no change needed.
       * Every other duration string in the report grepped and checked (only line 248's prose
         '1m42s' and the #29 windows row remain; both correct).
       * Every numeric claim re-verified on the branch: 29 png / 34 files in pr-review-2126,
         39 files / +1562 / -0, 45 commits on backup/stale-main-pre-reset, report 139 lines on
         origin/docs/pr-review-2324-report.

Round-2 fixes (Seven-vs-Six, §3 pre/post-remediation labelling, §6 resolved/outstanding split)
were confirmed REAL by both reviewers in round 3 and are unchanged.

VERIFY THIS IS REAL. Re-run 'gh pr checks 29' and 'gh pr checks 30' yourself and compare every row
of BOTH §4 tables against the live output and against ci-evidence-2930.png. Check for any remaining
value anywhere in the report that disagrees with its cited evidence.
`

const santaPrompt = `Adversarially review the PR review report at ${reportPath} (repo ${REPO}).
You are the santa-loop reviewer. Return NICE only if EVERY criterion passes; otherwise NAUGHTY.
${prior}
RUBRIC (objective pass conditions):
1. Accuracy — every claim verdict backed by cited, reproducible evidence.
2. Screenshots — every VERIFIED claim cites a screenshot path that EXISTS. Verify with ls.
3. Honesty — no overclaim; inaccurate PR bodies called out plainly; "CI green" carries scope caveats;
   unverifiable items labelled UNVERIFIABLE.
4. Security — exact counts (Critical/High/Medium/Low), findings not minimised.
5. Merge order — explicitly justified, accounts for file overlap and rebase implications.
6. Completeness — body accuracy, security, CI, conflicts all covered.
7. No fabrication — spot-check at least 2 cited screenshots exist and re-run at least 1 quoted command.
8. Internal consistency — EVERY stated value (counts, durations, statuses) must match both the table it\nsummarises and the evidence it cites. This has failed three rounds running, each time because a fix was\napplied to one row and its siblings left stale. Check rows adjacent to any corrected value.

Be genuinely adversarial. List every blocking issue concretely. If the prior round's fixes are real and
no NEW defect exists, return NICE — do not invent issues to appear rigorous.`

phase('Santa')

const [claudeVerdict, codexVerdict] = await parallel([
  () => agent(santaPrompt, { label: 'santa4:claude', phase: 'Santa', schema: VERDICT, effort: 'high' }),
  () => agent(
    `Run an INDEPENDENT external review using the codex CLI and report its verdict faithfully.

IMPORTANT: last round codex's first invocation hung reading stdin. Always redirect stdin from /dev/null.
Write the prompt to a file first, then pass it as a single argument:

  cat > /tmp/santa4-prompt.txt <<'PROMPT'
${santaPrompt}

Respond with a final line exactly: VERDICT: NICE  or  VERDICT: NAUGHTY
List each blocking issue on its own line prefixed with "BLOCKING: ".
PROMPT

  cd ${REPO} && timeout 900 codex exec --skip-git-repo-check "$(cat /tmp/santa4-prompt.txt)" < /dev/null 2>&1 | tee /tmp/codex-santa4-2930.txt

Then:
- Screenshot codex's real output:
  uv run --no-project --with pillow python ${SHOT} "codex santa-loop round 4 verdict" /tmp/codex-santa4-2930.txt ${PROOF}/santa-codex-r4.png
- Report codex's VERDICT line VERBATIM. Do NOT substitute your own judgement.
- If codex produces no verdict, return NAUGHTY with a blocking item saying codex could not verify.`,
    { label: 'santa4:codex', phase: 'Santa', schema: VERDICT, effort: 'high' },
  ),
])

const both = [claudeVerdict, codexVerdict].filter(Boolean)
const converged = both.length === 2 && both.every(v => v.verdict === 'NICE')
log(`round 4: claude=${claudeVerdict?.verdict} codex=${codexVerdict?.verdict} → ${converged ? 'CONVERGED' : 'STILL BLOCKED'}`)

return { claude: claudeVerdict, codex: codexVerdict, converged, blocking: both.flatMap(v => v.blocking || []) }
