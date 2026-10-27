# Version traps: 4.x code on 5.2

Each entry was run headless with `--factory-startup` on Blender 4.5.14 LTS and
5.2.3 LTS. "4.5" and "5.2" below are those builds; a change between them landed
in 5.0, 5.1 or 5.2. Entries marked *both* had already changed by 4.5 and still
catch code remembered from 3.x. For anything not listed, check
`bpy.app.version` and read Blender's per-version release notes
(developer.blender.org, "Release Notes").

Start scripts with a version check:

```python
import bpy

if bpy.app.version < (5, 2, 0):
    raise SystemExit(f"needs Blender 5.2 or later, got {bpy.app.version_string}")
```

| Area | 4.x code that fails | 5.2 code | Observed |
|---|---|---|---|
| Render engine | `render.engine = "BLENDER_EEVEE_NEXT"` | `render.engine = "BLENDER_EEVEE"` | 5.2 raises `TypeError: enum "BLENDER_EEVEE_NEXT" not found`; 4.5 rejects `BLENDER_EEVEE` the same way. |
| Compositor | `scene.node_tree` | `scene.compositing_node_group` | `Scene` has no `node_tree` in 5.2. `scene.use_nodes` still reads (True) but warns `DeprecationWarning: 'Scene.use_nodes' is expected to be removed in Blender 6.0`. |
| Animation | `action.fcurves` | `bpy_extras.anim_utils.action_get_channelbag_for_slot(action, slot).fcurves`, with `slot = ob.animation_data.action_slot` | `Action` has no `fcurves` in 5.2. The channelbag call works on 4.5 too, so use it for code that must run on both. |
| Video and multilayer output | `image_settings.file_format = "FFMPEG"` | `image_settings.media_type = "VIDEO"` (selects `FFMPEG`) | With `media_type` `IMAGE`, 5.2 raises `TypeError` for `FFMPEG` and `OPEN_EXR_MULTILAYER`; `MULTI_LAYER_IMAGE` selects the latter. 4.5 has no `media_type`. |
| Custom properties | `ob["my_prop"]` or `ob.get("my_prop")` for a property defined with `bpy.props` | `ob.my_prop` | 5.2: `ob.get()` returns `None` and `ob[...]` raises `KeyError`; 4.5 returned the value. Plain ID properties (`ob["x"] = 1.5`) still read with `ob.get("x")`. |
| Materials | `mat.use_nodes = True` before touching `mat.node_tree` | use `mat.node_tree` directly | 5.2 creates new materials with a node tree; setting `use_nodes` warns it is expected to be removed in 6.0. On 4.5 a new material's `node_tree` is `None` until `use_nodes = True`; `if mat.node_tree is None: mat.use_nodes = True` serves both. |
| EEVEE settings | `scene.eevee.use_gtao` | (removed) | Present in 4.5, absent in 5.2. *Both*: `use_bloom`, `use_ssr`, `use_soft_shadows`, `shadow_cube_size` are absent in 4.5 and 5.2. |
| Operator context | `bpy.ops.object.select_all({"scene": scene}, action="DESELECT")` | `with bpy.context.temp_override(scene=scene): bpy.ops.object.select_all(action="DESELECT")` | *Both*: `ValueError: 1-2 args execution context is supported`. |
| OBJ, STL, PLY I/O | `export_scene.obj`, `import_scene.obj`, `export_mesh.stl`, `import_mesh.stl`, `export_mesh.ply` | `wm.obj_export`, `wm.obj_import`, `wm.stl_export`, `wm.stl_import`, `wm.ply_export` | *Both*: the old operators do not exist. |
| Principled BSDF inputs | `"Specular"`, `"Emission"`, `"Subsurface"`, `"Transmission"`, `"Clearcoat"`, `"Sheen"` | `"Specular IOR Level"`, `"Emission Color"` (with `"Emission Strength"`), `"Subsurface Weight"`, `"Transmission Weight"`, `"Coat Weight"`, `"Sheen Weight"` | *Both*: none of the old input names exist on a new material's Principled BSDF. |
| Smooth shading | `mesh.use_auto_smooth = True` | `mesh.shade_smooth()` | *Both*: `Mesh` has no `use_auto_smooth`; `shade_smooth()` exists in both. |

Additions in 5.2 that 4.5 lacks, so a script that uses them needs 5.x: the
`ACES 1.3` and `ACES 2.0` view transforms, the `Rec.2100-PQ` and
`Rec.2100-HLG` display devices, `bpy.data.colorspace.working_space`, and the
`AVIF` output format.
