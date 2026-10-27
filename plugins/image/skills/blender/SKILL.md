---
name: image-blender
description: "Drive Blender 5.2 LTS from scripts: headless invocation, bpy scripting, scene and render setup, Cycles and EEVEE settings, choosing and proving the render device, colour management, glTF, FBX, OBJ, USD and STL import and export, 4.x to 5.x API traps, and proving a run worked. Use when running Blender headless or in the background, writing or fixing a bpy script, rendering a .blend from the command line, setting up a Cycles GPU render, exporting glTF or FBX, or when a remembered Blender API fails on 5.x."
---

# Blender (tool layer)

How to drive Blender itself: run it headless, script it with `bpy`, set up and
render a scene, pick the render device, pin colour management, move files in
and out, and prove a run worked. Written for Blender 5.2 LTS.

This skill is the mechanics only. Modelling, topology, UVs, materials, lighting
and art judgement belong to a craft skill; a project's look belongs to that
project's own documents. Installing or pinning Blender, adding Python modules to
Blender's Python, and allowing Blender in each harness belong to the packages
skill. A craft or project skill can say "load `image-blender` for the
mechanics" and build on what follows.

## Before a run

```bash
blender --version                 # first line names the version, e.g. "Blender 5.2.3 LTS"
pgrep -ix blender || echo idle    # one Blender at a time: wait while another runs
SKILL=/abs/path/to/this/skill     # the installed skill directory
OUT=/abs/path/to/scratch          # outputs go here, never beside the inputs
IN=/abs/path/to/input.blend       # the file to work on, when there is one
```

Match the process name, ignoring case, with `pgrep -ix blender`. `pgrep -f`
also matches any process whose arguments merely mention a Blender path, an
agent session included, and then waits forever.

## Run headless

```bash
blender -b "$IN" --factory-startup --python-exit-code 1 \
    -P /abs/script.py -- --out "$OUT/result.blend"
```

Leave out `"$IN"` to start from the factory scene.

| Flag | Why |
|---|---|
| `-b` | Background mode: no window. Without it Blender opens a GUI on the user's screen. |
| `--factory-startup` | Starts from Blender's built-in defaults: the user's saved preferences, add-ons and GPU choice are not read, so runs are reproducible. Online access stays off. |
| `--python-exit-code 1` | An uncaught exception exits 1; otherwise Blender reports success (0) even when the script raised. |
| `-P script.py` | Runs a script file; several `-P` run in order in one process. |
| `--python-expr '...'` | Runs an inline script, for one-liners. |
| `--` | Ends Blender's options. Everything after it is the script's, read from `sys.argv`. |

Arguments run left to right: the `.blend` loads where it appears, and an `-o`,
`-F` or `-E` placed after the `-f` or `-a` does not apply to that render. Details, exit
codes and a script skeleton: [`references/invocation.md`](references/invocation.md).

## Etiquette

- Never open a GUI window on the user's machine: always pass `-b`.
- Start one Blender at a time, after checking none is running: another session
  may be using the GPU.
- Absolute paths for every input and output; `//` in a render path means
  "relative to the .blend".
- Keep scripts idempotent: get-or-create by name, tag what the script owns, and
  remove and rebuild only that on a rerun.
- Never overwrite an input. Save a copy
  (`bpy.ops.wm.save_as_mainfile(filepath=..., copy=True)`) to a new path.
  Saving over an existing file leaves a `.blend1` backup beside it.

## Script shape

1. Check `bpy.app.version` first and stop with a clear message when it is too
   old (`raise SystemExit(...)` exits 1).
2. Read options after `--` with `argparse` `parse_known_args`, since other
   scripts and Cycles' `--cycles-device` share that space.
3. Prefer the data API (`bpy.data.*.new`, `link`, `batch_remove`) to operators.
   Positional context dictionaries were removed: wrap an operator that needs a
   context in `with bpy.context.temp_override(...)`.
4. End by printing one machine-readable line, `RESULT {json}`, with
   `flush=True`, and `sys.exit(1)` after it when a check failed.

## Render

- Engine identifiers in 5.2: `CYCLES`, `BLENDER_EEVEE`, `BLENDER_WORKBENCH`.
- Cycles samples: `scene.cycles.samples` (factory default 4096, adaptive);
  EEVEE: `scene.eevee.taa_render_samples` (default 64). Set them explicitly.
- Choose the Cycles device by probing, never by assuming a vendor:
  `-P "$SKILL/scripts/cycles_devices.py"` before the render enables the first
  available backend (OptiX, CUDA, HIP, oneAPI, Metal) or falls back to the CPU.
- `--factory-startup` starts with no compute device. A scene set to `GPU` then
  renders on the CPU and still exits 0, so enable a device in the same process,
  after the `.blend` is loaded (name the file before the `-P`).
- Prove the device: add `--debug-cycles` and read the log's
  `Path tracing on: <device>` entry (`grep -A4 'Path tracing on:'`; with
  `--with-cpu` or `+CPU` the CPU is listed on the lines after it).

Settings, output formats and device routes: [`references/render.md`](references/render.md).

## Colour management

Pin the view transform, look, exposure, gamma and display device in every
render script and record them in the result line. They decide the output
pixels: at 0.5 linear, Standard writes 188 and AgX 170 in 8-bit PNG, and
Standard clips at 1.0 where AgX compresses. With the same settings 4.5.14 and
5.2.3 produced identical pixel values, so after an upgrade or a file change
compare the colour settings before anything else. Factory default: AgX, look
None. Compare renders by decoded pixels, never by file bytes (PNG files carry
date and render-time metadata, and 5.2 writes colour chunks 4.5 did not). For measurement, render to OpenEXR (linear,
view transform not applied) or set `scene.render.dither_intensity = 0` (the
default 1.0 adds ±1 code noise).
Details and the measured table: [`references/render.md`](references/render.md#colour-management).

## Import and export

| Format | Export | Import |
|---|---|---|
| glTF / GLB | `bpy.ops.export_scene.gltf` | `bpy.ops.import_scene.gltf` |
| FBX | `bpy.ops.export_scene.fbx` | `bpy.ops.import_scene.fbx` |
| OBJ | `bpy.ops.wm.obj_export` | `bpy.ops.wm.obj_import` |
| USD | `bpy.ops.wm.usd_export` | `bpy.ops.wm.usd_import` |
| STL | `bpy.ops.wm.stl_export` | `bpy.ops.wm.stl_import` |

The old `export_scene.obj` and `export_mesh.stl` names are gone. Selection,
modifier and axis options differ per format (glTF does not apply modifiers by
default; FBX spells axes `-Z`, the others `NEGATIVE_Z`): see
[`references/io.md`](references/io.md).

## Version traps

Check `bpy.app.version` before trusting remembered code. The 4.x code that
fails on 5.2 most often:

- `BLENDER_EEVEE_NEXT` is now `BLENDER_EEVEE`.
- `scene.node_tree` is gone: the compositor is `scene.compositing_node_group`.
- `action.fcurves` is gone: use
  `bpy_extras.anim_utils.action_get_channelbag_for_slot(action, slot).fcurves`.
- Video and multilayer EXR need `image_settings.media_type` set first.
- A `bpy.props` property is no longer readable as `ob["name"]` or `ob.get("name")`.

The full table, each entry run on 4.5.14 and 5.2.3:
[`references/version-traps.md`](references/version-traps.md).

## Verify a run

A run worked only when all of these hold:

1. The exit code is 0 (with `--python-exit-code 1`).
2. The script's `RESULT` line says `"ok": true` and the log shows no Python
   exception.
3. Each output file exists and is non-empty; a render has the expected size, an
   export reads back with the expected objects.
4. For a Cycles render, the `Path tracing on:` entry names the intended
   device.

## Scripts

- `scripts/cycles_devices.py`: probe Cycles backends and enable one in this
  process. Options after `--`: `--backend auto|CPU|<TYPE>`, `--with-cpu`,
  `--require-gpu`, `--probe-only`.
- `scripts/smoke.py`: prove an install end to end. It builds a tiny scene,
  renders it, exports glb, fbx, obj, usd and stl and reads each back:
  `blender -b --factory-startup --python-exit-code 1 --debug-cycles -P "$SKILL/scripts/smoke.py" -- --out "$OUT/smoke"`.
  `--out` must be new, empty, or an earlier smoke run's directory.

Both print one `RESULT {json}` line and exit 1 on failure.

## References

- [`references/invocation.md`](references/invocation.md): command line, argument order, exit codes, script skeleton.
- [`references/render.md`](references/render.md): engines, samples, output, devices, colour management.
- [`references/io.md`](references/io.md): import and export options, selection, round-trip check.
- [`references/version-traps.md`](references/version-traps.md): 4.x to 5.x changes, verified.

Headless conventions adapted from scenario-labs/skills `scenario-blender-expert` (MIT, commit 72b763c), re-verified on 5.2.3.
