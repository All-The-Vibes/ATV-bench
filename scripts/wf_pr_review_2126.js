export const meta = {
  name: 'pr-review-2126',
  description: 'End-to-end review of PRs #21 and #22: verify body claims via screenshotted E2E tests, atv-security scan, CI/conflict checks, merge order, then codex-verified santa-loop',
  phases: [
    { title: 'Recon' },
    { title: 'E2E' },
    { title: 'Security' },
    { title: 'Synthesize' },
    { title: 'SantaReview' },
  ],
}

const REPO = '/home/sschofield/repos/atv-bench'
const PROOF = `${REPO}/docs/proof/pr-review-2126`
const SEC_SKILL = '/home/sschofield/repos/ATV-starterkit/plugins/atv-skill-atv-security/skills/atv-security/SKILL.md'

const PRS = [
  {
    num: 21,
    wt: `${REPO}/.claude/worktrees/pr-21-review`,
    title: 'docs: sync README/CONTRIBUTING/arenas with merged PRs #19 + #20',
    kind: 'docs',
    claims: [
      '`atv-bench games` → 20 live / 22 total',
      '`atv-bench harnesses` → 3 live readers (claude-code, copilot-cli, codex)',
      'README test badge shows 983 (real hermetic pass count)',
      'docs/arenas.md references docs/proof/wave-c/matrix.json (not _e2e/FINAL_MATRIX.json), Wave-C tally 20 PASS / 2 upstream-blocked',
      'plan-schedule deterministic under --seed (identical seed => identical plan)',
      'rate --enforce-gates refuses a thin corpus (fails closed)',
      'lift refuses a phantom-precision single-cluster CI (fails closed)',
      'All 5 referenced proof scripts exist (capture_live_match, capture_isolation_proof, e2e_arena_matrix, rerun_failed_arenas, consolidate_wave_c_proof)',
      'Doc-only change (no code, no uv.lock)',
    ],
  },
  {
    num: 22,
    wt: `${REPO}/.claude/worktrees/pr-22-review`,
    title: 'feat: atv-bench quickstart — one-command harness evaluation UX',
    kind: 'feature',
    claims: [
      '`atv-bench quickstart` command exists and shows help/UX',
      'quickstart infers harness and shows fingerprint',
      'quickstart offers model selection with non-interactive/CI fallback (--model --yes --json)',
      'Default is fast 3-game taste; --all runs all 20 live games',
      'Scoring: per-game + overall clustered-CI lift, fail-closed on thin games (insufficient N)',
      'G5/G6 credibility gates decide credible vs provisional',
      'CodexCliAdapter makes codex a runnable harness (bare:codex works)',
      'models.py surfaces configured model as picker default',
      'Full hermetic suite: 1020 passed, 0 failures',
      'Scorecard rendered at docs/proof/quickstart/scorecard-example.png; self-contained scorecard.html',
      'Adds questionary as runtime dependency',
    ],
  },
]

const RECON_SCHEMA = {
  type: 'object',
  required: ['pr', 'mergeable', 'conflicts', 'ci_all_green', 'ci_summary', 'diff_summary', 'touches_code', 'notes'],
  properties: {
    pr: { type: 'number' },
    mergeable: { type: 'boolean' },
    conflicts: { type: 'boolean' },
    ci_all_green: { type: 'boolean' },
    ci_summary: { type: 'string' },
    diff_summary: { type: 'string' },
    touches_code: { type: 'boolean' },
    files_changed: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
}

const E2E_SCHEMA = {
  type: 'object',
  required: ['pr', 'claims', 'screenshots', 'overall_body_accurate', 'summary'],
  properties: {
    pr: { type: 'number' },
    claims: {
      type: 'array',
      items: {
        type: 'object',
        required: ['claim', 'verdict', 'evidence'],
        properties: {
          claim: { type: 'string' },
          verdict: { type: 'string', enum: ['VERIFIED', 'FAILED', 'PARTIAL', 'UNTESTABLE'] },
          evidence: { type: 'string' },
          screenshot: { type: 'string' },
        },
      },
    },
    screenshots: { type: 'array', items: { type: 'string' } },
    overall_body_accurate: { type: 'boolean' },
    summary: { type: 'string' },
  },
}

const SEC_SCHEMA = {
  type: 'object',
  required: ['pr', 'grade', 'critical', 'high', 'findings', 'summary'],
  properties: {
    pr: { type: 'number' },
    grade: { type: 'string' },
    critical: { type: 'number' },
    high: { type: 'number' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['rule', 'severity', 'file', 'detail'],
        properties: {
          rule: { type: 'string' },
          severity: { type: 'string' },
          file: { type: 'string' },
          detail: { type: 'string' },
        },
      },
    },
    summary: { type: 'string' },
  },
}

const VERDICT_SCHEMA = {
  type: 'object',
  required: ['verdict', 'checks', 'critical_issues', 'suggestions'],
  properties: {
    verdict: { type: 'string', enum: ['PASS', 'FAIL'] },
    checks: {
      type: 'array',
      items: {
        type: 'object',
        required: ['criterion', 'result', 'detail'],
        properties: {
          criterion: { type: 'string' },
          result: { type: 'string', enum: ['PASS', 'FAIL'] },
          detail: { type: 'string' },
        },
      },
    },
    critical_issues: { type: 'array', items: { type: 'string' } },
    suggestions: { type: 'array', items: { type: 'string' } },
  },
}

function reconPrompt(pr) {
  return `You are reviewing GitHub PR #${pr.num} ("${pr.title}") in the All-The-Vibes/ATV-bench repo.
An isolated git worktree of the PR head is checked out at: ${pr.wt}

Do ALL work with Bash. Gather objective recon (do NOT test functionality yet):

1. Mergeability + conflicts (run from ${REPO}, read-only):
   gh api repos/All-The-Vibes/ATV-bench/pulls/${pr.num} --jq '{mergeable,mergeable_state,changed_files,additions,deletions}'
   A "dirty" mergeable_state or mergeable:false means conflicts. "blocked" means branch protection (NOT a conflict).
2. CI status: gh pr checks ${pr.num}  (all non-skipped checks must be "pass"; "skipping" is fine).
3. Diff scope: git -C ${pr.wt} diff --stat main...HEAD  and  git -C ${pr.wt} diff --name-only main...HEAD
4. Whether it touches executable code vs docs only (look at file extensions/paths under src/).

Return the structured JSON. files_changed = full list from name-only. touches_code = true if any src/**/*.py or scripts/*.py or pyproject/uv.lock changed. Be factual.`
}

function e2ePrompt(pr) {
  const shot = `python3 ${REPO}/scripts/shot_terminal.py`
  return `You are an end-to-end verification engineer for GitHub PR #${pr.num} ("${pr.title}") in All-The-Vibes/ATV-bench.
The PR head is checked out in an ISOLATED worktree at: ${pr.wt}
ALL commands must run inside that worktree: use \`cd ${pr.wt} && uv run atv-bench ...\` (the CLI is installed via uv; a .venv exists).

YOUR JOB: test EVERY claim from the PR body against the LIVE CLI/code, and SCREENSHOT each test as verifiable proof.

Screenshot helper (renders captured terminal text to a PNG):
  1. Capture real output:  cd ${pr.wt} && uv run atv-bench <cmd> > /tmp/pr${pr.num}_<slug>.txt 2>&1
  2. Render screenshot:    ${shot} "PR#${pr.num}: <label>" /tmp/pr${pr.num}_<slug>.txt ${PROOF}/pr${pr.num}-<slug>.png
  Every executed test MUST produce a screenshot PNG under ${PROOF}/ named pr${pr.num}-<slug>.png.
  For claims about file contents (badges, references, script existence), capture the grep/ls/sed output to a txt file and screenshot THAT.

Claims to verify for PR #${pr.num}:
${pr.claims.map((c, i) => `  ${i + 1}. ${c}`).join('\n')}

Guidance:
- For docs claims (PR 21): grep the actual files (README.md, docs/arenas.md, CONTRIBUTING.md), run \`atv-bench games\`, \`atv-bench harnesses\`, \`plan-schedule --seed\` twice to prove determinism, \`rate --enforce-gates\` and \`lift\` on a thin/phantom corpus to prove fail-closed. ls the 5 proof scripts. Check \`git diff --name-only main...HEAD\` proves doc-only.
- For feature claims (PR 22): \`atv-bench quickstart --help\`, exercise \`--yes --json --model ...\` non-interactive path, confirm \`atv-bench harnesses\` lists codex as live/runnable, grep pyproject.toml for questionary, confirm scorecard-example.png exists, run the hermetic test suite for the touched modules if fast (e.g. \`uv run pytest -q tests/ -k quickstart\` — capture pass count; do NOT claim 1020 unless you actually run the full suite, otherwise mark PARTIAL with what you observed).
- If a claim genuinely cannot be exercised (needs network/Docker/live auth), mark UNTESTABLE and say why. Prefer VERIFIED/FAILED with real evidence.
- Never fabricate. evidence must quote real command output. screenshot field = the PNG path you created for that claim.

Return the structured JSON with one entry per claim and the list of all screenshot paths you produced.`
}

function secPrompt(pr) {
  return `You are running the ATV unified security audit (the /atv-security skill) against GitHub PR #${pr.num} in All-The-Vibes/ATV-bench.
The FULL skill methodology is at: ${SEC_SKILL} — READ IT FIRST with \`cat\`, then apply it.
The PR head worktree is at: ${pr.wt}. Scan the PR's CHANGED files plus the config surfaces.

Apply the skill's phases to this PR's surface:
- Phase 2/3 (Config): scan .github/**, .vscode/** for the 33-rule taxonomy (Secrets SEC-*, MCP-*, HOOK-*, AGENT-*, PERM-*, INJ-*, ACC-*, EXEC-*, SETUP-*).
- Phase 4 (OWASP Top 10 2021): scan changed Python source (this is a Python/uv project — src/**, scripts/**) for injection, broken auth, sensitive data exposure, SSRF, insecure deserialization, hardcoded secrets, command/path injection (esp. subprocess/docker/network calls in the quickstart + adapter code for PR 22).
- Phase 5 (STRIDE): threat-model the changed code paths.
- Phase 6: grade per surface with N/A semantics.

Focus on what the PR ACTUALLY changes (git -C ${pr.wt} diff --name-only main...HEAD). Docs-only diffs (PR 21) should still get a config scan but OWASP/STRIDE is N/A if no source changed.
Be precise: a finding needs a real rule ID, file, and evidence. Do not invent findings. Report critical/high counts and an overall grade.

Return the structured JSON.`
}

// ---- Phases 1-3: pipeline both PRs through recon -> e2e -> security ----
log('Reviewing PRs #21 and #22: recon -> screenshotted E2E -> atv-security scan')

const perPr = await pipeline(
  PRS,
  (pr) => agent(reconPrompt(pr), { label: `recon:pr${pr.num}`, phase: 'Recon', schema: RECON_SCHEMA }),
  (recon, pr) =>
    agent(e2ePrompt(pr), { label: `e2e:pr${pr.num}`, phase: 'E2E', schema: E2E_SCHEMA }).then((e2e) => ({ pr: pr.num, recon, e2e })),
  (bundle, pr) =>
    agent(secPrompt(pr), { label: `sec:pr${pr.num}`, phase: 'Security', schema: SEC_SCHEMA }).then((sec) => ({ ...bundle, sec })),
)

const results = perPr.filter(Boolean)

// ---- Phase 4: synthesize merge order + review report ----
phase('Synthesize')
const synthesisInput = JSON.stringify(results, null, 2)
const report = await agent(
  `You are the lead reviewer synthesizing an end-to-end review of two PRs in All-The-Vibes/ATV-bench.
Here is the collected evidence (recon + screenshotted E2E claim verification + atv-security scan) for each PR:

${synthesisInput}

Write a decisive Markdown review report to ${PROOF}/REVIEW_REPORT.md using the Write capability (via Bash heredoc or a python script). It MUST contain, per PR:
- Body-accuracy verdict: are the PR body's claims accurate? (cite VERIFIED/FAILED/PARTIAL counts and any failed claim)
- Screenshot evidence: list the PNG proof paths captured under docs/proof/pr-review-2126/
- Security: grade + critical/high counts + any real findings
- CI: green? Conflicts? Mergeable?
And overall:
- MERGE ORDER recommendation with justification (consider: docs PR #21 is low-risk/small; feature PR #22 is large. Does either depend on the other? Would merging one first cause the other to conflict or need a rebase? PR 21 documents features from #19/#20 already on main; PR 22 adds new code + a questionary dep. Recommend a concrete order and say why.)
- A final GO / NO-GO per PR and any blocking issues.

Return a concise plain-text executive summary (the same content you wrote to the file), including the recommended merge order and per-PR GO/NO-GO. This summary will be handed to independent reviewers.`,
  { label: 'synthesize', phase: 'Synthesize' },
)

// ---- Phase 5: Santa-loop — dual independent review, codex must verify ----
phase('SantaReview')

const rubric = `RUBRIC (each criterion PASS/FAIL with objective condition):
| Criterion | Pass Condition |
| Evidence-backed | Every "VERIFIED" claim is backed by a real screenshot/command output path, not asserted |
| Body accuracy | The report's body-accuracy verdicts match the underlying evidence |
| Security soundness | atv-security findings are correctly triaged; no critical/high left unflagged |
| CI & conflicts | CI-green and merge-conflict conclusions match the recon data |
| Merge order justified | The recommended merge order has a sound, dependency-aware rationale |
| No overclaiming | No fabricated results; UNTESTABLE/PARTIAL used honestly |
| Completeness | All body claims for both PRs are addressed |`

const reviewPacket = `INDEPENDENT REVIEW — you have NOT seen any other review. Find problems; do not rubber-stamp.

You are verifying the correctness of an end-to-end PR review (PRs #21 and #22 of All-The-Vibes/ATV-bench).
The full evidence bundle and the reviewer's synthesized report/merge-order decision follow.

${rubric}

=== SYNTHESIZED REPORT ===
${report}

=== RAW EVIDENCE BUNDLE (recon + e2e + security per PR) ===
${synthesisInput}

Screenshot proof PNGs live under ${PROOF}/ . The report file is ${PROOF}/REVIEW_REPORT.md .
Evaluate every rubric criterion as PASS or FAIL and return the structured JSON verdict.`

// Reviewer A: Claude Opus code-reviewer (context-isolated)
// Reviewer B: codex exec (external model) — REQUIRED to verify per the goal
const reviewerBPrompt = `You are Reviewer B in a santa-loop dual review, running as codex (an independent external model).
A Claude-based review of PRs #21 and #22 needs independent verification. Use \`codex exec --sandbox read-only\` to inspect the repo at ${REPO} and the proof artifacts at ${PROOF}/ if helpful, then evaluate the packet below.

Run this to get codex's independent verdict, then RELAY codex's answer as your structured JSON:

Write the packet to a temp file and invoke codex:
  PF=$(mktemp /tmp/santa-b-XXXXXX.txt)
  cat > "$PF" <<'SANTA_EOF'
${reviewPacket}

Return ONLY a JSON object: {"verdict":"PASS|FAIL","checks":[{"criterion","result","detail"}],"critical_issues":[],"suggestions":[]}
SANTA_EOF
  codex exec --sandbox read-only -C ${REPO} - < "$PF" 2>&1 | tail -80
  rm -f "$PF"

Parse codex's returned JSON verdict. If codex could not run or returned malformed output, set verdict FAIL with a critical_issue explaining that codex verification did not complete. Return the structured JSON reflecting CODEX's verdict (this is the required codex verification step).`

let round = 0
let santa = null
while (round < 3) {
  round++
  const [a, b] = await parallel([
    () =>
      agent(`${reviewPacket}\n\nYou are an independent quality reviewer (Reviewer A). Return the structured JSON verdict.`, {
        label: `santa:A:r${round}`,
        phase: 'SantaReview',
        agentType: 'code-reviewer',
        model: 'opus',
        schema: VERDICT_SCHEMA,
      }),
    () =>
      agent(reviewerBPrompt, {
        label: `santa:B:codex:r${round}`,
        phase: 'SantaReview',
        schema: VERDICT_SCHEMA,
      }),
  ])
  santa = { round, a, b }
  const aPass = a && a.verdict === 'PASS'
  const bPass = b && b.verdict === 'PASS'
  log(`Santa round ${round}: Reviewer A (opus)=${a ? a.verdict : 'ERR'}  Reviewer B (codex)=${b ? b.verdict : 'ERR'}`)
  if (aPass && bPass) {
    santa.nice = true
    break
  }
  santa.nice = false
  // Collect issues for the record; report is read-only evidence synthesis, so we surface rather than auto-edit code.
  const issues = [
    ...((a && a.critical_issues) || []).map((i) => `A: ${i}`),
    ...((b && b.critical_issues) || []).map((i) => `B(codex): ${i}`),
  ]
  log(`NAUGHTY — issues to reconcile: ${issues.join(' | ') || '(none listed)'}`)
  // Re-synthesize the report incorporating the reviewers' critical issues, then re-review.
  if (round < 3) {
    await agent(
      `Reviewers flagged issues with the PR review report at ${PROOF}/REVIEW_REPORT.md. Update that file to address these critical issues (correct any inaccurate verdicts, add missing evidence caveats, fix merge-order rationale). Do NOT fabricate; if an issue reflects a genuine evidence gap, state the limitation honestly.

Critical issues:
${issues.map((i) => `- ${i}`).join('\n')}

Return the corrected executive summary text.`,
      { label: `santa:fix:r${round}`, phase: 'SantaReview' },
    ).then((fixed) => {
      santa.fixedSummary = fixed
    })
  }
}

return {
  prs: results.map((r) => ({
    pr: r.pr,
    body_accurate: r.e2e?.overall_body_accurate,
    e2e_summary: r.e2e?.summary,
    screenshots: r.e2e?.screenshots,
    security_grade: r.sec?.grade,
    security_critical: r.sec?.critical,
    security_high: r.sec?.high,
    ci_green: r.recon?.ci_all_green,
    conflicts: r.recon?.conflicts,
    mergeable: r.recon?.mergeable,
  })),
  report_file: `${PROOF}/REVIEW_REPORT.md`,
  santa_verdict: santa?.nice ? 'NICE' : 'NAUGHTY (escalated)',
  santa_rounds: santa?.round,
  reviewer_A: santa?.a?.verdict,
  reviewer_B_codex: santa?.b?.verdict,
  executive_summary: santa?.fixedSummary || report,
}
