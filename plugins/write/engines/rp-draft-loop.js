export const meta = {
  name: 'rp-draft-loop',
  description: 'Profile-driven drafting engine. mode:"solo" = one agent drafts + self-audits against the profile (cheap default). mode:"gate" = Prose -> Critic + Continuity in parallel -> iterate (scrutiny tier). Profiles: rp | hush. Returns the passage + what was caught.',
  whenToUse: 'Pass {profileConfig:<resolved mode manifest>, beatBrief, mode?:"solo"|"gate", contextFile?, maxIterations?, reviewerModel?, draftModel?}. The caller resolves .claude/modes/<mode>.yaml into a flat profileConfig (see plugins/write/engines/README.md). profileConfig points agents at its own rubric/voice/bible/continuity-authority/craftCore. Output stays in chat; never written to a manuscript or canon file.',
  phases: [
    { title: 'Draft', detail: 'Prose agent drafts the passage from the profile canon + brief' },
    { title: 'Review', detail: 'Critic (voice/exposition) + Continuity (canon/POV) score in parallel' },
    { title: 'Revise', detail: 'Prose redrafts against both critiques; re-score (cap 3 rounds)' },
  ],
}

// ---- generic engine: all project-specific wiring arrives via args.profileConfig (a resolved flat mode manifest).
// The PROJECT resolves .claude/modes/<mode>.yaml into a flat profileConfig and passes it in; this engine
// carries NO project paths. profileConfig keys:
//   { mode, label, unit, rubric, voiceSpec, craftCore, continuityAuthority, bible, context,
//     defaultRunMode?, reviewerModel?, draftModel? }
// See plugins/write/engines/README.md for the contract + the manifest->profileConfig resolver mapping. ----

// ---- inputs ----
// The Workflow runtime passes `args` as a JSON string (not a parsed object), so parse it here.
// Tolerate both forms (string → parse; object → use as-is; undefined → {}).
let a
try {
  a = typeof args === 'string' ? JSON.parse(args) : (args || {})
} catch (e) {
  return { error: `rp-draft-loop: args was a string but not valid JSON: ${e.message}` }
}
const P = a.profileConfig
if (!P) {
  return { error: 'rp-draft-loop is manifest-driven: pass args.profileConfig (a resolved flat mode manifest). See plugins/write/engines/README.md — resolve .claude/modes/<mode>.yaml first.' }
}
const profileKey = a.profile || P.mode || 'custom'
const CORE = P.craftCore
if (!CORE) {
  return { error: 'profileConfig.craftCore is required (absolute path to the shared craft-core).' }
}
const brief = a.beatBrief || a.brief || 'Continue from the exact end-state of the context file.'
const ctxFile = a.contextFile || P.context
const maxRounds = a.maxIterations || 3
const mode = a.mode || P.defaultRunMode || 'gate' // 'solo' = one agent draft + self-audit (cheap); 'gate' = independent Critic+Continuity loop (scrutiny)
const reviewerModel = a.reviewerModel || P.reviewerModel || 'sonnet' // gate reviewers grade an explicit checklist — cheaper than the drafter
const draftModel = a.draftModel || P.draftModel || null // null = inherit the session model for drafting
const draftOpts = (label, phase) => {
  const o = { label, phase, schema: PROSE_SCHEMA }
  if (draftModel) o.model = draftModel
  return o
}

const SOURCES = `Profile: ${P.label}. Read these before drafting/scoring:
- Shared craft-core (universal auto-fails + directives — apply on EVERY profile): ${CORE}
- Your rubric (profile-specific directives + scoring criteria): ${P.rubric}
- Voice spec: ${P.voiceSpec}
- Continuity authority: ${P.continuityAuthority}
- Bible / character canon: ${P.bible}
- Working text / scene context (read for state, surrounding prose, and any locked beat): ${ctxFile}`

function prosePrompt(revisionNote) {
  return `You are the PROSE agent in a drafting loop for ${P.label}. Read the sources, then draft ONE ${P.unit}.

${SOURCES}

Follow the PROSE directives in your rubric EXACTLY and render in the register of the voice spec. If the scene context supplies a LOCKED beat (staging, on-page lines, do-not-translate / do-not-resolve rules, dials), it overrides any drafting instinct — honor it to the letter.
${revisionNote ? `\nTHIS IS A REVISION. The previous draft was REJECTED. Fix EVERY item below and introduce no new violations:\n${revisionNote}\n` : ''}
BRIEF:
${brief}

Return the structured field \`beat\` containing ONLY the finished prose — no preamble, no commentary, no explanation of choices, no headers, no notes. Do NOT write to any file.`
}

function soloPrompt() {
  return `You are the PROSE ENGINE (solo self-audit mode) for ${P.label}. Read the sources, draft ONE ${P.unit}, THEN audit your own draft against your rubric's CRITIC and CONTINUITY checks and fix every issue before returning.

${SOURCES}

Follow the PROSE directives in your rubric EXACTLY; render in the voice spec's register; honor any LOCKED beat in the scene context to the letter.

BRIEF:
${brief}

SELF-AUDIT (do this silently, then return only the corrected prose): check your draft against every CRITIC auto-fail and CONTINUITY category named in your rubric AND the shared craft-core — POV / psychic-awareness, canon speech & voice-budgets, telegraphing, verbal tics, exposition-dump, failure-to-advance, invented mechanics, setting / locked-beat breaks, softened stakes, flatness, AND the universal craft auto-fails (both_true_resolution, narrated_contradiction, editorializing_close) — and rewrite to eliminate each.

Return: \`beat\` = ONLY the finished, self-corrected prose (no preamble/headers/notes; do NOT write to any file); \`selfCaught\` = a short list of issues you caught and fixed (one short phrase each, e.g. "pov: cut 'he decides'"), or [] if none.`
}

const SOLO_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    beat: { type: 'string' },
    selfCaught: { type: 'array', items: { type: 'string' } },
  },
  required: ['beat', 'selfCaught'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    pass: { type: 'boolean' },
    violations: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          category: { type: 'string' },
          severity: { type: 'string', enum: ['auto-fail', 'minor'] },
          quote: { type: 'string' },
          why: { type: 'string' },
          fix: { type: 'string' },
        },
        required: ['category', 'severity', 'quote', 'why', 'fix'],
      },
    },
    revision_directive: { type: 'string' },
  },
  required: ['pass', 'violations', 'revision_directive'],
}

const PROSE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: { beat: { type: 'string' } },
  required: ['beat'],
}

function criticPrompt(draft) {
  return `You are the CRITIC agent for ${P.label}. Read the shared craft-core (${CORE}), your rubric (${P.rubric}), and voice spec (${P.voiceSpec}). Score the DRAFT below against BOTH the CRITIC section of your rubric AND every auto-fail in the shared craft-core (voice/register, mechanical/telegraphic phrasing, exposition density, the universal auto-fails — both_true_resolution, narrated_contradiction, editorializing_close — and any profile-specific auto-fails). Be adversarial — assume flaws exist; if your first pass finds nothing, look again. PASS only per your rubric's stated thresholds. Quote the offending text verbatim and give a concrete fix in each violation. If it passes, return pass=true with empty violations and empty revision_directive; if it fails, write revision_directive as one concrete paragraph the Prose agent can act on.

DRAFT:
"""
${draft}
"""`
}

function continuityPrompt(draft) {
  return `You are the CONTINUITY agent for ${P.label}. Read your continuity authority (${P.continuityAuthority}), your rubric (${P.rubric}), the bible/character canon (${P.bible}), and the scene context (${ctxFile} — including any LOCKED beat in its comment-block). Score the DRAFT below against the CONTINUITY categories DEFINED IN YOUR PROFILE RUBRIC ONLY. PASS only if there are zero violations. Quote the offending text verbatim and give a concrete fix. If it passes, return pass=true with empty violations and empty revision_directive; if it fails, write revision_directive as one concrete paragraph the Prose agent can act on.

DRAFT:
"""
${draft}
"""`
}

// ---- solo mode: one agent drafts + self-audits; no independent reviewers, no loop ----
if (mode === 'solo') {
  const soloOpts = { label: `engine:solo:${profileKey}`, phase: 'Draft', schema: SOLO_SCHEMA }
  if (draftModel) soloOpts.model = draftModel
  const r = await agent(soloPrompt(), soloOpts)
  if (!r || !r.beat) {
    return { beat: null, profile: profileKey, mode: 'solo', error: 'engine returned nothing' }
  }
  log(`[${profileKey}] solo: self-caught ${(r.selfCaught || []).length} issue(s)`)
  return { beat: r.beat, profile: profileKey, mode: 'solo', selfCaught: r.selfCaught || [] }
}

// ---- gate mode: draft ----
let draftObj = await agent(prosePrompt(''), draftOpts(`prose:draft:${profileKey}`, 'Draft'))
let draft = draftObj && draftObj.beat
if (!draft) {
  return { beat: null, profile: profileKey, converged: false, rounds: 0, unresolved: ['prose agent returned nothing'], critic: null, continuity: null }
}

// ---- iterate: score in parallel, redraft on failure, cap at maxRounds ----
let crit = null
let cont = null
let round = 0
const caughtLog = []

for (round = 1; round <= maxRounds; round++) {
  const [c, k] = await parallel([
    () => agent(criticPrompt(draft), { label: `critic:${profileKey}:r${round}`, phase: 'Review', schema: VERDICT_SCHEMA, model: reviewerModel }),
    () => agent(continuityPrompt(draft), { label: `continuity:${profileKey}:r${round}`, phase: 'Review', schema: VERDICT_SCHEMA, model: reviewerModel }),
  ])
  crit = c
  cont = k

  const critPass = !!(c && c.pass)
  const contPass = !!(k && k.pass)
  const critViol = (c && c.violations) || []
  const contViol = (k && k.violations) || []
  log(`[${profileKey}] Round ${round}: critic ${critPass ? 'PASS' : 'FAIL'} (${critViol.length} viol), continuity ${contPass ? 'PASS' : 'FAIL'} (${contViol.length} viol)`)

  if (critPass && contPass) break

  critViol.forEach((v) => caughtLog.push(`critic:${v.category}`))
  contViol.forEach((v) => caughtLog.push(`continuity:${v.category}`))

  if (round < maxRounds) {
    const directive = [
      !critPass && c ? `CRITIC fixes required:\n${c.revision_directive || critViol.map((v) => `- ${v.category}: ${v.fix}`).join('\n')}` : '',
      !contPass && k ? `CONTINUITY fixes required:\n${k.revision_directive || contViol.map((v) => `- ${v.category}: ${v.fix}`).join('\n')}` : '',
    ].filter(Boolean).join('\n\n')
    const revObj = await agent(prosePrompt(directive), draftOpts(`prose:revise:${profileKey}:r${round}`, 'Revise'))
    draft = revObj && revObj.beat
    if (!draft) {
      return { beat: null, profile: profileKey, converged: false, rounds: round, unresolved: ['prose agent returned nothing on revision'], critic: crit, continuity: cont }
    }
  }
}

const converged = !!(crit && crit.pass && cont && cont.pass)
const finalUnresolved = []
;((crit && crit.violations) || []).forEach((v) => finalUnresolved.push(`critic:${v.category}`))
;((cont && cont.violations) || []).forEach((v) => finalUnresolved.push(`continuity:${v.category}`))
const uniq = (arr) => arr.filter((x, i) => arr.indexOf(x) === i)

return {
  beat: draft,
  profile: profileKey,
  converged,
  rounds: round > maxRounds ? maxRounds : round,
  finalCriticPass: !!(crit && crit.pass),
  finalContinuityPass: !!(cont && cont.pass),
  caughtAndFixed: uniq(caughtLog),
  unresolved: converged ? [] : uniq(finalUnresolved),
  critic: crit,
  continuity: cont,
}
