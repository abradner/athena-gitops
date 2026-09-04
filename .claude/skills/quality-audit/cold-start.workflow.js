// Cold-start quality audit — fleet dig → adversarial verify → completeness → coherence.
// Invoke with Workflow({scriptPath, args}) where args is:
// {
//   dir:  "<scratch dir holding commits-<key>.txt per tranche>",
//   head: "<sha the working tree is checked out at>",
//   tranches: [{ key:"T1", era:"...", range:"a..b", commitsFile:"<dir>/commits-T1.txt",
//                slices:[{ id:"t1-messaging", focus:"...", security?:true }] }],
//   lenses:  [{ id:"frontend", lens:"..." }, ...]      // cross-cutting coherence reviewers
// }
export const meta = {
  name: 'quality-audit-cold-start',
  description: 'Fleet audit of N tranches: dig (sonnet), adversarial verify, completeness, cross-cut coherence',
  phases: [
    { title: 'Dig', detail: 'subsystem + security diggers per tranche', model: 'sonnet (default; operator-selectable via args.diggerModel)' },
    { title: 'Completeness', detail: 'one assessor per tranche, PR-body + tickets driven', model: 'sonnet' },
    { title: 'Verify', detail: 'adversarial refutation of high/medium findings' },
    { title: 'Coherence', detail: 'cross-cutting reviewers over all tranches' },
  ],
}

const { dir: DIR, head: HEAD, tranches: TRANCHES, lenses: LENSES } = args
const DIGGER_MODEL = args.diggerModel || 'sonnet'
const TRACKER = args.tracker || "this repo's tracker as named in AGENTS.md — if AGENTS.md names none, say so rather than guessing"

const FINDINGS_SCHEMA = {
  type: 'object', required: ['findings', 'slice_health', 'patterns_observed', 'loose_ends'],
  properties: {
    findings: { type: 'array', items: { type: 'object',
      required: ['file', 'line', 'category', 'severity', 'summary', 'evidence', 'suggested_direction'],
      properties: { file: { type: 'string' }, line: { type: 'integer' },
        category: { enum: ['correctness', 'architecture', 'smell', 'maintainability', 'simplicity', 'security'] },
        severity: { enum: ['high', 'medium', 'low'] },
        summary: { type: 'string' }, evidence: { type: 'string' }, suggested_direction: { type: 'string' } } } },
    slice_health: { type: 'string' },
    patterns_observed: { type: 'array', items: { type: 'string' } },
    loose_ends: { type: 'array', items: { type: 'string' } },
  },
}
const VERDICTS_SCHEMA = { type: 'object', required: ['verdicts'], properties: { verdicts: { type: 'array', items: { type: 'object',
  required: ['index', 'verdict', 'note'], properties: { index: { type: 'integer' }, verdict: { enum: ['confirmed', 'refuted', 'fixed_later'] }, note: { type: 'string' } } } } } }
const COMPLETENESS_SCHEMA = { type: 'object', required: ['features', 'tranche_verdict'], properties: {
  features: { type: 'array', items: { type: 'object', required: ['feature', 'prs', 'promised', 'shipped', 'pct_complete', 'missing', 'confidence'],
    properties: { feature: { type: 'string' }, prs: { type: 'array', items: { type: 'integer' } }, promised: { type: 'string' }, shipped: { type: 'string' },
      pct_complete: { type: 'integer' }, missing: { type: 'array', items: { type: 'string' } }, confidence: { enum: ['high', 'medium', 'low'] } } } },
  tranche_verdict: { type: 'string' } } }
const COHERENCE_SCHEMA = { type: 'object', required: ['themes', 'summary'], properties: {
  themes: { type: 'array', items: { type: 'object', required: ['title', 'observation', 'evidence', 'recommendation'],
    properties: { title: { type: 'string' }, observation: { type: 'string' }, evidence: { type: 'string' }, recommendation: { type: 'string' } } } },
  summary: { type: 'string' } } }

function digPrompt(tr, s) {
  return [
    'You are one digger in a retrospective code-quality audit. You audit ONE slice of ONE tranche of landed work.',
    'Tranche ' + tr.key + ' era: ' + tr.era, 'Git range: ' + tr.range, 'HEAD (working tree): ' + HEAD,
    'Your slice: ' + s.focus, '',
    'Method: read the commit list at ' + tr.commitsFile + ' (bot bumps already excluded); run `git diff <range> --stat -- <paths>` then the FULL diff for your slice; read files at HEAD where the diff is ambiguous (later tranches may already have fixed things — still report; a verifier marks fixed_later). `git show <sha>` for commit messages, `gh pr view <N>` for what a PR promised. Ignore lockfiles/generated files.',
    'Dimensions: correctness, architecture, code smells, maintainability, simplicity' + (s.security ? ', and ESPECIALLY security (dedicated security slice)' : '') + '.',
    'At most 12 findings, most significant first. Severity honestly: high = bites users/operators or structurally corrosive. `file`+`line` valid at HEAD.',
    'Also: slice_health (one paragraph); patterns_observed (conventions good or bad — the coherence pass consumes these); loose_ends (stubs, TODOs, dead toggles, half-wired paths, "next ticket" mentions — be generous; the completeness pass consumes these).',
    'No praise-padding. A clean slice gets few findings and says so.',
  ].join('\n')
}

phase('Dig')
log('Fanning out ' + TRANCHES.reduce((n, t) => n + t.slices.length, 0) + ' diggers across ' + TRANCHES.length + ' tranches')

const trancheResults = await parallel(TRANCHES.map(tr => async () => {
  const digs = await parallel(tr.slices.map(s => () =>
    agent(digPrompt(tr, s), { label: 'dig:' + s.id, phase: 'Dig', model: DIGGER_MODEL, schema: FINDINGS_SCHEMA })))
  const sliceResults = tr.slices.map((s, i) => ({ slice: s.id, result: digs[i] }))
  const looseEnds = digs.flatMap((d, i) => d ? d.loose_ends.map(l => '[' + tr.slices[i].id + '] ' + l) : [])
  const all = digs.flatMap((d, i) => (d ? d.findings.map(f => ({ ...f, slice: tr.slices[i].id })) : []))
  const highMed = all.filter(f => f.severity !== 'low')
  const chunks = []; for (let i = 0; i < highMed.length; i += 5) chunks.push(highMed.slice(i, i + 5))
  log(tr.key + ': ' + all.length + ' findings (' + highMed.length + ' high/med), ' + looseEnds.length + ' loose ends')

  const [comp, verdictChunks] = await Promise.all([
    agent([
      'You assess FEATURE COMPLETENESS for tranche ' + tr.key + ': ' + tr.era, 'Range ' + tr.range + '; commit list ' + tr.commitsFile + '; HEAD ' + HEAD,
      'Method: group commits into features; `gh pr view <N> --json title,body` for what was PROMISED; try ' + TRACKER + ' for matching tickets and say if unreachable; check code at HEAD for gap evidence. Loose ends from the dig pass:',
      looseEnds.length ? looseEnds.map(l => '  - ' + l).join('\n') : '  (none)',
      'For each feature: promised vs shipped, honest pct_complete (intuitive leap allowed, label confidence), CONCRETE missing pieces each ticket-ready, and whether each deferral has a real ticket vs a prose mention. Then tranche_verdict.',
    ].join('\n'), { label: 'complete:' + tr.key, phase: 'Completeness', model: 'sonnet', schema: COMPLETENESS_SCHEMA }),
    parallel(chunks.map((c, ci) => () =>
      agent([
        'Adversarial verifier. Findings below come from a first-pass reviewer over ' + tr.range + '. Try to REFUTE each: read code at HEAD (' + HEAD + '), `git log --oneline <file>` for later fixes, check tests, judge whether the scenario actually happens. Verdicts: confirmed / refuted / fixed_later. `index` is the 0-based position below — the first item is 0, not 1. Default to refuted when evidence does not hold; note what you checked.',
        '', ...c.map((f, i) => i + '. [' + f.slice + '][' + f.severity + '][' + f.category + '] ' + f.file + ':' + f.line + ' — ' + f.summary + ' | evidence: ' + f.evidence),
      ].join('\n'), { label: 'verify:' + tr.key + ':' + ci, phase: 'Verify', schema: VERDICTS_SCHEMA })
        .then(v => ({ chunk: c, verdicts: v ? v.verdicts : null })))),
  ])
  const verified = []
  for (const vc of verdictChunks.filter(Boolean)) vc.chunk.forEach((f, i) => {
    const v = vc.verdicts ? vc.verdicts.find(x => x.index === i) : null
    verified.push({ ...f, verdict: v ? v.verdict : 'unverified', verdict_note: v ? v.note : 'verifier failed' })
  })
  const lows = all.filter(f => f.severity === 'low').map(f => ({ ...f, verdict: 'unverified_low', verdict_note: '' }))
  return { tranche: tr.key, range: tr.range, sliceResults, completeness: comp, findings: [...verified, ...lows], looseEnds }
}))

phase('Coherence')
const solid = trancheResults.filter(Boolean)
const patternDump = solid.flatMap(t => t.sliceResults.filter(x => x.result).flatMap(x => x.result.patterns_observed.map(p => '[' + t.tranche + '/' + x.slice + '] ' + p))).join('\n')
const healthDump = solid.flatMap(t => t.sliceResults.filter(x => x.result).map(x => '[' + t.tranche + '/' + x.slice + '] ' + x.result.slice_health)).join('\n\n')
const titles = solid.flatMap(t => t.findings.filter(f => f.verdict === 'confirmed').map(f => '[' + t.tranche + '/' + f.slice + '][' + f.category + '] ' + f.summary)).join('\n')
const coherence = await parallel(LENSES.map(l => () => agent([
  'You are a cross-cutting architecture reviewer over ' + TRANCHES.length + ' eras of work (' + TRANCHES.map(t => t.key + ' = ' + t.era).join('; ') + '). Your lens: ' + l.lens,
  'Read actual code at HEAD where you need to settle a question — do not just aggregate. Judge DRIFT and DIVERGENCE across eras: where did conventions shift without old code being brought along? Where do parallel implementations of the same idea coexist?',
  '== Patterns observed ==', patternDump, '', '== Slice health verdicts ==', healthDump, '', '== Confirmed finding titles ==', titles,
].join('\n'), { label: 'coherence:' + l.id, phase: 'Coherence', schema: COHERENCE_SCHEMA })))

return { head: HEAD, tranches: solid, coherence: LENSES.map((l, i) => ({ lens: l.id, result: coherence[i] })) }
