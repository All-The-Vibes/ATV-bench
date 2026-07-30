export const meta = {
  name: 'pr-review-2930',
  description: 'End-to-end adversarial review of open PRs #29/#30 with screenshot proof, security scan, CI/conflict gates, merge ordering, and codex-verifiable santa-loop',
  phases: [
    { title: 'Recon', detail: 'per-PR body claim extraction + mechanical CI/conflict facts' },
    { title: 'E2E', detail: 'execute every body claim, screenshot each verification' },
    { title: 'Security', detail: 'atv-security config + OWASP/STRIDE per PR' },
    { title: 'Synthesis', detail: 'merge order + consolidated report' },
    { title: 'Santa', detail: 'independent Claude + codex adversarial review' },
  ],
}

const REPO = '/home/sschofield/repos/atv-bench'
const PROOF = `${REPO}/docs/proof/pr-review-2930`
const PRS = [
  { num: 29, wt: `${REPO}/.claude/worktrees/review-pr29`, slug: 'pr29' },
  { num: 30, wt: `${REPO}/.claude/worktrees/review-pr30`, slug: 'pr30' },
]

const SHOT = `${REPO}/scripts/shot_terminal.py`

// ---------- schemas ----------
const RECON = {
  type: 'object',
  required: ['pr', 'claims', 'ci', 'conflicts'],
  properties: {
    pr: { type: 'integer' },
    claims: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'text', 'verify_cmd', 'falsifiable'],
        properties: {
          id: { type: 'string' },
          text: { type: 'string', description: 'the verbatim claim from the PR body' },
          verify_cmd: { type: 'string', description: 'a concrete shell command that would FALSIFY the claim if it is wrong' },
          falsifiable: { type: 'boolean', description: 'false if the claim cannot be mechanically checked' },
        },
      },
    },
    ci: {
      type: 'object',
      required: ['all_green', 'checks', 'notes'],
      properties: {
        all_green: { type: 'boolean' },
        checks: { type: 'array', items: { type: 'string' } },
        notes: { type: 'string', description: 'scope caveats: which lanes actually run, what CI does NOT cover' },
      },
    },
    conflicts: {
      type: 'object',
      required: ['mergeable', 'merge_state', 'rebase_needed'],
      properties: {
        mergeable: { type: 'string' },
        merge_state: { type: 'string' },
        rebase_needed: { type: 'boolean' },
      },
    },
  },
}

const E2E = {
  type: 'object',
  required: ['pr', 'results', 'body_accurate'],
  properties: {
    pr: { type: 'integer' },
    body_accurate: { type: 'boolean' },
    results: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'verdict', 'evidence', 'screenshot'],
        properties: {
          id: { type: 'string' },
          verdict: { type: 'string', enum: ['VERIFIED', 'FAILED', 'UNVERIFIABLE'] },
          evidence: { type: 'string', description: 'actual observed output, quoted — not a paraphrase' },
          screenshot: { type: 'string', description: 'absolute path to the PNG proving it, or empty if none' },
        },
      },
    },
  },
}

const SEC = {
  type: 'object',
  required: ['pr', 'grade', 'critical', 'high', 'findings', 'screenshot'],
  properties: {
    pr: { type: 'integer' },
    grade: { type: 'string' },
    critical: { type: 'integer' },
    high: { type: 'integer' },
    findings: { type: 'array', items: { type: 'string' } },
    screenshot: { type: 'string' },
  },
}

const VERDICT = {
  type: 'object',
  required: ['verdict', 'blocking', 'notes'],
  properties: {
    verdict: { type: 'string', enum: ['NICE', 'NAUGHTY'] },
    blocking: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
  },
}

const shotRule = `
SCREENSHOT PROTOCOL (mandatory — a claim with no screenshot is UNVERIFIABLE, never VERIFIED):
  1. Run the real command, tee its true output:  <cmd> > /tmp/<slug>-<id>.txt 2>&1
  2. Render it:  uv run --no-project --with pillow python ${SHOT} "<title>" /tmp/<slug>-<id>.txt ${PROOF}/<slug>-<id>.png
  3. Report the ABSOLUTE png path in the screenshot field.
Never fabricate output. Never screenshot a command you did not run. If a command fails, screenshot the
failure — a failing screenshot is valid evidence and is far more useful than a missing one.
`

// ---------- Phase 1: recon ----------
phase('Recon')
log(`Reviewing ${PRS.length} open PRs: ${PRS.map(p => '#' + p.num).join(', ')}`)

const recon = await parallel(PRS.map(p => () => agent(
  `You are reviewing PR #${p.num} of the repo All-The-Vibes/ATV-bench.
Worktree with the PR head already checked out: ${p.wt}  (cd there; do NOT touch ${REPO} itself)

TASKS:
1. Read the PR body:  gh pr view ${p.num} --json body,title -q .body
2. Decompose it into ATOMIC, INDIVIDUALLY-CHECKABLE claims. A claim is any assertion about behaviour,
   test counts, coverage, fixes, or CI. For each, write verify_cmd = a command that would FALSIFY it.
   Be adversarial: prefer commands that could expose an overclaim (exact test counts, mutation checks,
   "X is load-bearing" assertions) over ones that trivially pass.
3. Mechanical facts (run these, report exactly):
   - CI:  gh pr checks ${p.num}
     all_green = true ONLY if zero FAIL/pending-failure. In notes, state the SCOPE honestly —
     which lanes run, and what CI does NOT execute (e.g. integration-marked tests).
   - Conflicts:  gh pr view ${p.num} --json mergeable,mergeStateStatus
     rebase_needed = true if the head is behind origin/main in a way requiring rebase.
     Check:  cd ${p.wt} && git log --oneline origin/main..HEAD and git log --oneline HEAD..origin/main

Return the structured object. Do not fix anything. Report only what you observe.`,
  { label: `recon:#${p.num}`, phase: 'Recon', schema: RECON },
)))

const reconOk = recon.filter(Boolean)
log(`Recon complete: ${reconOk.map(r => `#${r.pr} ${r.claims.length} claims, CI green=${r.ci.all_green}`).join(' | ')}`)

// ---------- Phases 2+3: E2E and security, pipelined per PR ----------
phase('E2E')

const perPr = await pipeline(
  reconOk,
  // stage 1 — execute every claim with screenshot proof
  (r) => {
    const p = PRS.find(x => x.num === r.pr)
    return agent(
      `You are the E2E verification agent for PR #${r.pr}. Worktree: ${p.wt} (cd there first).
Your job: EXECUTE each claim and prove the result with a screenshot. You are adversarial — your value
is in catching overclaims, not in confirming them. A claim that is *nearly* right is FAILED, not VERIFIED.

CLAIMS TO VERIFY:
${r.claims.map(c => `- [${c.id}] ${c.text}\n    suggested falsifier: ${c.verify_cmd}`).join('\n')}

${shotRule}

RULES:
- Run the FULL hermetic suite at least once:  uv run pytest -q -m "not integration"
  and screenshot it. Report the EXACT pass/skip/fail counts you observe. If the body states a count
  that differs from what you observe, that specific claim is FAILED and you must quote both numbers.
- For any "this layer is load-bearing / removing it breaks X" claim: actually attempt the mutation in a
  scratch copy and observe the failure. Assertion without execution = UNVERIFIABLE.
- body_accurate = true ONLY if zero claims are FAILED.
- Use slug "${p.slug}" in screenshot filenames.
Return the structured object with an absolute screenshot path per claim.`,
      { label: `e2e:#${r.pr}`, phase: 'E2E', schema: E2E },
    )
  },
  // stage 2 — security, starts as soon as this PR's E2E finishes
  (e2eRes, r) => {
    const p = PRS.find(x => x.num === r.pr)
    return agent(
      `Run the /atv-security audit on PR #${r.pr}. Worktree: ${p.wt} (cd there).
The skill is installed at ~/.claude/skills/atv-security/SKILL.md — READ IT and follow its methodology:
  (a) agentic-config audit of .github/ and .vscode/ (33-rule AgentShield taxonomy: Secrets, Permissions,
      Hooks, MCP Servers, Agents & Skills)
  (b) OWASP Top 10 (2021) static checks + STRIDE threat model on the source.

SCOPE the OWASP/STRIDE pass to what this PR actually changes:
  cd ${p.wt} && git diff --name-only origin/main...HEAD
but run the CONFIG audit repo-wide (a PR can regress workflow permissions anywhere).
PR #29 touches CI workflow files — scrutinise workflow permissions, secret handling, and any
self-hosted-runner exposure with particular care.

${shotRule}
Write the audit summary to a text file and screenshot it as ${PROOF}/${p.slug}-atv-security.png.
Report grade, exact critical/high counts, and each finding. Under-reporting a real finding is the
worst possible outcome — be conservative and flag anything genuinely suspicious.`,
      { label: `security:#${r.pr}`, phase: 'Security', schema: SEC },
    ).then(sec => ({ e2e: e2eRes, sec }))
  },
)

// pipeline yields only the LAST stage's value, so stage 2 explicitly carries the E2E
// result forward — otherwise the body-accuracy findings never reach synthesis.
const joined = perPr.filter(Boolean)
const e2eAll = joined.map(j => j.e2e).filter(Boolean)
const security = joined.map(j => j.sec).filter(Boolean)

const failedClaims = e2eAll.flatMap(e =>
  (e.results || []).filter(r => r.verdict === 'FAILED').map(r => `#${e.pr} ${r.id}: ${r.text || r.evidence}`))
log(`E2E complete: ${e2eAll.map(e => `#${e.pr} body_accurate=${e.body_accurate}`).join(' | ')}${failedClaims.length ? ` | ${failedClaims.length} FAILED claim(s)` : ''}`)

// ---------- Phase 4: synthesis + merge order ----------
phase('Synthesis')

const synthesis = await agent(
  `You are the lead reviewer. Produce the consolidated review report for PRs #29 and #30.

RECON:
${JSON.stringify(reconOk, null, 2)}

E2E CLAIM VERIFICATION (authoritative — these verdicts came from actually running the commands):
${JSON.stringify(e2eAll, null, 2)}

SECURITY:
${JSON.stringify(security, null, 2)}

The E2E agents wrote screenshots into ${PROOF}. Read every *.png filename there (ls ${PROOF}) and also
re-read each PR body (gh pr view N --json body) so your report cites real evidence, not summaries.

DELIVERABLE — write ${PROOF}/REVIEW_REPORT.md containing:
1. Executive summary — per PR: GO / NO-GO, and if any body claim FAILED, say so plainly up front.
   Do NOT smooth over a failed claim. An inaccurate PR body is a defect worth blocking on.
2. Per-PR claim table: claim | verdict | evidence | screenshot path.
3. Security: grade, critical/high counts, findings per PR.
4. CI: green/red per PR **with the honest scope caveat** — state explicitly which lanes run and which
   tests CI never executes.
5. Merge conflicts / rebase needs.
6. **MERGE ORDER** with justification. Consider: are the PRs independent or stacked? Do they touch
   overlapping files (check: git diff --name-only origin/main...HEAD in each worktree)? Does merging one
   force a rebase of the other? Give an explicit numbered sequence and the reason for it.
7. A "known limitations of this review" section — what you could NOT verify and why.

Every factual assertion must cite either a screenshot path, a command output, or a gh query.
Return a concise summary of your findings and the merge order.`,
  { label: 'synthesis', phase: 'Synthesis' },
)

// ---------- Phase 5: santa-loop, Claude + codex ----------
phase('Santa')

const reportPath = `${PROOF}/REVIEW_REPORT.md`

const santaPrompt = `Adversarially review the PR review report at ${reportPath} (repo ${REPO}).
You are the santa-loop reviewer. Return NICE only if EVERY criterion passes; otherwise NAUGHTY.

RUBRIC (objective pass conditions):
1. Accuracy — every claim verdict is backed by cited, reproducible evidence. No assertion without proof.
2. Screenshots — every VERIFIED claim cites a screenshot path that EXISTS on disk. Verify with ls.
3. Honesty — no overclaim. If a PR body is inaccurate the report says so plainly. "CI green" carries
   its scope caveat. Unverifiable items are labelled UNVERIFIABLE, not VERIFIED.
4. Security — atv-security results reported with exact counts, findings not minimised.
5. Merge order — explicitly justified, accounts for file overlap and rebase implications.
6. Completeness — all 4 requested dimensions covered (body accuracy, security, CI, conflicts).
7. No fabrication — spot-check at least 2 cited screenshots exist and at least 1 quoted command output
   is reproducible by re-running it yourself.

Be genuinely adversarial. A report that looks polished but cites a missing screenshot, or that grades a
body "accurate" when a test count is off by even one, is NAUGHTY. List every blocking issue concretely.`

const [claudeVerdict, codexVerdict] = await parallel([
  () => agent(santaPrompt, { label: 'santa:claude', phase: 'Santa', schema: VERDICT, effort: 'high' }),
  () => agent(
    `Run an INDEPENDENT external review using the codex CLI, then report its verdict faithfully.

Codex is verified working headless (codex-cli 0.130.0). Invoke it so it can actually read the repo:

  cd ${REPO} && timeout 900 codex exec --skip-git-repo-check "$(cat <<'PROMPT'
${santaPrompt}

Respond with a final line exactly: VERDICT: NICE  or  VERDICT: NAUGHTY
List each blocking issue on its own line prefixed with "BLOCKING: ".
PROMPT
)" 2>&1 | tee /tmp/codex-santa-2930.txt

Then:
- Screenshot codex's real output:
  uv run --no-project --with pillow python ${SHOT} "codex santa-loop verdict" /tmp/codex-santa-2930.txt ${PROOF}/santa-codex.png
- Parse codex's VERDICT line and report it VERBATIM. Do NOT substitute your own judgement for codex's.
  If codex says NAUGHTY, you return NAUGHTY with its BLOCKING lines.
- If codex fails to run or produces no verdict, return NAUGHTY with a blocking item saying codex could
  not verify — never silently pass a review codex did not actually complete.`,
    { label: 'santa:codex', phase: 'Santa', schema: VERDICT, effort: 'high' },
  ),
])

const verdicts = [claudeVerdict, codexVerdict].filter(Boolean)
const bothNice = verdicts.length === 2 && verdicts.every(v => v.verdict === 'NICE')
const blocking = verdicts.flatMap(v => v.blocking || [])

log(`santa: claude=${claudeVerdict?.verdict ?? 'ERROR'} codex=${codexVerdict?.verdict ?? 'ERROR'} → ${bothNice ? 'CONVERGED' : 'NEEDS FIXES'}`)

return {
  recon: reconOk,
  e2e: e2eAll,
  failedClaims,
  security,
  synthesis,
  santa: { claude: claudeVerdict, codex: codexVerdict, converged: bothNice, blocking },
  report: reportPath,
  proofDir: PROOF,
}
