# Import and export

Operator names and defaults below are read from Blender 5.2.3 LTS under
`--factory-startup` (the glTF and FBX add-ons are enabled there by default).
Every export and import was run.

## Operators and their options

| Format | Export | Import | Extension |
|---|---|---|---|
| glTF | `bpy.ops.export_scene.gltf` | `bpy.ops.import_scene.gltf` | `.glb`, `.gltf` |
| FBX | `bpy.ops.export_scene.fbx` | `bpy.ops.import_scene.fbx` | `.fbx` |
| OBJ | `bpy.ops.wm.obj_export` | `bpy.ops.wm.obj_import` | `.obj` (+ `.mtl`) |
| USD | `bpy.ops.wm.usd_export` | `bpy.ops.wm.usd_import` | `.usd`, `.usda`, `.usdc` |
| STL | `bpy.ops.wm.stl_export` | `bpy.ops.wm.stl_import` | `.stl` |
| PLY | `bpy.ops.wm.ply_export` | `bpy.ops.wm.ply_import` | `.ply` |

Options that change what lands in the file (factory default in brackets):

| | glTF | FBX | OBJ | USD | STL |
|---|---|---|---|---|---|
| Selected objects only | `use_selection` [False] | `use_selection` [False] | `export_selected_objects` [False] | `selected_objects_only` [False] | `export_selected_objects` [False] |
| Apply modifiers | `export_apply` [**False**] | `use_mesh_modifiers` [True] | `apply_modifiers` [True] | `evaluation_mode` [`RENDER`] | `apply_modifiers` [True] |
| Axes | `export_yup` [True] | `axis_forward` [`-Z`], `axis_up` [`Y`] | `forward_axis` [`NEGATIVE_Z`], `up_axis` [`Y`] | `export_global_forward_selection` [`NEGATIVE_Z`], `export_global_up_selection` [`Y`], used when `convert_orientation` [False] | `forward_axis` [`Y`], `up_axis` [`Z`] |
| Scale | none | `global_scale` [1.0], `apply_unit_scale` [True], `apply_scale_options` [`FBX_SCALE_NONE`] | `global_scale` [1.0] | `convert_scene_units` [`METERS`], `meters_per_unit` [1.0] | `global_scale` [1.0], `use_scene_unit` [False] |
| Materials | `export_materials` [`EXPORT`] | written | `export_materials` [True] | `export_materials` [True] | none |

Format notes:

- glTF `export_format` is `GLB` (one binary file) or `GLTF_SEPARATE`
  (`.gltf` + `.bin`). `GLTF_EMBEDDED` is offered only when the glTF add-on
  preference `allow_embedded_format` is on, which `--factory-startup` leaves
  off (4.5 and 5.2 alike). The glTF exporter does not apply modifiers unless
  `export_apply=True`:
  a cube with one subdivision level exported 24 vertices without it and 96
  with it.
- FBX spells axes `-Z`; OBJ, STL and USD spell them `NEGATIVE_Z`. Passing one
  spelling to the other operator raises `TypeError`.
- FBX `object_types` limits what is written: `{"MESH"}` leaves out the camera,
  lights, armatures and empties.
- OBJ writes a `.mtl` beside the `.obj` when `export_materials` is on.
- USD picks binary or text from the extension; with the default
  `export_textures_mode` (`NEW`) it creates a `textures/` directory beside the
  file.
- The importers mirror the exporters: `import_scene.gltf`, `import_scene.fbx`
  (`global_scale`, `axis_forward`, `axis_up`), `wm.obj_import` and
  `wm.stl_import` (`global_scale`, `forward_axis`, `up_axis`), `wm.usd_import`
  (`scale`, `import_materials`).
- The pre-4.x names `export_scene.obj`, `import_scene.obj`, `export_mesh.stl`,
  `import_mesh.stl` and `export_mesh.ply` do not exist in 4.5 or 5.2.

## Export the selection and read it back

Selection works headless once the object is in the view layer. Read the file
back to prove the export: check what the importer adds. An import selects the
objects it adds and deselects everything else, so select again before any
later export that uses the selection.

```python
"""Export one object to GLB and prove it reads back.

blender -b /abs/scene.blend --factory-startup --python-exit-code 1 \
    -P export_one.py -- --object Cube --out /abs/out/cube.glb
"""
import argparse
import json
import os
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
parser = argparse.ArgumentParser(prog="export_one.py")
parser.add_argument("--object", required=True)
parser.add_argument("--out", required=True)
args, _ = parser.parse_known_args(argv)

target = bpy.data.objects[args.object]
for ob in bpy.context.view_layer.objects:
    ob.select_set(ob == target)
bpy.context.view_layer.objects.active = target
os.makedirs(os.path.dirname(args.out), exist_ok=True)
bpy.ops.export_scene.gltf(filepath=args.out, export_format="GLB",
                          use_selection=True, export_apply=True)

before = set(bpy.data.objects)
bpy.ops.import_scene.gltf(filepath=args.out)
added = [ob for ob in bpy.data.objects if ob not in before]
result = {
    "ok": os.path.getsize(args.out) > 0 and [ob.type for ob in added] == ["MESH"],
    "out": args.out,
    "bytes": os.path.getsize(args.out),
    "reimported": [ob.name for ob in added],
}
bpy.data.batch_remove(added)
print("RESULT " + json.dumps(result), flush=True)
if not result["ok"]:
    sys.exit(1)
```

`scripts/smoke.py` runs the same round trip for glb, fbx, obj, usd and stl.

## .blend files

- Load a file by naming it on the command line (`blender -b /abs/in.blend ...`),
  or from a script with `bpy.ops.wm.open_mainfile(filepath=...)`.
- Save with `bpy.ops.wm.save_as_mainfile(filepath=..., copy=True)` to a new
  path; see [invocation.md](invocation.md#script-skeleton) for the copy and
  `.blend1` behaviour.
