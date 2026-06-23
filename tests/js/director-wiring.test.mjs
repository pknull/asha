#!/usr/bin/env node
/* director-wiring.test.mjs — deterministic live-execution test for the optional
 * Director (anti-rush pacing) reviewer in plugins/write/engines/rp-draft-loop.js.
 *
 * The engine is a Workflow-format script: it references free globals
 * (args/agent/parallel/log/phase), uses top-level await, and `return`s its
 * result. The Workflow runtime wraps it in an async function with those globals
 * injected. We replicate that here with mocked agents so we can execute the REAL
 * engine logic and assert the Director's runtime behaviour:
 *
 *   A. directorRubric set      -> 3 reviewers fire in ONE parallel batch; a
 *      failing Director verdict blocks convergence and forces a redraft whose
 *      directive carries the Director's pacing fix; director fields populated.
 *   B. directorRubric absent   -> only 2 reviewers fire; director fields are
 *      undefined (zero cost).
 *
 * Usage: node director-wiring.test.mjs [path/to/rp-draft-loop.js]
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const ENGINE_PATH = process.argv[2] || resolve(HERE, '..', '..', 'plugins', 'write', 'engines', 'rp-draft-loop.js');

function loadEngineRunner(enginePath) {
  let src = readFileSync(enginePath, 'utf8');
  // `export const meta = {...}` is invalid inside a Function body; demote it.
  src = src.replace(/export\s+const\s+meta/, 'const meta');
  // Wrap so the engine's top-level `return`s resolve the async IIFE.
  const body = `return (async () => {\n${src}\n})();`;
  // eslint-disable-next-line no-new-func
  const factory = new Function('args', 'agent', 'parallel', 'log', 'phase', body);
  return (env) => factory(env.args, env.agent, env.parallel, env.log, env.phase);
}

const run = loadEngineRunner(ENGINE_PATH);

// --- minimal harness primitives -------------------------------------------- #
function makeHarness(verdictFor) {
  const calls = [];          // every agent label, in call order
  const parallelBatches = []; // arrays of labels submitted together to parallel()
  const prompts = {};        // label -> prompt text (last seen)

  const agent = async (prompt, opts) => {
    const label = opts.label;
    calls.push(label);
    prompts[label] = prompt;
    return verdictFor(label, prompt);
  };
  const parallel = async (thunks) => {
    // Record the batch's labels by peeking: run thunks, collecting labels as the
    // agent fires. We capture the slice of `calls` produced by this batch.
    const before = calls.length;
    const out = await Promise.all(thunks.map((t) => t()));
    parallelBatches.push(calls.slice(before).sort());
    return out;
  };
  const log = () => {};
  const phase = () => {};
  return { agent, parallel, log, phase, calls, parallelBatches, prompts };
}

const baseProfile = {
  mode: 'rp',
  label: 'RP',
  unit: 'beat',
  rubric: '/x/rubric.md',
  voiceSpec: '/x/voice.md',
  craftCore: '/x/craft-core-universal.md',
  continuityAuthority: '/x/continuity.md',
  bible: '/x/bible.md',
  context: '/x/context.md',
};

let failures = 0;
function check(name, cond) {
  if (cond) { console.log(`  ✓ ${name}`); }
  else { console.error(`  ✗ ${name}`); failures++; }
}

function pass() { return { pass: true, violations: [], revision_directive: '' }; }

// --- Test A: Director present, fails round 1, passes round 2 ---------------- #
async function testWithDirector() {
  console.log('Test A: directorRubric set — 3 reviewers, failing Director forces redraft');
  const h = makeHarness((label) => {
    if (label.startsWith('prose:draft')) return { beat: 'RUSHED DRAFT v1' };
    if (label.startsWith('prose:revise')) return { beat: 'SLOWER DRAFT v2' };
    if (label.startsWith('critic')) return pass();
    if (label.startsWith('continuity')) return pass();
    if (label.startsWith('director')) {
      if (label.includes(':r1')) {
        return {
          pass: false,
          violations: [{ category: 'rush_to_climax', severity: 'auto-fail',
            quote: 'lands the idea', why: 'arrives inside the beat', fix: 'withhold the payoff' }],
          revision_directive: 'Withhold the payoff; render only the approach.',
        };
      }
      return pass(); // r2+
    }
    throw new Error(`unexpected agent label: ${label}`);
  });

  const result = await run({
    ...h,
    args: { profileConfig: { ...baseProfile, directorRubric: '/x/director-rubric.md' },
      beatBrief: 'A deliberately rushed beat that sprints to its payoff.' },
  });

  const firstReview = h.parallelBatches[0] || [];
  check('round 1 ran exactly 3 reviewers in one parallel batch', firstReview.length === 3);
  check('the parallel batch included a director:*:r1 reviewer',
    firstReview.some((l) => /^director:.*:r1$/.test(l)));
  check('the parallel batch included critic + continuity',
    firstReview.some((l) => l.startsWith('critic')) && firstReview.some((l) => l.startsWith('continuity')));
  check('a redraft was triggered after the Director FAIL (prose:revise fired)',
    h.calls.some((l) => l.startsWith('prose:revise')));
  const reviseLabel = h.calls.find((l) => l.startsWith('prose:revise'));
  check('the redraft directive carried the Director pacing fix',
    !!reviseLabel && /DIRECTOR \(pacing\) fixes required/.test(h.prompts[reviseLabel]));
  check('engine converged after the Director started passing', result.converged === true);
  check('finalDirectorPass is true in the return', result.finalDirectorPass === true);
  check('director verdict object present in the return', !!result.director);
  check('rounds === 2 (one redraft)', result.rounds === 2);
  check("caughtAndFixed records 'director:rush_to_climax'",
    Array.isArray(result.caughtAndFixed) && result.caughtAndFixed.includes('director:rush_to_climax'));
}

// --- Test B: Director absent -> zero cost ---------------------------------- #
async function testWithoutDirector() {
  console.log('Test B: directorRubric absent — 2 reviewers, director fields undefined');
  const h = makeHarness((label) => {
    if (label.startsWith('prose:draft')) return { beat: 'DRAFT' };
    if (label.startsWith('prose:revise')) return { beat: 'DRAFT2' };
    if (label.startsWith('critic')) return pass();
    if (label.startsWith('continuity')) return pass();
    if (label.startsWith('director')) throw new Error('director must NOT run when directorRubric is unset');
    throw new Error(`unexpected agent label: ${label}`);
  });

  const result = await run({
    ...h,
    args: { profileConfig: { ...baseProfile }, beatBrief: 'A clean beat.' },
  });

  const firstReview = h.parallelBatches[0] || [];
  check('round 1 ran exactly 2 reviewers', firstReview.length === 2);
  check('no director:* reviewer fired', !h.calls.some((l) => l.startsWith('director')));
  check('finalDirectorPass is undefined (omitted)', result.finalDirectorPass === undefined);
  check('director field is undefined (omitted)', result.director === undefined);
  check('engine still converged', result.converged === true);
}

(async () => {
  console.log(`engine: ${ENGINE_PATH}\n`);
  await testWithDirector();
  console.log('');
  await testWithoutDirector();
  console.log('');
  if (failures) { console.error(`FAILED: ${failures} assertion(s)`); process.exit(1); }
  console.log('All director-wiring assertions passed.');
})();
