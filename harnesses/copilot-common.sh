#!/usr/bin/env bash
# harnesses/copilot-common.sh — Copilot content converters shared by the
# install engine (via harnesses/copilot.sh) and the build engine (lib/build.sh,
# `asha build copilot` — Copilot-native plugin packaging).
#
# Extracted from harnesses/copilot.sh (2026-07-01, issue #3) so the plugin
# build can reuse the exact same emitters without dragging in the installer's
# COPILOT_HOME targets and entry points.
#
# Contract expected from the sourcing engine:
#   helpers:  log say die ensure_dir   (info optional)
#   globals:  DRY_RUN
#   tools:    python3 on PATH (frontmatter parsing)

# Plugins never converted for Copilot (Claude-only content).
_COPILOT_SKIP_PLUGINS=()  # no Claude-only plugins currently shipped

_copilot_is_skip_plugin() {
  local p="$1" sp
  [[ ${#_COPILOT_SKIP_PLUGINS[@]} -eq 0 ]] && return 1  # empty-array guard (bash 3.2 + set -u)
  for sp in "${_COPILOT_SKIP_PLUGINS[@]}"; do [[ "$p" == "$sp" ]] && return 0; done
  return 1
}

# Extract the `name:` value from a YAML frontmatter file. Echoes the name
# (or empty string if not present). Looks at the first frontmatter block only.
_copilot_skill_name_from_md() {
  local md="$1"
  python3 - "$md" <<'PYEOF'
import re, sys
text = open(sys.argv[1]).read()
if not text.startswith("---\n"):
    sys.exit(0)
end = text.find("\n---\n", 4)
if end == -1:
    sys.exit(0)
fm = text[4:end]
m = re.search(r"^name\s*:\s*(\S+)", fm, re.MULTILINE)
if m:
    print(m.group(1).strip())
PYEOF
}

# Generate a Copilot-clean SKILL.md from a Claude command MD. Strips the keys
# Copilot's parser is assumed to reject (argument-hint, allowed-tools) plus any
# other keys identified as non-portable. Idempotent (only writes on diff).
_copilot_emit_command_skill() {
  local src="$1" dest="$2"

  # Use python so we can do correct YAML-style frontmatter manipulation.
  local content
  content="$(python3 - "$src" <<'PYEOF'
import re, sys

KEYS_TO_DROP = {"argument-hint", "allowed-tools"}

src = sys.argv[1]
text = open(src).read()
if not text.startswith("---\n"):
    sys.stderr.write(f"WARN: no frontmatter, emitting body only: {src}\n")
    sys.stdout.write(text)
    sys.exit(0)

end = text.find("\n---\n", 4)
if end == -1:
    sys.stderr.write(f"WARN: no closing ---, emitting body only: {src}\n")
    sys.stdout.write(text)
    sys.exit(0)

fm = text[4:end]
body = text[end+5:]

# Drop blocks. We treat any line starting with `<key>:` as a top-level key,
# and skip until the next top-level key or end. Indented continuation lines
# belong to the previous key.
out_lines = []
skip_until_next_key = False
for line in fm.split("\n"):
    # Top-level key match: starts at column 0, has a colon
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:", line)
    if m:
        key = m.group(1)
        skip_until_next_key = key in KEYS_TO_DROP
        if not skip_until_next_key:
            out_lines.append(line)
    else:
        # Continuation (indented) — keep iff the most recent top-level key was kept
        if not skip_until_next_key:
            out_lines.append(line)

new_fm = "\n".join(out_lines)
preamble = """## Copilot harness adapter

This file was rendered from an Asha command source. Treat slash-command and Claude `Task` references below as workflow intent, not literal Copilot tool names. When the workflow asks for agents, use Copilot agents when available; otherwise execute the same phases inline and preserve the output contract.

"""
sys.stdout.write(f"---\n{new_fm}\n---\n{preamble}{body}")
PYEOF
)"

  # Idempotent write. On the unchanged path the dest mtime is STILL bumped —
  # the drift check compares mtimes, and a content-identical dest with an old
  # mtime would be flagged stale forever (mirrors the codex twin,
  # harnesses/codex.sh — omitting this made `asha doctor copilot` flap).
  if [[ -f "$dest" ]]; then
    local current; current="$(cat "$dest")"
    if [[ "$current" == "$content" ]]; then
      [[ $DRY_RUN -eq 1 ]] || touch "$dest"
      log "[copilot] command-skill unchanged: $dest"
      return 0
    fi
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    say "  EMIT [copilot-command-skill]  $src -> $dest"
  else
    printf '%s' "$content" > "$dest"
    log "emitted [copilot-command-skill]: $dest (from $src)"
  fi
}

# Generate a Copilot .agent.md from a Claude agent MD. KEEP-list policy:
# Copilot's agent loader tolerance for unknown frontmatter keys is UNVERIFIED,
# and asha agent files carry Claude-vocabulary keys (`tools: Bash, Edit, ...`,
# Claude `model:` names, `memory:`/`ownership:`/`dispatch_priority:` and a tail
# of one-off keys). v1 keeps exactly {name, description} — dropped tools: means
# unrestricted default, a graceful degradation. Revisit after plant-and-probe;
# the policy lives in the KEYS_TO_KEEP set below.
# Body passes through unchanged. Idempotent (only writes on diff).
_copilot_emit_agent_md() {
  local src="$1" dest="$2"

  local content
  content="$(python3 - "$src" <<'PYEOF'
import re, sys

KEYS_TO_KEEP = {"name", "description"}

src = sys.argv[1]
text = open(src).read()
if not text.startswith("---\n"):
    sys.stderr.write(f"WARN: no frontmatter, emitting body only: {src}\n")
    sys.stdout.write(text)
    sys.exit(0)

end = text.find("\n---\n", 4)
if end == -1:
    sys.stderr.write(f"WARN: no closing ---, emitting body only: {src}\n")
    sys.stdout.write(text)
    sys.exit(0)

fm = text[4:end]
body = text[end+5:]

out_lines = []
keep_current_key = False
for line in fm.split("\n"):
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:", line)
    if m:
        keep_current_key = m.group(1) in KEYS_TO_KEEP
        if keep_current_key:
            out_lines.append(line)
    else:
        if keep_current_key:
            out_lines.append(line)

new_fm = "\n".join(out_lines)
sys.stdout.write(f"---\n{new_fm}\n---\n{body}")
PYEOF
)"

  if [[ -f "$dest" ]]; then
    local current; current="$(cat "$dest")"
    if [[ "$current" == "$content" ]]; then
      # mtime bump on the unchanged path — same rationale as the command-skill
      # emitter above (mtime-based freshness audits must not flag this).
      [[ $DRY_RUN -eq 1 ]] || touch "$dest"
      log "[copilot] agent unchanged: $dest"
      return 0
    fi
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    say "  EMIT [copilot-agent]  $src -> $dest"
  else
    printf '%s' "$content" > "$dest"
    log "emitted [copilot-agent]: $dest (from $src)"
  fi
}
