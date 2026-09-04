// Refresh audit — dig NEW tranches, re-verify every open item from the existing
// audit doc at the new HEAD, score the trajectory against the named patterns.
// Invoke with Workflow({scriptPath, args}) where args is:
// {
//   dir:  "<scratch dir>", head: "<sha>", range: "<prevHead>..<head>",
//   numberingNote: "one-line note if tranche numbers changed, else ''",
//   tranches: [{ key:"T5", era:"...", commitsFile:"<dir>/commits-T5.txt",
//                slices:[{ id:"t5-backend", focus:"...", security?:true }] }],
//   openItemsFile: "<dir>/open-items.json",   // array of {id, kind, title, detail|feature...}
//   openCount: 106,
//   priorPatterns: ["1. ...", "2. ..."],       // the audit's named patterns + last verdicts
//   deltaFocus: "the single most direct test this refresh should settle (optional)"
// }
export const meta = {
  name: 'quality-audit-refresh',
  description: 'Audit new tranches, re-verify all open audit items at new HEAD, score trajectory vs the named patterns',
  phases: [
    { title: 'Dig', detail: 'slice diggers per new tranche', model: 'sonnet (default; operator-selectable via args.diggerModel)' },
    { title: 'Reverify', detail: 'every open item from the audit doc re-checked at new HEAD' },
    { title: 'Completeness', detail: 'one assessor per new tranche', model: 'sonnet' },
    { title: 'Verify', detail: 'adversarial refutation of new high/medium findings' },
    { title: 'Delta', detail: 'named patterns scored against the new tranches' },
  ],
}

const { dir: DIR, head: HEAD, range: RANGE, tranches: TRANCHES, openItemsFile: OPEN_FILE, openCount: N_OPEN, priorPatterns: PRIOR } = args
const DIGGER_MODEL = args.diggerModel || 'sonnet'
const TRACKER = args.tracker || "this repo's tracker as named in AGENTS.md — if AGENTS.md names none, say so rather than guessing"
const NOTE = args.numberingNote || ''
const DELTA_FOCUS = args.deltaFocus || ''

const FINDINGS_SCHEMA = { type: 'object', required: ['findings', 'slice_health', 'patterns_observed', 'loose_ends'], properties: {
  findings: { type: 'array', items: { type: 'object', required: ['file', 'line', 'category', 'severity', 'summary', 'evidence', 'suggested_direction'],
    properties: { file: { type: 'string' }, line: { type: 'integer' }, category: { enum: ['correctness', 'architecture', 'smell', 'maintainability', 'simplicity', 'security'] },
      severity: { enum: ['high', 'medium', 'low'] }, summary: { type: 'string' }, evidence: { type: 'string' }, suggested_direction: { type: 'string' } } } },
  slice_health: { type: 'string' }, patterns_observed: { type: 'array', items: { type: 'string' } }, loose_ends: { type: 'array', items: { type: 'string' } } } }
const VERDICTS_SCHEMA = { type: 'object', required: ['verdicts'], properties: { verdicts: { type: 'array', items: { type: 'object', required: ['index', 'verdict', 'note'],
  properties: { index: { type: 'integer' }, verdict: { enum: ['confirmed', 'refuted', 'fixed_later'] }, note: { type: 'string' } } } } } }
const REVERIFY_SCHEMA = { type: 'object', required: ['results'], properties: { results: { type: 'array', items: { type: 'object', required: ['id', 'status', 'evidence'],
  properties: { id: { type: 'integer' }, status: { enum: ['open', 'fixed', 'partially_fixed', 'invalid'] }, evidence: { type: 'string' }, fixed_by: { type: 'string' } } } } } }
const COMPLETENESS_SCHEMA = { type: 'object', required: ['features', 'tranche_verdict'], properties: {
  features: { type: 'array', items: { type: 'object', required: ['feature', 'prs', 'promised', 'shipped', 'pct_complete', 'missing', 'confidence'],
    properties: { feature: { type: 'string' }, prs: { type: 'array', items: { type: 'integer' } }, promised: { type: 'string' }, shipped: { type: 'string' },
      pct_complete: { type: 'integer' }, missing: { type: 'array', items: { type: 'string' } }, confidence: { enum: ['high', 'medium', 'low'] } } } },
  tranche_verdict: { type: 'string' } } }
const DELTA_SCHEMA = { type: 'object', required: ['verdicts', 'summary', 'new_debt', 'focus_verdict'], properties: {
  verdicts: { type: 'array', items: { type: 'object', required: ['pattern', 'direction', 'evidence'],
    properties: { pattern: { type: 'string' }, direction: { enum: ['improved', 'unchanged', 'worsened', 'not_applicable'] }, evidence: { type: 'string' } } } },
  focus_verdict: { type: 'string' }, new_debt: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' } } }

function digPrompt(tr, s) {
  return [
    'You are a digger in a retrospective code-quality audit. You audit ONE slice of ONE new tranche. A prior audit already covers everything before this range and lives at docs/audit/ — skim its Fix-now, Structural themes and Code health sections first so you can say when this tranche repeats, closes, or worsens a known pattern.',
    NOTE, 'Tranche ' + tr.key + ': ' + tr.era,
    'Range for the whole new period: ' + RANGE + ' (tranches may be interleaved on main — use the commit list to know which commits are yours). HEAD ' + HEAD + ' is checked out.',
    'Commit list: ' + tr.commitsFile + '. Your slice: ' + s.focus, '',
    'Method: `git show <sha> --stat` / `git show <sha>` for your commits (messages name trade-offs and deferrals); read files at HEAD where the diff is ambiguous; `gh pr view <N>` for what a PR promised. Ignore lockfiles/generated files.',
    'Dimensions: correctness, architecture, code smells, maintainability, simplicity' + (s.security ? ', and ESPECIALLY security (dedicated security slice)' : '') + '.',
    'At most 12 findings, most significant first, severity honest, `file`+`line` valid at HEAD.',
    'Also: slice_health; patterns_observed (say whether each is NEW, REPEATS a documented pattern, or CLOSES one); loose_ends (stubs, TODOs, half-wired paths, "next ticket" mentions — note whether each has a visible ticket reference). No praise-padding.',
  ].join('\n')
}

phase('Dig')
const reverifyChunks = []
for (let i = 0; i < N_OPEN; i += 6) reverifyChunks.push([i, Math.min(i + 5, N_OPEN - 1)])
log(TRANCHES.reduce((n, t) => n + t.slices.length, 0) + ' diggers + ' + reverifyChunks.length + ' re-verify chunks over ' + N_OPEN + ' open items, concurrently')

const [trancheResults, reverified] = await Promise.all([
  parallel(TRANCHES.map(tr => async () => {
    const digs = await parallel(tr.slices.map(s => () => agent(digPrompt(tr, s), { label: 'dig:' + s.id, phase: 'Dig', model: DIGGER_MODEL, schema: FINDINGS_SCHEMA })))
    const sliceResults = tr.slices.map((s, i) => ({ slice: s.id, result: digs[i] }))
    const all = digs.flatMap((d, i) => (d ? d.findings.map(f => ({ ...f, slice: tr.slices[i].id })) : []))
    const highMed = all.filter(f => f.severity !== 'low')
    const looseEnds = digs.flatMap((d, i) => d ? d.loose_ends.map(l => '[' + tr.slices[i].id + '] ' + l) : [])
    const chunks = []; for (let i = 0; i < highMed.length; i += 5) chunks.push(highMed.slice(i, i + 5))
    log(tr.key + ': ' + all.length + ' findings (' + highMed.length + ' high/med), ' + looseEnds.length + ' loose ends')
    const [comp, verdictChunks] = await Promise.all([
      agent(['You assess FEATURE COMPLETENESS for tranche ' + tr.key + ': ' + tr.era, NOTE, 'Commit list ' + tr.commitsFile + '. HEAD ' + HEAD + '.',
        'Method: group commits into features; `gh pr view <N> --json title,body` for what was PROMISED; try ' + TRACKER + ' and say if unreachable; check code at HEAD. Loose ends from the dig pass:',
        looseEnds.length ? looseEnds.map(l => '  - ' + l).join('\n') : '  (none)',
        'Per feature: promised vs shipped, honest pct_complete, CONCRETE ticket-ready missing pieces, and whether each deferral has a real ticket. Then tranche_verdict.'].join('\n'),
        { label: 'complete:' + tr.key, phase: 'Completeness', model: 'sonnet', schema: COMPLETENESS_SCHEMA }),
      parallel(chunks.map((c, ci) => () => agent([
        'Adversarial verifier over ' + RANGE + ', tranche ' + tr.key + '. Try to REFUTE each finding: read code at HEAD ' + HEAD + ', `git log --oneline <file>` for later in-range fixes (feedback batches and self-review commits fix many first-pass issues), check tests, judge whether the scenario happens. Verdicts: confirmed / refuted / fixed_later. `index` is the 0-based position below — the first item is 0, not 1. Default to refuted when evidence does not hold; note what you checked.',
        '', ...c.map((f, i) => i + '. [' + f.slice + '][' + f.severity + '][' + f.category + '] ' + f.file + ':' + f.line + ' — ' + f.summary + ' | evidence: ' + f.evidence)].join('\n'),
        { label: 'verify:' + tr.key + ':' + ci, phase: 'Verify', schema: VERDICTS_SCHEMA }).then(v => ({ chunk: c, verdicts: v ? v.verdicts : null })))),
    ])
    const verified = []
    for (const vc of verdictChunks.filter(Boolean)) vc.chunk.forEach((f, i) => { const v = vc.verdicts ? vc.verdicts.find(x => x.index === i) : null
      verified.push({ ...f, verdict: v ? v.verdict : 'unverified', verdict_note: v ? v.note : 'verifier failed' }) })
    const lows = all.filter(f => f.severity === 'low').map(f => ({ ...f, verdict: 'unverified_low', verdict_note: '' }))
    return { tranche: tr.key, sliceResults, findings: [...verified, ...lows], completeness: comp, looseEnds }
  })),
  parallel(reverifyChunks.map(([a, b]) => () => agent([
    'You re-verify OPEN ITEMS from a prior code-quality audit against the CURRENT code. ' + NOTE,
    'Read ' + OPEN_FILE + ' (JSON array). Handle ONLY items with id ' + a + ' through ' + b + ' inclusive.',
    'Since the audit, ' + RANGE + ' landed. For each item decide by reading code at HEAD ' + HEAD + ' and `git log --oneline ' + RANGE + ' -- <file>`:',
    '  open — still true as described;  fixed — resolved in range (name commit/PR in fixed_by);  partially_fixed — say what remains;  invalid — was wrong or no longer applicable, say why.',
    'Be strict: "fixed" needs the code to be there, not a commit message claiming it. For completeness-gap items ask whether the specific missing piece now exists. Evidence cites file:line or a commit.'].join('\n'),
    { label: 'reverify:' + a + '-' + b, phase: 'Reverify', schema: REVERIFY_SCHEMA })
    .then(r => {
      // A schema-valid response can still misbehave two ways: under-report (fewer results
      // than ids in [a, b], silently dropping items) or over-report (an id outside [a, b],
      // or the same id twice, which would misattribute or double-count downstream). Keep
      // only the first in-range result per id, fill every id the response never covered.
      // An omission is 'unverified', not 'open': extract-open-items.py re-extracts items
      // regardless of the prior ledger's status, so an id this pass never confirms could
      // already be 'fixed'/'invalid' there — defaulting it to 'open' would let a dropped
      // result silently reopen a closed ledger entry. Synthesis carries the prior state
      // forward for 'unverified' instead of overwriting it (see SKILL.md).
      // `r` itself can also be null — the structured-output retry cap gave up on this whole
      // chunk (see SKILL.md's known failure modes) — a different failure than a well-formed
      // response quietly omitting one id, so the fallback evidence says which one happened.
      const inRange = (r ? r.results : []).filter(x => x.id >= a && x.id <= b)
      const byId = new Map()
      for (const x of inRange) if (!byId.has(x.id)) byId.set(x.id, x)
      const fallbackEvidence = r
        ? 'agent omitted this id from its response — treated as unverified, not silently dropped'
        : 'agent call for this chunk returned no response (schema-retry cap) — treated as unverified, not silently dropped'
      for (let id = a; id <= b; id++) {
        if (!byId.has(id)) byId.set(id, { id, status: 'unverified', evidence: fallbackEvidence })
      }
      return { results: [...byId.values()] }
    })
  )),
])

phase('Delta')
const solid = trancheResults.filter(Boolean)
const patterns = solid.flatMap(t => t.sliceResults.filter(x => x.result).flatMap(x => x.result.patterns_observed.map(p => '[' + t.tranche + '/' + x.slice + '] ' + p)))
const delta = await agent([
  'You score whether the NEWEST tranches (' + solid.map(t => t.tranche).join(', ') + '; range ' + RANGE + ') continued or broke the patterns a prior audit named. ' + NOTE,
  DELTA_FOCUS ? 'The single most direct test to settle: ' + DELTA_FOCUS : '',
  '== The named patterns, with the verdict the previous tranche earned ==', ...PRIOR,
  '== What the dig pass observed in the new tranches ==', patterns.join('\n'),
  'Method: verify against the actual code and diff yourself; do not just aggregate. Rerun counts (e.g. `any` occurrences per SPA) at the previous HEAD and at this HEAD. Check whether new endpoints got the conventions the audit says never back-propagate.',
  'Output: per-pattern direction with concrete evidence from THESE tranches; focus_verdict; new_debt (debt no pattern covers); summary on trajectory.',
].join('\n'), { label: 'delta', phase: 'Delta', schema: DELTA_SCHEMA })

return { range: RANGE, head: HEAD, tranches: solid, reverified: reverified.filter(Boolean).flatMap(r => r.results), delta }
