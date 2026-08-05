export const meta = {
  name: 'commission-loop',
  description: 'Adversarial commissioning harness. N independent workers draft against a brief, each claim cited to a named source; per-draft verifier panels try to REFUTE the claims against the sources themselves; a ranker orders only the survivors. Returns a ranked, citation-checked shortlist — the engine never writes a file, so promotion is always the caller’s explicit act.',
  whenToUse: 'Pass {brief, sources:[paths], unit?, workers?, angles?, verifierLenses?, requireCitations?, maxShortlist?, workerModel?, verifierModel?, rankModel?, verifierAgentType?, context?}. Use when fan-out drafting is wanted but fabrication is the risk: canon-grounded narrative beats, design proposals argued from real files, migration plans citing real call sites. Output stays in chat; nothing is written to project files.',
  phases: [
    { title: 'Commission', detail: 'independent workers draft from the brief + sources, one angle each' },
    { title: 'Verify', detail: 'per-draft adversarial panel — each lens tries to refute the claims against the sources' },
    { title: 'Rank', detail: 'survivors ranked for fidelity + grounding; rejects returned with their findings' },
  ],
}

// ---- generic engine: all project wiring arrives via args. No project paths,
// no built-in domain vocabulary. The write boundary is structural: workers and
// verifiers RETURN text; the engine returns a report; no stage writes a file.
// A draft is a proposal, not a commit — same contract as the RP turn loop,
// where the only path into project state runs through the caller. ----

let a
try {
  a = typeof args === 'string' ? JSON.parse(args) : (args || {})
} catch (e) {
  return { error: `commission-loop: args was a string but not valid JSON: ${e.message}` }
}

const brief = a.brief
const sources = Array.isArray(a.sources) ? a.sources.filter(Boolean) : []
if (!brief) return { error: 'commission-loop: args.brief is required — the commission each worker drafts against.' }
if (sources.length === 0) {
  return { error: 'commission-loop: args.sources is required (array of file paths). The sources are the ground truth verifiers refute against; a commission with no sources has nothing to verify claims by — use a plain agent instead.' }
}

const unit = a.unit || 'artifact'
const workers = Math.max(1, a.workers || 3)
const maxShortlist = Math.max(1, a.maxShortlist || 3)
const requireCitations = a.requireCitations !== false
const lenses = (Array.isArray(a.verifierLenses) && a.verifierLenses.length > 0)
  ? a.verifierLenses
  : ['fabrication', 'contradiction']
const workerModel = a.workerModel || null      // null = inherit session model
const verifierModel = a.verifierModel || 'sonnet' // refuting a checklist is cheaper than drafting
const rankModel = a.rankModel || null
const verifierAgentType = a.verifierAgentType || null // e.g. 'claim-verifier' — a Read/Grep/Glob-only agent makes the verifier read-only STRUCTURALLY, not just by instruction
const ctxNote = a.context ? `\nAdditional working context (read, not a claim source): ${a.context}` : ''

// Deterministic diversity: no randomness (the Workflow runtime forbids it, and
// resumability wants identical prompts per index). Angles cycle by worker index.
const DEFAULT_ANGLES = [
  'conservative — the minimal version, hewing closest to what the sources already establish',
  'ambitious — the fullest version the brief and sources can support without inventing beyond them',
  'skeptical — treat the brief’s own assumptions as suspect; draft the version that survives that scrutiny',
]
const angles = (Array.isArray(a.angles) && a.angles.length > 0) ? a.angles : DEFAULT_ANGLES
const angleFor = (i) => angles[i % angles.length]

const SOURCE_LIST = sources.map((s) => `- ${s}`).join('\n')

const NO_WRITE = 'Do NOT write, edit, or create any file. Return output only. Your work is a PROPOSAL for human review — promotion into project files is the caller’s decision, never yours.'

const DRAFT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    artifact: { type: 'string' },
    claims: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          claim: { type: 'string' },
          source: { type: 'string' },
          quote: { type: 'string' },
        },
        required: ['claim', 'source', 'quote'],
      },
    },
  },
  required: ['artifact', 'claims'],
}

const VERIFY_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    verdict: { type: 'string', enum: ['pass', 'fail'] },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          type: { type: 'string', enum: ['fabrication', 'uncited', 'misquote', 'contradiction', 'other'] },
          detail: { type: 'string' },
          claim: { type: 'string' },
        },
        required: ['type', 'detail'],
      },
    },
  },
  required: ['verdict', 'findings'],
}

const RANK_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    ranking: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          draft: { type: 'integer' },
          rationale: { type: 'string' },
        },
        required: ['draft', 'rationale'],
      },
    },
  },
  required: ['ranking'],
}

function workerPrompt(i) {
  return `You are WORKER ${i + 1} of ${workers} in an adversarial commissioning harness. Draft ONE ${unit} for the brief below. Other workers are drafting the same brief from other angles; a verifier panel will try to REFUTE your claims against the sources, so ground everything.

Your angle: ${angleFor(i)}

Sources (the only ground truth; read them before drafting):
${SOURCE_LIST}${ctxNote}

BRIEF:
${brief}

Rules:
- Every factual assertion your ${unit} makes about the world of the sources MUST appear in the claims array: the claim, the source path it comes from, and the load-bearing quote VERBATIM (paraphrase is where qualifiers die).${requireCitations ? `\n- An assertion you cannot cite does not go in the ${unit}. If the sources are silent on something you need, say so inside the ${unit} ("not established in sources") rather than inventing it.` : ''}
- Invented specifics — names, mechanics, numbers, events the sources do not establish — are the failure this harness exists to catch. The verifiers are instructed to assume you fabricated something.
- ${NO_WRITE}`
}

function lensBody(lens) {
  if (lens === 'fabrication') {
    return `Your lens: FABRICATION. For EVERY entry in the claims array: open the named source and verify the quote appears there and actually supports the claim. A quote that is absent, altered, or stretched past what it says = finding (misquote/fabrication). Then sweep the ${unit} itself for factual assertions NOT in the claims array${requireCitations ? ' — each is an "uncited" finding' : ''}. Invented names, mechanics, numbers, or events = fabrication.`
  }
  if (lens === 'contradiction') {
    return `Your lens: CONTRADICTION. Read the sources, then the ${unit}. Find every statement — cited or not — that CONTRADICTS what the sources establish. A claim can quote its source accurately and still contradict a different source; that is exactly the case this lens exists for.`
  }
  return `Your lens: ${lens.toUpperCase()}. Judge the ${unit} strictly through this lens against the sources. Any concrete failure under this lens is a finding (type "other", detail naming the lens).`
}

function verifierPrompt(lens, draft) {
  return `You are an ADVERSARIAL VERIFIER in a commissioning harness. Your job is to REFUTE the draft below, not to approve it. Assume it contains at least one fabrication and hunt for it; if your first pass finds nothing, look again. Approval without demonstrated effort is a failed verification. When genuinely uncertain whether a claim is supported, the verdict is fail — an unverifiable claim must not ride a pass into promotion.

Sources (ground truth):
${SOURCE_LIST}

${lensBody(lens)}

Verdict rule: ANY finding of type fabrication, uncited, misquote, or contradiction means verdict=fail. Findings of type "other" fail only if they violate your lens materially. ${NO_WRITE}

DRAFT ${unit} (with its claims register):
"""
${JSON.stringify(draft)}
"""`
}

function rankPrompt(survivors) {
  const blocks = survivors.map((s) => `--- DRAFT ${s.i + 1} (angle: ${s.angle}; claims: ${s.draft.claims.length}; verifier findings: ${s.findingsCount}) ---\n${s.draft.artifact}`).join('\n\n')
  return `You are the RANKER in a commissioning harness. Every draft below already survived adversarial verification against the sources — do not re-verify. Rank them (best first) by: fidelity to the brief, how much of the ${unit} is grounded in the sources vs merely compatible with them, and craft. Return at most ${maxShortlist} entries in the ranking array, each rationale one concrete sentence naming what won or lost it the spot. ${NO_WRITE}

BRIEF:
${brief}

${blocks}`
}

// ---- Commission -> Verify: pipeline, no barrier — a draft enters verification
// the moment it exists, while slower workers still draft. ----
const idxs = []
for (let i = 0; i < workers; i++) idxs.push(i)

const results = await pipeline(
  idxs,
  async (i) => {
    const opts = { label: `commission:worker:${i + 1}`, phase: 'Commission', schema: DRAFT_SCHEMA }
    if (workerModel) opts.model = workerModel
    const draft = await agent(workerPrompt(i), opts)
    return { i, angle: angleFor(i), draft }
  },
  async (r) => {
    if (!r || !r.draft || !r.draft.artifact) return { ...(r || {}), failed: 'worker returned nothing' }
    const verdicts = await parallel(lenses.map((lens) => () => {
      const opts = { label: `verify:${lens}:draft${r.i + 1}`, phase: 'Verify', schema: VERIFY_SCHEMA, model: verifierModel }
      if (verifierAgentType) opts.agentType = verifierAgentType
      return agent(verifierPrompt(lens, r.draft), opts).then((v) => ({ lens, v }))
    }))
    const usable = verdicts.filter((x) => x && x.v)
    // A verifier that died is a fail, not a shrug: an unverified draft must not
    // ride a missing verdict into the shortlist.
    const missing = lenses.length - usable.length
    const failed = usable.filter((x) => x.v.verdict !== 'pass')
    const survived = missing === 0 && failed.length === 0
    const findingsCount = usable.reduce((n, x) => n + (x.v.findings || []).length, 0)
    return { ...r, verdicts: usable, missingVerdicts: missing, survived, findingsCount }
  }
)

const done = results.filter(Boolean)
const survivors = done.filter((r) => r.survived)
const rejected = done.filter((r) => !r.survived).map((r) => ({
  draft: r.i + 1,
  angle: r.angle,
  failed: r.failed || null,
  missingVerdicts: r.missingVerdicts || 0,
  findings: (r.verdicts || []).flatMap((x) => (x.v.findings || []).map((f) => ({ lens: x.lens, ...f }))),
  artifact_excerpt: r.draft && r.draft.artifact ? r.draft.artifact.slice(0, 280) : null,
}))

log(`commission-loop: ${done.length}/${workers} drafted, ${survivors.length} survived verification, ${rejected.length} rejected`)

// ---- Rank: a true barrier — ranking needs every survivor at once. Skipped
// when there is nothing to compare. ----
let shortlist = []
if (survivors.length === 1) {
  shortlist = [{ rank: 1, draft: survivors[0].i + 1, angle: survivors[0].angle, artifact: survivors[0].draft.artifact, claims: survivors[0].draft.claims, rationale: 'sole survivor of verification' }]
} else if (survivors.length > 1) {
  const ranked = await agent(rankPrompt(survivors), { label: 'commission:rank', phase: 'Rank', schema: RANK_SCHEMA, ...(rankModel ? { model: rankModel } : {}) })
  const order = (ranked && Array.isArray(ranked.ranking)) ? ranked.ranking : []
  const byIndex = new Map(survivors.map((s) => [s.i + 1, s]))
  shortlist = order
    .filter((e) => byIndex.has(e.draft))
    .slice(0, maxShortlist)
    .map((e, pos) => {
      const s = byIndex.get(e.draft)
      return { rank: pos + 1, draft: e.draft, angle: s.angle, artifact: s.draft.artifact, claims: s.draft.claims, rationale: e.rationale }
    })
  // Ranker misbehavior (empty/unknown indices) must not eat verified work:
  // fall back to verification order rather than returning nothing.
  if (shortlist.length === 0) {
    shortlist = survivors.slice(0, maxShortlist).map((s, pos) => ({ rank: pos + 1, draft: s.i + 1, angle: s.angle, artifact: s.draft.artifact, claims: s.draft.claims, rationale: 'ranker returned no usable ranking; verification order' }))
  }
}

return {
  shortlist,
  rejected,
  stats: {
    workers,
    drafted: done.filter((r) => !r.failed).length,
    survived: survivors.length,
    lenses,
    requireCitations,
  },
  promotion_note: 'Nothing has been written to any file. Promote a shortlisted artifact by explicit, human-reviewed action — the same gate discipline as the RP turn loop.',
}
