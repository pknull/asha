#!/usr/bin/env bash
# Render every agent through every harness adapter in disposable homes.
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

command -v jq >/dev/null 2>&1 || { echo "SKIP: jq not available" >&2; exit 0; }
command -v python3 >/dev/null 2>&1 || { echo "SKIP: python3 not available" >&2; exit 0; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK"/{home,asha,claude,codex,copilot,opencode,tmp}
printf '{}\n' > "$WORK/claude/settings.json"
: > "$WORK/codex/config.toml"

INSTALL_LOG="$WORK/install.log"
if ! env -i \
    PATH=/usr/bin:/bin \
    HOME="$WORK/home" \
    LANG=C.UTF-8 \
    TMPDIR="$WORK/tmp" \
    ASHA_HOME="$WORK/asha" \
    CLAUDE_HOME="$WORK/claude" \
    CODEX_HOME="$WORK/codex" \
    COPILOT_HOME="$WORK/copilot" \
    ASHA_OPENCODE_HOME="$WORK/opencode" \
    bash "$REPO_ROOT/install.sh" --target all --only code,panel,rp,session,test,write \
    >"$INSTALL_LOG" 2>&1; then
  cat "$INSTALL_LOG" >&2
  exit 1
fi

python3 - "$REPO_ROOT" "$WORK" <<'PY'
import json
import re
import sys
from pathlib import Path

import yaml

root = Path(sys.argv[1])
work = Path(sys.argv[2])
capabilities = json.loads((root / "harnesses/capabilities.json").read_text(encoding="utf-8"))
namespaces = json.loads((root / "namespaces.json").read_text(encoding="utf-8"))
enforced = capabilities["agent_frontmatter"]["tool_allowlist_enforced"]
agent_files = sorted(root.glob("plugins/*/agents/*.md"))
errors = []


def frontmatter(path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("missing opening frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError("missing closing frontmatter")
    data = yaml.safe_load(text[4:end]) or {}
    if not isinstance(data, dict):
        raise ValueError("frontmatter is not a mapping")
    return data


expected = {name: set() for name in ("claude", "codex", "copilot", "opencode")}
for source in agent_files:
    data = frontmatter(source)
    plugin = source.parent.parent.name
    namespace = namespaces[plugin]
    declared = data["name"]
    sanitized = declared.replace(":", "-")
    if ":" in sanitized:
        errors.append(f"{source.relative_to(root)}: sanitizer retained a colon")

    paths = {
        # Claude's native namespace is a directory and its native filename is
        # the source filename; generated adapters use the declared name.
        "claude": work / "claude/agents" / namespace / source.name,
        "codex": work / "codex/agents" / f"{namespace}-{sanitized}.toml",
        "copilot": work / "copilot/agents" / f"{namespace}-{sanitized}.agent.md",
        "opencode": work / "opencode/agents" / f"{namespace}-{sanitized}.md",
    }
    for harness, path in paths.items():
        expected[harness].add(path)
        if not path.exists():
            errors.append(f"{harness}: missing rendered agent {path.relative_to(work)}")
            continue
        if ":" in str(path.relative_to(work / harness / "agents")):
            errors.append(f"{harness}: colon in rendered path {path.relative_to(work)}")

    if paths["claude"].exists():
        has_tools = "tools" in frontmatter(paths["claude"])
        if has_tools != enforced["claude"]:
            errors.append(f"claude: tools presence disagrees with enforcement registry for {source.name}")
    if paths["codex"].exists():
        text = paths["codex"].read_text(encoding="utf-8")
        has_tools = re.search(r"^tools\s*=", text, re.MULTILINE) is not None
        if has_tools != enforced["codex"]:
            errors.append(f"codex: tools presence disagrees with enforcement registry for {source.name}")
    for harness in ("copilot", "opencode"):
        if paths[harness].exists():
            has_tools = "tools" in frontmatter(paths[harness])
            if has_tools != enforced[harness]:
                errors.append(f"{harness}: tools presence disagrees with enforcement registry for {source.name}")

patterns = {
    "claude": "*.md",
    "codex": "*.toml",
    "copilot": "*.agent.md",
    "opencode": "*.md",
}
for harness, pattern in patterns.items():
    actual = set((work / harness / "agents").rglob(pattern))
    if actual != expected[harness]:
        missing = sorted(expected[harness] - actual)
        extra = sorted(actual - expected[harness])
        if missing:
            errors.append(f"{harness}: expected paths absent: {', '.join(str(p.relative_to(work)) for p in missing)}")
        if extra:
            errors.append(f"{harness}: unexpected agent paths: {', '.join(str(p.relative_to(work)) for p in extra)}")

# Rendered non-Claude bodies may describe the same role boundary, but any
# claim that a tool allowlist is enforced must carry its Claude-only qualifier.
claim = re.compile(
    r"(?:tool|frontmatter) allowlist.{0,200}enforc\w*|enforc\w*.{0,200}(?:tool|frontmatter) allowlist",
    re.IGNORECASE | re.DOTALL,
)
qualifier = re.compile(r"claude(?: code)?(?:[- ]only|\s+alone)", re.IGNORECASE)
for harness in ("codex", "copilot", "opencode"):
    for path in expected[harness]:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for match in claim.finditer(text):
            context = text[max(0, match.start() - 160):match.end() + 160]
            if not qualifier.search(context):
                errors.append(
                    f"{harness}: unqualified tool-allowlist enforcement claim in {path.relative_to(work)}"
                )

if errors:
    print("\n".join(errors), file=sys.stderr)
    raise SystemExit(1)
print(f"rendered {len(agent_files)} agents through claude, codex, copilot, and opencode")
PY

# Canonical names are colon-free, so exercise the adapters' compatibility
# sanitizer with an invalid legacy name created only inside the temp tree.
mkdir -p "$WORK/probe/plugins/legacy/agents"
cat > "$WORK/probe/plugins/legacy/agents/legacy.md" <<'EOF'
---
name: character:legacy
description: Legacy colon-name filename probe.
tools: []
---
Probe.
EOF

for harness in codex copilot opencode; do
  (
    set -euo pipefail
    export HOME="$WORK/home"
    export ASHA_HOME="$WORK/asha"
    export CODEX_HOME="$WORK/codex"
    export COPILOT_HOME="$WORK/copilot"
    export ASHA_OPENCODE_HOME="$WORK/opencode"
    export TMPDIR="$WORK/tmp"
    # shellcheck source=../lib/install.sh
    source "$REPO_ROOT/lib/install.sh"
    PLUGINS_DIR="$WORK/probe/plugins"
    DRY_RUN=0
    FORCE=0
    VERBOSE=0
    ONLY=""
    # shellcheck disable=SC1090
    source "$REPO_ROOT/harnesses/$harness.sh"
    if [[ "$harness" == opencode ]]; then
      asha_artifact_begin opencode
    fi
    "${harness}_install_agents" legacy legacy
    if [[ "$harness" == opencode ]]; then
      asha_artifact_finalize opencode 0
    fi
  )
done

[[ -f "$WORK/codex/agents/legacy-character-legacy.toml" ]]
[[ -f "$WORK/copilot/agents/legacy-character-legacy.agent.md" ]]
[[ -f "$WORK/opencode/agents/legacy-character-legacy.md" ]]
if find "$WORK"/{codex,copilot,opencode}/agents -maxdepth 1 -type f -name '*:*' -print -quit | grep -q .; then
  echo "colon remained in a rendered compatibility-probe filename" >&2
  exit 1
fi

echo "test-agent-render: PASS"
