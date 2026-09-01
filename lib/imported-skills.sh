#!/usr/bin/env bash
# source-scoped library: no set flags or execution at file scope
# lib/imported-skills.sh — imported skill enumeration and mount adapters.
#
# Sourced by lib/install.sh after MARKET_ROOT and portable helpers are available.
# The functions intentionally reuse the install engine's die/log/say/ensure_dir/
# mklink helpers and the harnesses' existing source enumeration machinery.

# The user-owned import plane is deliberately outside the repository. Skills
# from it use one stable namespace on every harness and remain opt-in under a
# scoped install (`--only imported` or an unscoped install).
asha_imported_skills_root() {
  printf '%s/skills\n' "${ASHA_HOME:-$HOME/.asha}"
}

_asha_imported_skills_selected() {
  [[ -z "${ONLY:-}" ]] && return 0
  local item
  local -a _asha_only_items
  IFS=',' read -ra _asha_only_items <<<"$ONLY"
  for item in "${_asha_only_items[@]}"; do
    [[ "$item" == imported ]] && return 0
  done
  return 1
}

# Emit the additional selected user skill source as tab-separated:
#   source-root  namespace  kind  label
# Repository plugins keep their existing per-plugin install order; the user
# source enters that same harness skill installer under the `imported` kind.
selected_imported_skill_sources() {
  local imported_root
  if _asha_imported_skills_selected; then
    imported_root="$(asha_imported_skills_root)"
    [[ -d "$imported_root" ]] \
      && printf '%s\t%s\t%s\t%s\n' "$imported_root" imported imported imported
  fi
}

# Enumerate skill directories within one selected source. The repository is
# authoritative for bundled sources. The user store is authoritative only
# through imported.lock.json: an untracked directory must never become an
# executable harness surface merely because it exists under ~/.asha/skills.
skill_dirs_from_source() {
  local src_dir="$1" kind="$2" skill name lock
  if [[ "$kind" != imported ]]; then
    for skill in "$src_dir"/*/; do
      [[ -d "$skill" ]] || continue
      printf '%s\n' "${skill%/}"
    done
    return 0
  fi

  [[ "${ASHA_IMPORTED_SKILLS_DRIFTED:-0}" != 1 ]] || return 0
  lock="$src_dir/imported.lock.json"
  [[ -f "$lock" ]] || return 0
  while IFS= read -r name; do
    printf '%s\n' "$src_dir/$name"
  done < <(jq -r '.skills | keys[]' "$lock")
}

# Validate an imported source in the harness process (not inside process
# substitution), so a malformed lock or missing recorded directory fails that
# harness loudly instead of being mistaken for an empty source.
validate_skill_source() {
  local src_dir="$1" kind="$2" skill name lock drift_rc=0
  [[ "$kind" == imported ]] || return 0
  ASHA_IMPORTED_SKILLS_DRIFTED=0
  lock="$src_dir/imported.lock.json"
  [[ -e "$lock" || -L "$lock" ]] || return 0
  [[ -f "$lock" && ! -L "$lock" ]] \
    || die "imported skill lockfile must be a regular file: $lock" 4
  while IFS= read -r name; do
    [[ "$name" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] \
      || die "invalid imported skill name in lockfile: $name" 4
    [[ ${#name} -le 64 ]] \
      || die "invalid imported skill name in lockfile (must be 1-64 characters): $name" 4
    [[ $((9 + ${#name})) -le 64 ]] \
      || die "imported skill mount name exceeds Agent Skills 64-character limit: imported-$name" 4
    skill="$src_dir/$name"
    [[ -d "$skill" && -f "$skill/SKILL.md" ]] \
      || die "imported skill recorded but missing SKILL.md: $skill" 4
  done < <(jq -r '.skills | keys[]' "$lock")
  python3 - "$MARKET_ROOT/plugins/asha/skills/find-skills/tools" "$lock" <<'PY' \
    || drift_rc=$?
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from find_skills_common import ValidationError
from find_skills_store import load_lock, status_store

lock_path = Path(sys.argv[2])
try:
    document = load_lock(lock_path)
    asha_home = lock_path.parent.parent
    for name in document["skills"]:
        status = status_store(asha_home, name)["skills"][0]
        if status["state"] != "clean":
            unsafe = (lock_path.parent / name).is_symlink() or any(
                issue["kind"] == "unsupported-local-symlink"
                for issue in status["issues"]
            )
            raise SystemExit(
                f"imported skill has drifted: {name} at {lock_path.parent / name}"
                if not unsafe else 3
            )
except ValidationError:
    raise SystemExit(2) from None
PY
  case "$drift_rc" in
    0) ;;
    1) ASHA_IMPORTED_SKILLS_DRIFTED=1 ;;
    3) die "imported skill has unsafe symlink drift: $src_dir" 4 ;;
    *) die "invalid imported skill lockfile: $lock" 4 ;;
  esac
}

# Build one harness-neutral mount adapter for an imported skill. The canonical
# store remains byte-for-byte upstream; only the derived SKILL.md name changes
# to match the portable `imported-<name>` mount directory.
prepare_imported_skill_adapter() {
  local source="$1" mounted_name="$2" root adapters adapter stage
  root="$(asha_imported_skills_root)"
  adapters="$root/.mounts"
  adapter="$adapters/$mounted_name"
  [[ "$mounted_name" =~ ^imported-[a-z0-9]+(-[a-z0-9]+)*$ ]] \
    || die "invalid imported skill mount name: $mounted_name" 4
  [[ ${#mounted_name} -le 64 ]] \
    || die "imported skill mount name exceeds Agent Skills 64-character limit: $mounted_name" 4
  # Sourced harness adapters consume this declared output channel immediately.
  # shellcheck disable=SC2034
  ASHA_IMPORTED_SKILL_ADAPTER="$adapter"
  if [[ ${DRY_RUN:-0} -eq 1 ]]; then
    log "would derive imported skill adapter: $source -> $adapter"
    return 0
  fi
  [[ ! -L "$adapters" ]] || die "imported skill adapter root must not be a symlink: $adapters" 4
  ensure_dir "$adapters"
  stage="$(mktemp -d "$adapters/.${mounted_name}.XXXXXX")"
  if ! cp -R "$source/." "$stage/"; then
    rm -rf "$stage"
    die "failed to copy imported skill into mount adapter: $source" 4
  fi
  if [[ -L "$stage/SKILL.md" ]]; then
    rm -rf "$stage"
    die "imported SKILL.md must not be a symlink: $source/SKILL.md" 4
  fi
  if ! python3 - "$stage/SKILL.md" "$mounted_name" <<'PY'
import sys
from pathlib import Path
try:
    import yaml
    from yaml.events import (
        AliasEvent,
        MappingEndEvent,
        MappingStartEvent,
        ScalarEvent,
        SequenceEndEvent,
        SequenceStartEvent,
    )
except ImportError:
    raise SystemExit(
        f"PyYAML is required to adapt imported skill {sys.argv[2]}; "
        "install PyYAML for python3 and retry"
    ) from None

path, name = Path(sys.argv[1]), sys.argv[2]
text = path.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)
if not lines or lines[0].rstrip("\r\n") != "---":
    raise SystemExit("imported SKILL.md lost its frontmatter")
end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "---"), -1)
if end < 0:
    raise SystemExit("imported SKILL.md lost its frontmatter")
frontmatter = "".join(lines[1:end])
parsed = yaml.safe_load(frontmatter)
if not isinstance(parsed, dict) or not isinstance(parsed.get("name"), str):
    raise SystemExit("imported SKILL.md must contain one frontmatter name")

events = list(yaml.parse(frontmatter))

def consume(index):
    event = events[index]
    if not isinstance(event, (MappingStartEvent, SequenceStartEvent)):
        return index + 1
    end_type = MappingEndEvent if isinstance(event, MappingStartEvent) else SequenceEndEvent
    index += 1
    while not isinstance(events[index], end_type):
        index = consume(index)
    return index + 1

root = next((i for i, event in enumerate(events) if isinstance(event, MappingStartEvent)), -1)
matches = []
index = root + 1
while root >= 0 and not isinstance(events[index], MappingEndEvent):
    key_index = index
    index = consume(index)
    value_index = index
    index = consume(index)
    key = events[key_index]
    if isinstance(key, ScalarEvent) and key.value == "name":
        matches.append(events[value_index])
if len(matches) > 1 or (
    matches and not isinstance(matches[0], (ScalarEvent, AliasEvent))
):
    raise SystemExit("imported SKILL.md must contain one frontmatter name")

if not matches:
    root_event = events[root]
    if root_event.flow_style:
        insert = root_event.end_mark.index
        frontmatter = frontmatter[:insert] + f"name: {name}, " + frontmatter[insert:]
    else:
        indent = " " * root_event.end_mark.column
        frontmatter = f"{indent}name: {name}\n" + frontmatter
else:
    value = matches[0]
    replacements = [(value.start_mark.index, value.end_mark.index, name)]
    if isinstance(value, ScalarEvent) and value.anchor:
        replacements.extend(
            (event.start_mark.index, event.end_mark.index, parsed["name"])
            for event in events
            if isinstance(event, AliasEvent) and event.anchor == value.anchor
        )
    for start, stop, replacement in sorted(replacements, reverse=True):
        frontmatter = frontmatter[:start] + replacement + frontmatter[stop:]
try:
    rewritten = yaml.safe_load(frontmatter)
except yaml.YAMLError as exc:
    raise SystemExit(
        f"rewritten frontmatter is invalid YAML for imported skill {name}: {exc}"
    ) from None
if not isinstance(rewritten, dict) or rewritten.get("name") != name:
    raise SystemExit(
        f"rewritten frontmatter does not carry the name for imported skill {name}"
    )
lines[1:end] = [frontmatter]
path.write_text("".join(lines), encoding="utf-8")
PY
  then
    rm -rf "$stage"
    die "failed to rewrite imported skill mount name: $source" 4
  fi
  [[ ! -L "$adapter" ]] || { rm -rf "$stage"; die "imported skill adapter must not be a symlink: $adapter" 4; }
  [[ ! -e "$adapter" || -d "$adapter" ]] \
    || { rm -rf "$stage"; die "imported skill adapter must be a directory: $adapter" 4; }
  rm -rf "$adapter"
  mv "$stage" "$adapter"
}

# Upgrade an older direct canonical-store link without requiring --force.
# Any other destination remains foreign and goes through mklink's refusal.
mklink_imported_skill() {
  local canonical="$1" adapter="$2" dest="$3" kind="$4" current expected
  if [[ -L "$dest" ]]; then
    current="$(resolve_path "$dest" 2>/dev/null || true)"
    expected="$(resolve_path "$canonical" 2>/dev/null || true)"
    if [[ -n "$expected" && "$current" == "$expected" ]]; then
      if [[ ${DRY_RUN:-0} -eq 1 ]]; then
        say "  LINK [$kind]  $adapter -> $dest (replaces direct imported mount)"
        return 0
      fi
      rm -f "$dest"
    fi
  fi
  mklink "$adapter" "$dest" "$kind"
}

# Return the canonical imported skill name for a direct store mount or its
# hidden derived mount adapter. Other nested and foreign paths are not owned.
imported_skill_name_from_target() {
  local target="$1" root="$2" abs_root="$3" name
  case "$target" in
    "$root"/*) name="${target#"$root"/}" ;;
    *)
      [[ -n "$abs_root" ]] || return 1
      case "$target" in "$abs_root"/*) name="${target#"$abs_root"/}" ;; *) return 1 ;; esac
      ;;
  esac
  case "$name" in
    .mounts/imported-*) name="${name#.mounts/imported-}" ;;
  esac
  [[ "$name" != */* && "$name" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || return 1
  printf '%s\n' "$name"
}
