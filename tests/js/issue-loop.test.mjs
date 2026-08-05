#!/usr/bin/env node
/* issue-loop.test.mjs — deterministic live-execution test for
 * plugins/code/engines/issue-loop.js.
 *
 * Executes the REAL engine body with mocked Workflow primitives and asserts
 * the loop's load-bearing behavior:
 *
 *   A. happy path: fetched issues triage in, workers produce candidates,
 *      cold review passes, publishes are draft-routed, report written.
 *   B. a triage rejection never reaches a worker.
 *   C. the acceptance conjunction is recomputed engine-side — accept=true
 *      with a false criterion is a rejection (labels outrank the label).
 *   D. a dead triage agent is a rejection with a stated reason, never an accept.
 *   E. missing worktree/failing-test evidence demotes a "candidate" — no review,
 *      no publish.
 *   F. a surrendered worker surfaces in outcomes and the report, unpublished.
 *   G. a dead reviewer fails the candidate (unreviewed work must not publish).
 *   H. findings outrank the verdict label: pass + hard finding = fail.
 *   I. max_issues caps dispatch; overflow is deferred and reported, not dropped.
 *   J. prompt contracts: worker gets failing-test-first + never-push + untrusted
 *      issue framing; reviewer is COLD (no worker reasoning in its prompt),
 *      refute-framed, fails on uncertainty; publisher routes through
 *      issue-loop-publish.sh and never composes a raw push.
 *   K. input validation errors without spawning agents.
 *   L. zero accepted issues still writes the report — silence is never success.
 *   M. engine source is Workflow-pure: no Date.now, Math.random, fs, imports.
 *
 * Usage: node issue-loop.test.mjs [path/to/issue-loop.js]
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const ENGINE_PATH = process.argv[2] || resolve(HERE, '..', '..', 'plugins', 'code', 'engines', 'issue-loop.js');

function loadEngineRunner(enginePath) {
  let src = readFileSync(enginePath, 'utf8');
  src = src.replace(/export\s+const\s+meta/, 'const meta');
  const body = `return (async () => {\n${src}\n})();`;
  // eslint-disable-next-line no-new-func
  const factory = new Function('args', 'agent', 'parallel', 'pipeline', 'log', 'phase', body);
  return (env) => factory(env.args, env.agent, env.parallel, env.pipeline, env.log, env.phase);
}

const SRC = readFileSync(ENGINE_PATH, 'utf8');
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
  // Mirror the REAL Workflow runtime contracts: parallel resolves a throwing
  // thunk to null; pipeline passes (prev, originalItem, index) and DROPS an
  // item to null when a stage throws.
  const parallel = async (thunks) => Promise.all(thunks.map((t) => t().catch(() => null)));
  const pipeline = async (items, ...stages) => Promise.all(items.map(async (item, idx) => {
    let v = item;
    try {
      for (const s of stages) v = await s(v, item, idx);
    } catch {
      return null;
    }
    return v;
  }));
  const log = () => {};
  const phase = () => {};
  return { agent, parallel, pipeline, log, phase, calls, prompts };
}

const BASE_ARGS = {
  repo_root: '/repo',
  run_id: '2026-08-05--0400--issue-loop',
  run_dir: 'Work/loops/2026-08-05--0400--issue-loop',
  now_utc: '2026-08-05 04:00 UTC',
  asha_root: '/asha',
  base_branch: 'main',
  config: { test_command: './tests/run-tests.sh', attempt_cap: 3, branch_prefix: 'issue-loop/', max_issues: 5 },
};

const ISSUES = (ns) => ({ issues: ns.map((n) => ({ number: n, title: `bug ${n}`, body: `body of issue ${n}` })) });
const CRITERIA_OK = {
  acceptance_criteria: true, tests_cover_area: true, blast_radius_local: true,
  no_security_surface: true, no_product_decision: true,
};
const triageAccept = { accept: true, criteria: { ...CRITERIA_OK }, reasoning: 'clear', clarification_needed: '', test_surface: 'tests/' };
const triageReject = { accept: false, criteria: { ...CRITERIA_OK, acceptance_criteria: false }, reasoning: 'ambiguous', clarification_needed: 'what does done mean?', test_surface: '' };
const candidateFor = (n) => ({
  status: 'candidate',
  branch: `issue-loop/issue-${n}`,
  worktree_path: `/repo/.asha/worktrees/2026-08-05--0400--issue-loop/issue-${n}`,
  failing_test_path: `tests/test-issue-${n}.sh`,
  failing_test_confirmed: true,
  attempts: 1,
  diagnosis: '',
  files_touched: [`src/thing-${n}.sh`, `tests/test-issue-${n}.sh`],
  summary: `fixed bug ${n}`,
});
const diffOk = (n) => ({ diff: `diff --git a/src/thing-${n}.sh ...`, stat: `2 files changed` });
const reviewPass = { verdict: 'pass', findings: [], summary: 'in scope' };
const publishOk = (n) => ({ published: true, pr_url: `https://github.com/x/y/pull/${n}`, detail: 'draft opened' });
const reportOk = { report_path: 'Work/loops/2026-08-05--0400--issue-loop/report.md' };

let failures = 0;
function check(name, cond, extra) {
  if (cond) { console.log(`  ✓ ${name}`); }
  else { failures += 1; console.log(`  ✗ ${name}${extra ? ` — ${extra}` : ''}`); }
}

// --- Scenario A: happy path ---------------------------------------------------
{
  const h = makeHarness((label) => {
    if (label === 'issues:fetch') return ISSUES([1, 2]);
    if (label.startsWith('triage:')) return triageAccept;
    if (label.startsWith('work:issue-')) return candidateFor(Number(label.split('-').pop()));
    if (label.startsWith('diff:issue-')) return diffOk(Number(label.split('-').pop()));
    if (label.startsWith('review:issue-')) return reviewPass;
    if (label.startsWith('publish:issue-')) return publishOk(Number(label.split('-').pop()));
    if (label === 'report:write') return reportOk;
    throw new Error(`unexpected agent: ${label}`);
  });
  const out = await run({ args: BASE_ARGS, ...h });
  console.log('Scenario A: happy path');
  check('two published outcomes', out.outcomes.filter((o) => o.publish && o.publish.published).length === 2, JSON.stringify(out.outcomes));
  check('report written', out.report_written === true && out.report_path === reportOk.report_path);
  check('stats reflect the run', out.stats.fetched === 2 && out.stats.accepted === 2 && out.stats.published === 2);
  check('report agent saw the run', (h.prompts['report:write'] || '').includes('issue-loop'));
}

// --- Scenario B: rejection never reaches a worker -----------------------------
{
  const h = makeHarness((label) => {
    if (label === 'issues:fetch') return ISSUES([1, 2]);
    if (label === 'triage:issue-1') return triageAccept;
    if (label === 'triage:issue-2') return triageReject;
    if (label.startsWith('work:issue-')) return candidateFor(Number(label.split('-').pop()));
    if (label.startsWith('diff:issue-')) return diffOk(1);
    if (label.startsWith('review:issue-')) return reviewPass;
    if (label.startsWith('publish:issue-')) return publishOk(1);
    if (label === 'report:write') return reportOk;
    throw new Error(`unexpected agent: ${label}`);
  });
  const out = await run({ args: BASE_ARGS, ...h });
  console.log('Scenario B: triage rejection');
  check('no worker for the rejected issue', !h.calls.includes('work:issue-2'));
  check('rejection carries the clarification', out.triage.rejected.some((r) => r.issue === 2 && /done mean/.test(r.clarification_needed || '')));
  check('accepted issue still publishes', out.stats.published === 1);
}

// --- Scenario C: conjunction recomputed engine-side ---------------------------
{
  const lying = { accept: true, criteria: { ...CRITERIA_OK, no_security_surface: false }, reasoning: 'looks fine', clarification_needed: '', test_surface: 'tests/' };
  const h = makeHarness((label) => {
    if (label === 'issues:fetch') return ISSUES([1]);
    if (label === 'triage:issue-1') return lying;
    if (label === 'report:write') return reportOk;
    throw new Error(`unexpected agent: ${label}`);
  });
  const out = await run({ args: BASE_ARGS, ...h });
  console.log('Scenario C: accept=true with a false criterion is a rejection');
  check('issue rejected despite accept=true', out.triage.rejected.some((r) => r.issue === 1));
  check('no worker dispatched', !h.calls.some((c) => c.startsWith('work:')));
}

// --- Scenario D: dead triage agent is a rejection, not silence ----------------
{
  const h = makeHarness((label) => {
    if (label === 'issues:fetch') return ISSUES([1]);
    if (label === 'triage:issue-1') return null;
    if (label === 'report:write') return reportOk;
    throw new Error(`unexpected agent: ${label}`);
  });
  const out = await run({ args: BASE_ARGS, ...h });
  console.log('Scenario D: dead triage agent');
  check('issue rejected with a stated reason', out.triage.rejected.some((r) => r.issue === 1 && /died|no verdict/i.test(r.reason || '')));
  check('never dispatched', !h.calls.some((c) => c.startsWith('work:')));
}

// --- Scenario E: candidate without evidence is demoted ------------------------
{
  const noEvidence = { ...candidateFor(1), failing_test_confirmed: false };
  const h = makeHarness((label) => {
    if (label === 'issues:fetch') return ISSUES([1]);
    if (label === 'triage:issue-1') return triageAccept;
    if (label === 'work:issue-1') return noEvidence;
    if (label === 'report:write') return reportOk;
    throw new Error(`unexpected agent: ${label}`);
  });
  const out = await run({ args: BASE_ARGS, ...h });
  console.log('Scenario E: missing failing-test evidence demotes the candidate');
  check('no review, no publish', !h.calls.some((c) => c.startsWith('review:') || c.startsWith('publish:')));
  check('outcome records the demotion', out.outcomes.some((o) => o.issue === 1 && o.work && o.work.status !== 'candidate'));
}

// --- Scenario E2: worktree outside the convention is demoted ------------------
{
  const rogue = { ...candidateFor(1), worktree_path: '/repo' };
  const h = makeHarness((label) => {
    if (label === 'issues:fetch') return ISSUES([1]);
    if (label === 'triage:issue-1') return triageAccept;
    if (label === 'work:issue-1') return rogue;
    if (label === 'report:write') return reportOk;
    throw new Error(`unexpected agent: ${label}`);
  });
  const out = await run({ args: BASE_ARGS, ...h });
  console.log('Scenario E2: worktree evidence outside .asha/worktrees/ demotes');
  check('no publish for the rogue worktree', !h.calls.some((c) => c.startsWith('publish:')));
  check('demotion recorded', out.outcomes.some((o) => o.issue === 1 && /worktree/i.test((o.work && o.work.diagnosis) || '')));
}

// --- Scenario F: surrender is a reported outcome ------------------------------
{
  const surrendered = { status: 'surrendered', branch: 'issue-loop/issue-1', worktree_path: '/repo/.asha/worktrees/2026-08-05--0400--issue-loop/issue-1', failing_test_path: 'tests/t.sh', failing_test_confirmed: true, attempts: 3, diagnosis: 'suite red after 3 attempts: flaky fixture', files_touched: [], summary: '' };
  const h = makeHarness((label) => {
    if (label === 'issues:fetch') return ISSUES([1]);
    if (label === 'triage:issue-1') return triageAccept;
    if (label === 'work:issue-1') return surrendered;
    if (label === 'report:write') return reportOk;
    throw new Error(`unexpected agent: ${label}`);
  });
  const out = await run({ args: BASE_ARGS, ...h });
  console.log('Scenario F: surrender');
  check('surrender surfaces in outcomes', out.outcomes.some((o) => o.issue === 1 && o.work.status === 'surrendered'));
  check('nothing published', out.stats.published === 0);
  check('report prompt carries the diagnosis', (h.prompts['report:write'] || '').includes('flaky fixture'));
}

// --- Scenario G: dead reviewer fails the candidate ----------------------------
{
  const h = makeHarness((label) => {
    if (label === 'issues:fetch') return ISSUES([1]);
    if (label === 'triage:issue-1') return triageAccept;
    if (label === 'work:issue-1') return candidateFor(1);
    if (label === 'diff:issue-1') return diffOk(1);
    if (label === 'review:issue-1') return null;
    if (label === 'report:write') return reportOk;
    throw new Error(`unexpected agent: ${label}`);
  });
  const out = await run({ args: BASE_ARGS, ...h });
  console.log('Scenario G: dead reviewer');
  check('not published', out.stats.published === 0 && !h.calls.some((c) => c.startsWith('publish:')));
  check('outcome says the reviewer died', out.outcomes.some((o) => o.issue === 1 && /review/i.test((o.review && o.review.summary) || '')));
}

// --- Scenario H: findings outrank the verdict label ---------------------------
{
  const inconsistent = { verdict: 'pass', findings: [{ type: 'scope-creep', detail: 'installer touched', file: 'install.sh' }], summary: 'fine' };
  const h = makeHarness((label) => {
    if (label === 'issues:fetch') return ISSUES([1]);
    if (label === 'triage:issue-1') return triageAccept;
    if (label === 'work:issue-1') return candidateFor(1);
    if (label === 'diff:issue-1') return diffOk(1);
    if (label === 'review:issue-1') return inconsistent;
    if (label === 'report:write') return reportOk;
    throw new Error(`unexpected agent: ${label}`);
  });
  const out = await run({ args: BASE_ARGS, ...h });
  console.log('Scenario H: pass + hard finding = fail');
  check('not published despite verdict=pass', out.stats.published === 0 && !h.calls.some((c) => c.startsWith('publish:')));
}

// --- Scenario I: max_issues caps dispatch, overflow deferred ------------------
{
  const args = { ...BASE_ARGS, config: { ...BASE_ARGS.config, max_issues: 2 } };
  const h = makeHarness((label) => {
    if (label === 'issues:fetch') return ISSUES([1, 2, 3]);
    if (label.startsWith('triage:')) return triageAccept;
    if (label.startsWith('work:issue-')) return candidateFor(Number(label.split('-').pop()));
    if (label.startsWith('diff:issue-')) return diffOk(1);
    if (label.startsWith('review:issue-')) return reviewPass;
    if (label.startsWith('publish:issue-')) return publishOk(1);
    if (label === 'report:write') return reportOk;
    throw new Error(`unexpected agent: ${label}`);
  });
  const out = await run({ args, ...h });
  console.log('Scenario I: run cap');
  check('only two workers dispatched', h.calls.filter((c) => c.startsWith('work:')).length === 2);
  check('third issue deferred, not dropped', out.triage.deferred.length === 1 && out.triage.deferred[0].issue === 3);
  check('report prompt names the deferral', /deferred/i.test(h.prompts['report:write'] || ''));
}

// --- Scenario J: prompt contracts ---------------------------------------------
{
  const h = makeHarness((label) => {
    if (label === 'issues:fetch') return ISSUES([1]);
    if (label === 'triage:issue-1') return triageAccept;
    if (label === 'work:issue-1') return candidateFor(1);
    if (label === 'diff:issue-1') return diffOk(1);
    if (label === 'review:issue-1') return reviewPass;
    if (label === 'publish:issue-1') return publishOk(1);
    if (label === 'report:write') return reportOk;
    throw new Error(`unexpected agent: ${label}`);
  });
  await run({ args: BASE_ARGS, ...h });
  console.log('Scenario J: prompt contracts');
  const t = h.prompts['triage:issue-1'] || '';
  const w = h.prompts['work:issue-1'] || '';
  const r = h.prompts['review:issue-1'] || '';
  const p = h.prompts['publish:issue-1'] || '';
  check('triage treats the issue text as untrusted', /UNTRUSTED/.test(t));
  check('triage marks uncertain criteria false', /uncertain/i.test(t) && /false/.test(t));
  check('worker: failing test FIRST', /failing test/i.test(w) && /FIRST/.test(w));
  check('worker: never pushes', /never push/i.test(w));
  check('worker: worktree convention + attempt cap present', w.includes('.asha/worktrees/') && /3/.test(w));
  check('worker treats the issue text as untrusted', /UNTRUSTED/.test(w));
  check('reviewer is cold: no worker summary/diagnosis leaks', !r.includes('fixed bug 1'));
  check('reviewer sees issue text and diff', r.includes('body of issue 1') && r.includes('diff --git'));
  check('reviewer is refute-framed and fails on uncertainty', /REFUTE|refute/.test(r) && /uncertain/i.test(r) && /fail/.test(r));
  check('reviewer applies the change-budget rule', /plausible surface|Change Budget/i.test(r));
  check('publisher routes through the guarded script', p.includes('issue-loop-publish.sh') && p.includes('/asha'));
  check('publisher forbids raw pushes', /never push directly|only through/i.test(p));
  check('publish happens only after review pass (call order)', h.calls.indexOf('review:issue-1') < h.calls.indexOf('publish:issue-1'));
}

// --- Scenario K: input validation ---------------------------------------------
{
  const h = makeHarness(() => { throw new Error('no agent should run'); });
  const noRoot = await run({ args: { config: BASE_ARGS.config }, ...h });
  const noCfg = await run({ args: { repo_root: '/repo', run_dir: 'Work/loops/x' }, ...h });
  console.log('Scenario K: input validation');
  check('missing repo_root errors without agents', !!noRoot.error && h.calls.length === 0);
  check('missing config errors with preflight guidance', !!noCfg.error && /preflight/i.test(noCfg.error));
}

// --- Scenario L: zero accepted still reports ----------------------------------
{
  const h = makeHarness((label) => {
    if (label === 'issues:fetch') return ISSUES([1]);
    if (label === 'triage:issue-1') return triageReject;
    if (label === 'report:write') return reportOk;
    throw new Error(`unexpected agent: ${label}`);
  });
  const out = await run({ args: BASE_ARGS, ...h });
  console.log('Scenario L: zero accepted');
  check('report still written', h.calls.includes('report:write') && out.report_written === true);
  check('stats say why nothing happened', out.stats.accepted === 0 && out.stats.published === 0);
}

// --- Scenario M: engine source is Workflow-pure -------------------------------
{
  console.log('Scenario M: source purity');
  check('no Date.now / new Date()', !/Date\.now|new Date\(/.test(SRC));
  check('no Math.random', !/Math\.random/.test(SRC));
  check('no fs / imports / require', !/require\(|from ['"]node:|import /.test(SRC));
  check('draft flag is part of the publish contract', /--draft/.test(SRC));
}

console.log('');
if (failures > 0) {
  console.log(`${failures} assertion(s) failed`);
  process.exit(1);
}
console.log('All issue-loop wiring assertions passed.');
