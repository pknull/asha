#!/usr/bin/env bash
# source-scoped library: no set flags at file scope (runs in the caller's shell)
# Ownership ledger for generated (non-symlink) harness artifacts.
# Manifest location is independent of harness homes so source deletion/rename
# cannot erase the evidence required for safe uninstall.

asha_artifact_manifest_path() {
  printf '%s/install-manifests/%s.json\n' "${ASHA_HOME:-$HOME/.asha}" "$1"
}

asha_artifact_begin() {
  # Consumed by separately sourced harness emitters while a manifest is active.
  # shellcheck disable=SC2034
  ASHA_ARTIFACT_HARNESS="$1"
  ASHA_ARTIFACT_STAGE="${TMPDIR:-/tmp}/asha-artifacts-$1-$$.jsonl"
  : > "$ASHA_ARTIFACT_STAGE"
}

asha_artifact_record() {
  local source="$1" destination="$2" type="$3" hash="$4"
  python3 - "$source" "$destination" "$type" "$hash" >> "$ASHA_ARTIFACT_STAGE" <<'PY'
import hashlib, json, os, sys
source, destination, kind, digest = sys.argv[1:]
print(json.dumps({
    "source": os.path.abspath(source),
    "destination": os.path.abspath(destination),
    "type": kind,
    "sha256": digest,
    "orphan": False,
}, sort_keys=True))
PY
}

asha_artifact_hash() {
  python3 - "$1" <<'PY'
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as f:
    for block in iter(lambda: f.read(1024 * 1024), b""):
        h.update(block)
print(h.hexdigest())
PY
}

asha_artifact_manifest_hash_for() {
  local harness="$1" destination="$2" manifest
  manifest="$(asha_artifact_manifest_path "$harness")"
  [[ -f "$manifest" ]] || return 1
  python3 - "$manifest" "$destination" <<'PY'
import json, os, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
dest = os.path.abspath(sys.argv[2])
for item in data.get("artifacts", []):
    if os.path.abspath(item.get("destination", "")) == dest:
        print(item.get("sha256", ""))
        raise SystemExit(0)
raise SystemExit(1)
PY
}

# Install a prepared file and record ownership. Foreign files are preserved
# unless their bytes already equal the deterministic output or --force is set.
asha_artifact_install_prepared() {
  local harness="$1" source="$2" destination="$3" type="$4" prepared="$5"
  local expected current recorded=""
  expected="$(asha_artifact_hash "$prepared")"
  recorded="$(asha_artifact_manifest_hash_for "$harness" "$destination" 2>/dev/null || true)"

  if [[ -e "$destination" || -L "$destination" ]]; then
    if [[ -d "$destination" && ! -L "$destination" ]]; then
      die "refusing to replace directory with generated artifact: $destination" 2
    fi
    if [[ -L "$destination" ]]; then
      [[ ${FORCE:-0} -eq 1 ]] \
        || die "refusing to overwrite foreign symlink artifact: $destination (use --force)" 2
      [[ ${DRY_RUN:-0} -eq 1 ]] || rm -f "$destination"
    elif [[ ! -f "$destination" ]]; then
      die "refusing to overwrite foreign non-file artifact: $destination" 2
    fi
    if [[ -f "$destination" && ! -L "$destination" ]]; then
      current="$(asha_artifact_hash "$destination")"
      if [[ -n "$recorded" && "$current" != "$recorded" && "$current" != "$expected" && ${FORCE:-0} -eq 0 ]]; then
        die "refusing to overwrite modified managed artifact: $destination (use --force)" 2
      fi
      if [[ -z "$recorded" && "$current" != "$expected" && ${FORCE:-0} -eq 0 ]]; then
        die "refusing to overwrite foreign generated artifact: $destination (use --force)" 2
      fi
    fi
  fi

  if [[ ${DRY_RUN:-0} -eq 1 ]]; then
    say "  EMIT [$type]  $source -> $destination"
    return 0
  fi
  ensure_dir "$(dirname "$destination")"
  if [[ ! -f "$destination" ]] || ! cmp -s "$prepared" "$destination"; then
    local tmp="$destination.tmp.$$"
    cat "$prepared" > "$tmp"
    mv "$tmp" "$destination"
  fi
  asha_artifact_record "$source" "$destination" "$type" "$expected"
}

asha_artifact_finalize() {
  local harness="$1" full="${2:-1}" manifest stage tmp
  manifest="$(asha_artifact_manifest_path "$harness")"
  stage="${ASHA_ARTIFACT_STAGE:-}"
  [[ -n "$stage" && -f "$stage" ]] || return 0
  if [[ ${DRY_RUN:-0} -eq 1 ]]; then rm -f "$stage"; return 0; fi
  ensure_dir "$(dirname "$manifest")"
  tmp="$manifest.tmp.$$"
  python3 - "$manifest" "$stage" "$harness" "$full" > "$tmp" <<'PY'
import hashlib, json, os, sys
manifest, stage, harness, full = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
try:
    old = json.load(open(manifest, encoding="utf-8")).get("artifacts", [])
except (OSError, ValueError):
    old = []
desired = []
with open(stage, encoding="utf-8") as f:
    for line in f:
        if line.strip(): desired.append(json.loads(line))
by_dest = {os.path.abspath(x["destination"]): x for x in desired}
for item in old:
    dest = os.path.abspath(item.get("destination", ""))
    if not dest or dest in by_dest: continue
    kept = dict(item)
    if full:
        expected = item.get("sha256", "")
        if os.path.isfile(dest) and not os.path.islink(dest):
            actual = hashlib.sha256(open(dest, "rb").read()).hexdigest()
            if actual == expected:
                os.unlink(dest)
                try: os.rmdir(os.path.dirname(dest))
                except OSError: pass
                continue
        if not os.path.lexists(dest):
            continue
        kept["orphan"] = True
    by_dest[dest] = kept
out = {
    "schema_version": 1,
    "harness": harness,
    "artifacts": sorted(by_dest.values(), key=lambda x: x["destination"]),
}
print(json.dumps(out, indent=2, sort_keys=True))
PY
  mv "$tmp" "$manifest"
  rm -f "$stage"
}

# Remove only files whose bytes still match the recorded installed hash.
# Modified files and their ownership records are preserved for manual review.
asha_artifact_uninstall() {
  local harness="$1" manifest tmp result removed
  manifest="$(asha_artifact_manifest_path "$harness")"
  [[ -f "$manifest" ]] || { echo 0; return 0; }
  python3 - "$manifest" <<'PY' >/dev/null 2>&1 || die "invalid generated-artifact manifest: $manifest" 2
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert isinstance(data, dict) and isinstance(data.get("artifacts", []), list)
PY
  if [[ ${DRY_RUN:-0} -eq 1 ]]; then
    python3 - "$manifest" <<'PY'
import json, sys
for x in json.load(open(sys.argv[1])).get("artifacts", []): print("  RM (managed)  " + x["destination"], file=sys.stderr)
PY
    python3 - "$manifest" <<'PY'
import json, sys
print(len(json.load(open(sys.argv[1])).get("artifacts", [])))
PY
    return 0
  fi
  tmp="$manifest.tmp.$$"
  result="$(python3 - "$manifest" "$tmp" <<'PY'
import hashlib, json, os, sys
manifest, out = sys.argv[1:]
data = json.load(open(manifest, encoding="utf-8"))
kept, removed = [], 0
for item in data.get("artifacts", []):
    dest, expected = item["destination"], item.get("sha256", "")
    if not os.path.lexists(dest):
        removed += 1
        continue
    if os.path.isfile(dest) and not os.path.islink(dest):
        h = hashlib.sha256(open(dest, "rb").read()).hexdigest()
        if h == expected:
            os.unlink(dest); removed += 1
            parent = os.path.dirname(dest)
            try: os.rmdir(parent)
            except OSError: pass
            continue
    print(f"WARN: preserving modified managed artifact: {dest}", file=sys.stderr)
    kept.append(item)
data["artifacts"] = kept
if kept:
    json.dump(data, open(out, "w", encoding="utf-8"), indent=2, sort_keys=True); open(out, "a").write("\n")
print(removed)
PY
)"
  removed="${result:-0}"
  if [[ -s "$tmp" ]]; then mv "$tmp" "$manifest"; else rm -f "$tmp" "$manifest"; fi
  echo "$removed"
}

asha_artifact_doctor() {
  local harness="$1" manifest
  manifest="$(asha_artifact_manifest_path "$harness")"
  [[ -f "$manifest" ]] || return 2
  python3 - "$manifest" <<'PY'
import hashlib, json, os, sys
try: data = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError) as e:
    print(f"invalid manifest: {e}")
    raise SystemExit(1)
bad = 0
for x in data.get("artifacts", []):
    dest, source = x.get("destination", ""), x.get("source", "")
    if x.get("orphan") or not os.path.exists(source):
        print(f"orphan: {dest} (source: {source})"); bad += 1; continue
    if not os.path.isfile(dest) or os.path.islink(dest):
        print(f"missing: {dest}"); bad += 1; continue
    actual = hashlib.sha256(open(dest, "rb").read()).hexdigest()
    if actual != x.get("sha256"):
        print(f"modified: {dest}"); bad += 1
raise SystemExit(1 if bad else 0)
PY
}
