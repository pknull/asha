# Invocation

Everything here ran on Blender 5.2.3 LTS, headless, with `--factory-startup`.
The commands use `SKILL` (the installed skill directory), `OUT` (an absolute
scratch directory) and `IN` (an input .blend).

## Command shape

```bash
# Run a script on a file (leave out "$IN" to start from the factory scene):
blender -b "$IN" --factory-startup --python-exit-code 1 -P /abs/script.py -- --out "$OUT/x.blend"
# Run scripts, then render one frame, in one process:
blender -b "$IN" --factory-startup --python-exit-code 1 -P /abs/first.py -P /abs/second.py \
    -E CYCLES -o "$OUT/frames/frame_####" -F PNG -f 1 -- --out "$OUT/x.blend"
```

Blender executes its arguments left to right:

- A `.blend` path loads where it appears. A render setting given before it is
  replaced by the file's own.
- `-E`, `-o` and `-F` apply to the `-f` (one frame) or `-a` (the frame range)
  that follows them. With `-o` after `-f 1`, the frame went to the file's own
  output path instead.
- Each `-P` script runs where it appears, in the same process: preferences and
  scene changes made by one are seen by the next and by a later `-f`.
- `--` ends Blender's options. Whatever follows is left in `sys.argv` for the
  scripts, and Cycles reads its own `--cycles-device` from there too.

Read script options with `parse_known_args`, so arguments meant for another
script or for Cycles do not abort the parse, and turn off prefix matching, so
another script's `--req` cannot switch on your `--require-gpu`:

```python
import argparse, sys

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
parser = argparse.ArgumentParser(prog="script.py", allow_abbrev=False)
parser.add_argument("--out", required=True)
args, _ = parser.parse_known_args(argv)
```

`argparse` exits 2 on a missing or malformed option, before the script prints
anything else. The bundled scripts override `ArgumentParser.error` so a bad
option becomes a failed result line instead.

One-liners need no file:

```bash
blender -b --factory-startup --python-exit-code 1 \
    --python-expr 'import bpy; print("RESULT", bpy.app.version_string)'
```

## Render a .blend from the command line

```bash
blender -b /abs/scene.blend --factory-startup --python-exit-code 1 \
    -E CYCLES -o "$OUT/frames/frame_####" -F PNG -f 1 > "$OUT/render.log" 2>&1
echo "exit=$?"
```

- `-f 1` renders frame 1; `-s 1 -e 24 -a` renders a range.
- `#` in the `-o` path becomes the zero-padded frame number. A path with no `#`
  gets `####` appended.
- Under `--factory-startup` this renders Cycles on the CPU whatever the file
  says. To use a GPU, run `-P "$SKILL/scripts/cycles_devices.py"` before `-f`
  (see [render.md](render.md#render-devices)).

## Exit codes

| Situation | Exit code |
|---|---|
| Script raises, with `--python-exit-code 1` | 1 |
| Script raises, without it | 0 (the traceback is only printed) |
| Script calls `sys.exit(3)` | 3 |
| Script raises `SystemExit("message")` | 1, message printed |
| `-f` cannot create or write its output directory | 1 |
| `argparse` rejects an option | 2 |
| `--cycles-device` names a backend with no device | 1 |

Blender creates a missing output directory itself (`$OUT/frames/` above), so
the exit 1 case is a directory it cannot create, such as one under a read-only
parent. An exit code of 0 is necessary, not sufficient: also check the result
line and the output files.

## Result line

End every script with one line a caller can parse, after all other output:

```python
print("RESULT " + json.dumps(result), flush=True)
if not result["ok"]:
    sys.exit(1)
```

Blender prints its own lines after the script ends (`Blender quit`), so find the
line by its prefix: `grep '^RESULT ' run.log`.

## Script skeleton

An idempotent build script: it removes what an earlier run made and rebuilds
it, and it saves a copy to a new path. Rerunning it on its own output changes
nothing.

```python
"""Add a generated plinth to a scene and save a copy.

blender -b /abs/in.blend --factory-startup --python-exit-code 1 \
    -P build.py -- --out /abs/out/scene.blend
(leave out /abs/in.blend to start from the factory scene)
"""
import argparse
import json
import os
import sys

import bpy

TAG, OWNER = "built_by", "build.py"   # marks the objects this script owns


def script_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(prog="build.py", allow_abbrev=False)
    parser.add_argument("--out", required=True)
    args, _ = parser.parse_known_args(argv)
    return args


def ensure_collection(scene, name):
    """The child collection this script made on an earlier run, or a new one."""
    for child in scene.collection.children:
        if child.get(TAG) == OWNER:
            return child
    made = bpy.data.collections.new(name)   # may come back as name.001
    made[TAG] = OWNER
    scene.collection.children.link(made)
    return made


def main():
    if bpy.app.version < (5, 2, 0):
        raise SystemExit(f"needs Blender 5.2 or later, got {bpy.app.version_string}")
    args = script_args()
    if not os.path.isabs(args.out):
        raise SystemExit(f"--out must be an absolute path: {args.out}")
    if bpy.data.filepath and os.path.realpath(args.out) == os.path.realpath(bpy.data.filepath):
        raise SystemExit("refusing to overwrite the input file")
    scene = bpy.context.scene
    target = ensure_collection(scene, "generated")
    # A rerun starts by deleting what the last run built, wherever it now sits.
    stale = [ob for ob in bpy.data.objects if ob.get(TAG) == OWNER]
    stale_meshes = [ob.data for ob in stale if ob.type == "MESH"]
    bpy.data.batch_remove(stale)
    for old in stale_meshes:
        if old.users == 0:
            bpy.data.meshes.remove(old)
    mesh = bpy.data.meshes.new("plinth")
    mesh.from_pydata([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.validate()
    ob = bpy.data.objects.new("plinth", mesh)
    ob[TAG] = OWNER
    target.objects.link(ob)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=args.out, copy=True)
    result = {
        "ok": True,
        "blender": bpy.app.version_string,
        "input": bpy.data.filepath,
        "out": args.out,
        "objects": sorted(o.name for o in bpy.data.objects),
        "collections": sorted(c.name for c in bpy.data.collections),
        "meshes": len(bpy.data.meshes),
    }
    print("RESULT " + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
```

Notes on the pattern:

- A name is only a request: when `name` is taken, `bpy.data.*.new(name)` hands
  back `name.001`. Hold on to the object `new()` returns; a lookup by the
  requested name can find the older one.
- A datablock with no users is not written to the saved file: link new objects
  into a collection that is in the scene.
- `save_as_mainfile(copy=True)` writes the copy and leaves the open file as it
  was (`bpy.data.filepath` still names the input).
- Under `--factory-startup` a save over an existing path keeps the previous
  version as `<name>.blend1`.

## Etiquette details

- One process at a time. Before starting, `pgrep -ix blender`; wait while it
  finds one. `-x` matches the process name (`-i` ignores its case); `pgrep -f`
  matches any process whose command line mentions a Blender path, including the
  calling agent.
- `-b` always. A run without it opens a window on the user's screen.
- `--factory-startup` starts from Blender's built-in defaults instead of the
  user's preferences, add-ons and startup file, and leaves
  `bpy.app.online_access` False.
- Blender's Python ignores `PYTHONPATH` unless `--python-use-system-env` is
  given. Making extra modules available to it is the packages skill's work.
- Never write next to an input or over it; write to `OUT` and report the paths.
