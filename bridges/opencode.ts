/**
 * Asha Hooks Bridge for OpenCode
 *
 * Translates OpenCode plugin events to Claude Code hook format,
 * then spawns the same shell scripts used by Claude Code.
 *
 * Events mapped:
 *   - tool.execute.before/after -> PostToolUse hooks
 *   - chat.message -> UserPromptSubmit hooks
 *   - session.idle (via event) -> SessionEnd hooks
 *
 * Installation:
 *   Copy this file to .opencode/plugin/asha-hooks.ts
 *   Or run: asha/install.sh
 */

import type { Plugin } from "@opencode-ai/plugin";
import { spawn } from "bun";
import { readFileSync, existsSync } from "fs";
import { join } from "path";

// --- Types ---
interface HookCommand {
  type: string;
  command: string;
}

interface HookMatcher {
  matcher?: string;
  hooks?: HookCommand[];
}

interface HooksConfig {
  hooks?: {
    PostToolUse?: HookMatcher[];
    UserPromptSubmit?: HookMatcher[];
    SessionEnd?: HookMatcher[];
  };
}

// --- Tool Name Transformation ---
// OpenCode uses lowercase, Claude Code uses PascalCase
const SPECIAL_TOOLS: Record<string, string> = {
  webfetch: "WebFetch",
  websearch: "WebSearch",
  todoread: "TodoRead",
  todowrite: "TodoWrite",
};

function transformToolName(name: string): string {
  const lower = name.toLowerCase();
  if (SPECIAL_TOOLS[lower]) return SPECIAL_TOOLS[lower];

  // Handle snake_case or kebab-case
  if (name.includes("-") || name.includes("_")) {
    return name
      .split(/[-_]+/)
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
      .join("");
  }

  // Simple capitalization
  return name.charAt(0).toUpperCase() + name.slice(1);
}

// --- Case Conversion ---
// Convert camelCase to snake_case for Claude Code compatibility
function toSnakeCase(obj: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(obj)) {
    const snakeKey = k.replace(/[A-Z]/g, (l) => `_${l.toLowerCase()}`);
    if (v && typeof v === "object" && !Array.isArray(v)) {
      result[snakeKey] = toSnakeCase(v as Record<string, unknown>);
    } else {
      result[snakeKey] = v;
    }
  }
  return result;
}

// --- Hook Execution ---
async function executeHook(
  command: string,
  stdinJson: string,
  cwd: string
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  const home = process.env.HOME || Bun.env.HOME || "";

  // Expand path variables (matching Claude Code behavior)
  const expanded = command
    .replace(/^~(?=\/|$)/g, home)
    .replace(/\$CLAUDE_PROJECT_DIR/g, cwd)
    .replace(/\$\{CLAUDE_PROJECT_DIR\}/g, cwd)
    .replace(/\$OPENCODE_PROJECT_DIR/g, cwd)
    .replace(/\$\{OPENCODE_PROJECT_DIR\}/g, cwd)
    // Remove quotes around path variables (common in hooks.json)
    .replace(/^"([^"]+)"$/, "$1");

  const proc = spawn(["bash", "-c", expanded], {
    cwd,
    stdin: "pipe",
    stdout: "pipe",
    stderr: "pipe",
    env: {
      ...process.env,
      CLAUDE_PROJECT_DIR: cwd,
      OPENCODE_PROJECT_DIR: cwd,
    },
  });

  proc.stdin.write(stdinJson);
  proc.stdin.end();

  const stdout = await new Response(proc.stdout).text();
  const stderr = await new Response(proc.stderr).text();
  const exitCode = await proc.exited;

  return { exitCode, stdout: stdout.trim(), stderr: stderr.trim() };
}

// --- Config Loading ---
function loadHooksConfig(cwd: string): HooksConfig | null {
  const configPath = join(cwd, ".claude", "hooks", "hooks.json");
  if (!existsSync(configPath)) return null;
  try {
    return JSON.parse(readFileSync(configPath, "utf-8"));
  } catch {
    return null;
  }
}

// --- Pattern Matching ---
function matchesTool(toolName: string, matcher?: string): boolean {
  if (!matcher || matcher === "*") return true;

  const patterns = matcher.split("|").map((p) => p.trim());
  return patterns.some((p) => {
    if (p.includes("*")) {
      const regex = new RegExp(`^${p.replace(/\*/g, ".*")}$`, "i");
      return regex.test(toolName);
    }
    return p.toLowerCase() === toolName.toLowerCase();
  });
}

// --- Plugin Export ---
export const AshaHooks: Plugin = async ({ directory, client }) => {
  const config = loadHooksConfig(directory);
  if (!config?.hooks) {
    // No hooks configured, plugin inactive
    return {};
  }

  // Cache tool inputs (OpenCode provides input in 'before', need it in 'after')
  const toolInputCache = new Map<string, Record<string, unknown>>();

  return {
    // Cache tool input for use in tool.execute.after
    "tool.execute.before": async (
      input: { tool: string; sessionID: string; callID: string },
      output: { args: Record<string, unknown> }
    ) => {
      const cacheKey = `${input.sessionID}:${input.callID}`;
      toolInputCache.set(cacheKey, output.args);
    },

    // PostToolUse: Run after tool execution
    "tool.execute.after": async (
      input: { tool: string; sessionID: string; callID: string },
      output: { title: string; output: string; metadata: unknown }
    ) => {
      const hooks = config.hooks?.PostToolUse;
      if (!hooks?.length) return;

      const cacheKey = `${input.sessionID}:${input.callID}`;
      const toolInput = toolInputCache.get(cacheKey) || {};
      toolInputCache.delete(cacheKey);

      const claudeToolName = transformToolName(input.tool);

      const stdinData = {
        session_id: input.sessionID,
        cwd: directory,
        hook_event_name: "PostToolUse",
        tool_name: claudeToolName,
        tool_input: toSnakeCase(toolInput),
        tool_response: {
          output: output.output,
          metadata: output.metadata,
        },
        hook_source: "opencode-asha-bridge",
      };

      for (const matcher of hooks) {
        if (!matchesTool(claudeToolName, matcher.matcher)) continue;

        for (const hook of matcher.hooks || []) {
          if (hook.type !== "command") continue;
          await executeHook(hook.command, JSON.stringify(stdinData), directory);
        }
      }
    },

    // UserPromptSubmit: Run when user submits a message
    "chat.message": async (
      input: {
        sessionID: string;
        agent?: string;
        model?: { providerID: string; modelID: string };
        messageID?: string;
      },
      output: {
        message: Record<string, unknown>;
        parts: Array<{ type: string; text?: string; [key: string]: unknown }>;
      }
    ) => {
      const hooks = config.hooks?.UserPromptSubmit;
      if (!hooks?.length) return;

      // Extract text from message parts
      const textParts = output.parts.filter((p) => p.type === "text" && p.text);
      const prompt = textParts.map((p) => p.text ?? "").join("\n");
      if (!prompt) return;

      const stdinData = {
        session_id: input.sessionID,
        cwd: directory,
        hook_event_name: "UserPromptSubmit",
        prompt,
        hook_source: "opencode-asha-bridge",
      };

      for (const matcher of hooks) {
        for (const hook of matcher.hooks || []) {
          if (hook.type !== "command") continue;

          const result = await executeHook(
            hook.command,
            JSON.stringify(stdinData),
            directory
          );

          // If hook outputs non-JSON text, inject as synthetic message
          // This handles LanguageTool corrections and other system reminders
          if (result.stdout && !result.stdout.startsWith("{")) {
            try {
              await client.session.prompt({
                path: { id: input.sessionID },
                body: {
                  parts: [{ type: "text", text: result.stdout }],
                },
                query: { directory },
              });
            } catch {
              // Injection failed, continue silently
            }
          }
        }
      }
    },

    // SessionEnd: Run when session becomes idle
    event: async (input: {
      event: { type: string; properties?: unknown };
    }) => {
      if (input.event.type !== "session.idle") return;

      const hooks = config.hooks?.SessionEnd;
      if (!hooks?.length) return;

      const props = input.event.properties as
        | { sessionID?: string }
        | undefined;
      if (!props?.sessionID) return;

      const stdinData = {
        session_id: props.sessionID,
        cwd: directory,
        hook_event_name: "SessionEnd",
        reason: "idle",
        hook_source: "opencode-asha-bridge",
      };

      for (const matcher of hooks) {
        for (const hook of matcher.hooks || []) {
          if (hook.type !== "command") continue;
          await executeHook(hook.command, JSON.stringify(stdinData), directory);
        }
      }
    },
  };
};

// Default export for OpenCode plugin discovery
export default AshaHooks;
