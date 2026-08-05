#!/usr/bin/env node
/* commission-wiring.test.mjs — deterministic live-execution test for
 * plugins/write/engines/commission-loop.js.
 *
 * Executes the REAL engine body with mocked Workflow primitives and asserts the
 * harness's load-bearing behavior:
 *
 *   A. a draft whose fabrication verifier fails is REJECTED with its lens-tagged
 *      findings; survivors are ranked; the ranker's order is honored.
 *   B. when every draft fails, the ranker never runs and the shortlist is empty
 *      (silence-is-not-success: the rejects carry their findings out).
 *   C. a sole survivor short-circuits ranking.
 *   D. a missing verifier verdict fails the draft — unverified work must not
 *      ride a dead verifier into the shortlist.
 *   E. the write boundary and citation duty are present in the prompts (the
 *      engine's promise that nothing writes project files is instruction-level
 *      for workers, so it must actually be instructed).
 *
 * Usage: node commission-wiring.test.mjs [path/to/commission-loop.js]
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const ENGINE_PATH = process.argv[2] || resolve(HERE, '..', '..', 'plugins', 'write', 'engines', 'commission-loop.js');

function loadEngineRunner(enginePath) {
  let src = readFileSync(enginePath, 'utf8');
  src = src.replace(/export\s+const\s+meta/, 'const meta');
  const body = `return (async () => {\n${src}\n})();`;
  // eslint-disable-next-line no-new-func
  const factory = new Function('args', 'agent', 'parallel', 'pipeline', 'log', 'phase', body);
  return (env) => factory(env.args, env.agent, env.parallel, env.pipeline, env.log, env.phase);
}

const run = loadEngineRunner(ENGINE_PATH);

function makeHarness(respond) {
  const calls = [];
  const prompts = {};
  const agent = async (prompt, opts) => {
    const label = (opts && opts.label) || 'unlabeled';
    calls.push(label);
    prompts[label] = prompt;
    return respond(label, prompt, opts);
  };
  const parallel = async (thunks) => Promise.all(thunks.map((t) => t()));
  const pipeline = async (items, ...stages) => Promise.all(items.map(async (item) => {
    let v = item;
    for (const s of stages) v = await s(v, item);
    return v;
  }));
  const log = () => {};
  const phase = () => {};
  return { agent, parallel, pipeline, log, phase, calls, prompts };
}

const BASE_ARGS = {
  brief: 'Draft the next beat.',
  sources: ['/x/canon.md', '/x/rules.md'],
  workers: 3,
  unit: 'beat',
};

const draftFor = (n) => ({ artifact: `draft-${n} text`, claims: [{ claim: `c${n}`, source: '/x/canon.md', quote: `q${n}` }] });
const passV = { verdict: 'pass', findings: [] };
const failV = { verdict: 'fail', findings: [{ type: 'fabrication', detail: 'invented mechanic', claim: 'c2' }] };

let failures = 0;
function check(name, cond, extra) {
  if (cond) { console.log(`  ✓ ${name}`); }
  else { failures += 1; console.log(`  ✗ ${name}${extra ? ` — ${extra}` : ''}`); }
}

// --- Scenario A: one fabricator among three ---------------------------------
{
  const h = makeHarness((label) => {
    if (label.startsWith('commission:worker:')) return draftFor(label.slice(-1));
    if (label === 'verify:fabrication:draft2') return failV;
    if (label.startsWith('verify:')) return passV;
    if (label === 'commission:rank') return { ranking: [{ draft: 3, rationale: 'tightest grounding' }, { draft: 1, rationale: 'fullest coverage' }] };
    throw new Error(`unexpected agent: ${label}`);
  });
  const out = await run({ args: BASE_ARGS, ...h });
  console.log('Scenario A: fabricated draft rejected, survivors ranked');
  check('two drafts shortlisted', out.shortlist.length === 2, JSON.stringify(out.shortlist));
  check('ranker order honored (draft 3 first)', out.shortlist[0] && out.shortlist[0].draft === 3);
  check('one draft rejected', out.rejected.length === 1);
  check('rejection carries the lens-tagged finding', out.rejected[0] && out.rejected[0].findings[0] && out.rejected[0].findings[0].lens === 'fabrication' && out.rejected[0].findings[0].type === 'fabrication');
  check('both lenses ran per draft (6 verify calls)', h.calls.filter((c) => c.startsWith('verify:')).length === 6);
  check('ranker ran exactly once', h.calls.filter((c) => c === 'commission:rank').length === 1);
  check('stats reflect survival', out.stats.survived === 2 && out.stats.drafted === 3);
}

// --- Scenario B: everything fails -> no ranker, findings surface -------------
{
  const h = makeHarness((label) => {
    if (label.startsWith('commission:worker:')) return draftFor(label.slice(-1));
    if (label.startsWith('verify:')) return failV;
    throw new Error(`unexpected agent: ${label}`);
  });
  const out = await run({ args: BASE_ARGS, ...h });
  console.log('Scenario B: total rejection');
  check('empty shortlist', out.shortlist.length === 0);
  check('ranker never ran', !h.calls.includes('commission:rank'));
  check('all three rejects carry findings', out.rejected.length === 3 && out.rejected.every((r) => r.findings.length > 0));
}

// --- Scenario C: sole survivor skips ranking ---------------------------------
{
  const h = makeHarness((label) => {
    if (label.startsWith('commission:worker:')) return draftFor(label.slice(-1));
    if (label.endsWith('draft1')) return passV;
    if (label.startsWith('verify:')) return failV;
    throw new Error(`unexpected agent: ${label}`);
  });
  const out = await run({ args: BASE_ARGS, ...h });
  console.log('Scenario C: sole survivor');
  check('shortlist is the sole survivor', out.shortlist.length === 1 && out.shortlist[0].draft === 1);
  check('ranker never ran', !h.calls.includes('commission:rank'));
}

// --- Scenario D: dead verifier fails the draft -------------------------------
{
  const h = makeHarness((label) => {
    if (label.startsWith('commission:worker:')) return draftFor(label.slice(-1));
    if (label === 'verify:contradiction:draft1') return null; // verifier died/skipped
    if (label.startsWith('verify:')) return passV;
    if (label === 'commission:rank') return { ranking: [{ draft: 2, rationale: 'r' }, { draft: 3, rationale: 'r' }] };
    throw new Error(`unexpected agent: ${label}`);
  });
  const out = await run({ args: BASE_ARGS, ...h });
  console.log('Scenario D: missing verdict is a fail');
  check('draft 1 not shortlisted', !out.shortlist.some((s) => s.draft === 1));
  check('draft 1 rejected with missingVerdicts', out.rejected.some((r) => r.draft === 1 && r.missingVerdicts === 1));
}

// --- Scenario E: prompts carry the contract ----------------------------------
{
  const h = makeHarness((label) => {
    if (label.startsWith('commission:worker:')) return draftFor(label.slice(-1));
    if (label.startsWith('verify:')) return passV;
    if (label === 'commission:rank') return { ranking: [{ draft: 1, rationale: 'r' }] };
    throw new Error(`unexpected agent: ${label}`);
  });
  await run({ args: BASE_ARGS, ...h });
  console.log('Scenario E: prompt contract');
  const w = h.prompts['commission:worker:1'] || '';
  const v = h.prompts['verify:fabrication:draft1'] || '';
  check('worker prompt forbids file writes', w.includes('Do NOT write, edit, or create any file'));
  check('worker prompt demands verbatim citations', w.includes('VERBATIM'));
  check('verifier prompt is refute-framed', v.includes('REFUTE') && v.includes('Assume it contains at least one fabrication'));
  check('verifier fails on uncertainty', v.includes('the verdict is fail'));
}

// --- Scenario F: input validation --------------------------------------------
{
  const h = makeHarness(() => { throw new Error('no agent should run'); });
  const noBrief = await run({ args: { sources: ['/x/a.md'] }, ...h });
  const noSources = await run({ args: { brief: 'x' }, ...h });
  console.log('Scenario F: input validation');
  check('missing brief errors without spawning agents', !!noBrief.error && h.calls.length === 0);
  check('missing sources errors with use-a-plain-agent guidance', !!noSources.error && noSources.error.includes('plain agent'));
}

console.log('');
if (failures > 0) {
  console.log(`${failures} assertion(s) failed`);
  process.exit(1);
}
console.log('All commission-loop wiring assertions passed.');
