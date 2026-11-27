// Rendered with an absolute checkout root. No provider calls or stdout logging.
import { execFile } from 'node:child_process';
const root = __ASHA_ROOT__;

export default function (pi) {
  let memory = '';
  const environment = (ctx) => ({ ...process.env, ASHA_ROOT: root,
    ASHA_HARNESS: 'pi', ASHA_SESSION_ID: ctx.sessionManager.getSessionId() });
  const payload = (ctx, fields = {}) => ({ session_id: ctx.sessionManager.getSessionId(), cwd: ctx.cwd, ...fields });
  const run = (file, args, input, ctx) => new Promise(resolve => {
    const child = execFile(file, args, { env: environment(ctx), timeout: 4000, maxBuffer: 128 * 1024 },
      (error, stdout, stderr) => resolve({ code: error ? error.code : 0, stdout, stderr }));
    child.stdin?.on('error', () => {});
    child.stdin?.end(JSON.stringify(input));
  });
  const control = async (name, ctx, fields = {}) => {
    if (!process.env.ASHA_HUB_SESSION_ID) return;
    await run(root + '/plugins/session/hooks/handlers/control-event.sh', [name], payload(ctx, fields), ctx);
  };
  pi.on('session_start', async (event, ctx) => {
    memory = '';
    await control('SessionStart', ctx, { source: ['new', 'resume', 'fork'].includes(event.reason) ? 'pi' + event.reason : 'startup' });
    const profile = process.env.ASHA_SESSION_PROFILE;
    if (profile === 'worker' || !(['chair', 'room'].includes(profile) || process.env.ASHA_PERSONA === '1')) return;
    const result = await run('python3', [root + '/plugins/session/tools/memory_v2.py', 'startup-context', '--project-dir', ctx.cwd], {}, ctx);
    if (result.code === 0) memory = result.stdout.trim();
    else console.error('Asha: published project Memory unavailable; verify project state before relying on it.');
  });
  pi.on('before_agent_start', async (event) => memory ? { systemPrompt: event.systemPrompt + '\n\n' + memory } : undefined);
  pi.on('agent_start', async (_event, ctx) => control('UserPromptSubmit', ctx));
  pi.on('tool_execution_start', async (_event, ctx) => control('PreToolUse', ctx));
  pi.on('tool_execution_end', async (_event, ctx) => control('PostToolUse', ctx));
  // agent_end can precede retries and queued work; only settled means idle.
  pi.on('agent_settled', async (_event, ctx) => control('Stop', ctx, { stop_hook_active: true }));
  pi.on('session_shutdown', async (event, ctx) => {
    if (event.reason === 'quit') await control('SessionEnd', ctx);
  });
  pi.on('tool_call', async (event, ctx) => {
    const result = await run(root + '/plugins/session/hooks/handlers/pi-policy-adapter.sh', [],
      payload(ctx, { tool_name: event.toolName, tool_input: event.input }), ctx);
    if (result.code === 2) return { block: true, reason: result.stderr.trim() || 'Blocked by Asha policy' };
    if (result.code !== 0) console.error('Asha: policy handler unavailable; native Pi permissions remain in effect.');
  });
}
